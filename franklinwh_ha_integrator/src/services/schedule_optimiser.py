"""Derive a dispatch schedule from a tariff.

Entering a tariff and then hand-building a matching schedule is the same
information typed twice, and the second time is the part people get wrong.
Given the rates, the shape of a sensible schedule follows: charge when import
is cheapest, hold through expensive import, export only when selling beats the
import it displaces.

The output is a **preset**, not a live schedule. It lands beside the built-ins
so it can be inspected, loaded, discarded, or selected later by an automation
or the tou_saved_dispatches entity — nothing reaches the battery until someone
chooses it. A generated schedule applied silently would be a change to how a
house runs, proposed by inference from a rate table.

Rules-based on purpose. An LP solver over forecast load and generation is a
different and much larger piece; the rules below cover the plans people
actually have, and every decision carries the reason it was made, because a
schedule nobody can interrogate is one they will undo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

# Dispatch ids — canonical set from src.services.dispatch_codes.
DISPATCH_SOLAR_EXPORT = 1   # Home Loads: solar runs the house, surplus exports,
                            # battery untouched
DISPATCH_STANDBY = 2
DISPATCH_SELF = 6
DISPATCH_EXPORT = 7
DISPATCH_GRID_CHARGE = 8

DISPATCH_LABELS = {
    DISPATCH_SOLAR_EXPORT: "Home Loads",
    DISPATCH_STANDBY: "Standby",
    DISPATCH_SELF: "Self Consumption",
    DISPATCH_EXPORT: "Grid Export",
    DISPATCH_GRID_CHARGE: "Grid Charge",
}

# Standby parks the battery: it neither charges nor supplies the house. On a
# price-based TOU schedule there is no block where that is the right answer —
# the worst case is "do not move energy", and Self Consumption already does
# that whenever solar covers the load. Standby during an expensive block is
# strictly worse: it buys at peak what the battery was holding. It stays in
# DISPATCH_LABELS because snapshots and hand-built schedules contain it, but
# the optimiser never emits it.
NEVER_EMIT = frozenset({DISPATCH_STANDBY})

# The `name` on a block is the only part of a schedule a person reads on the
# gateway. "Self Consumption" describes the dispatch; it does not say what the
# block is *for*. These state the intent, including the SOC bound the block
# runs to, so a schedule can be audited without recomputing the rates.
INTENT_CHARGE_SOLAR = "Charge from Solar"
INTENT_CHARGE_GRID = "Charge from Grid to MaxChargeSOC"
INTENT_EXPORT = "Export to Grid to MinDischargeSOC"
INTENT_EXPORT_SOLAR = "Export Solar to Grid"
INTENT_SELF_PEAK = "Self Consumption to MinDischargeSOC"
INTENT_SELF = "Self Consumption"

# The floor a generated schedule discharges to. Not 0: leaving nothing in
# reserve turns any forecast error into a peak-rate import, and the point of
# the reserve is that the forecast is a median, not a guarantee.
BATTERY_FLOOR_SOC = 10

# Charge targets are sized to cover forecast peak load plus this, for the same
# reason — a target sized exactly to the median is under target half the time.
CHARGE_HEADROOM_PCT = 10

# Rated usable energy per aPower unit, used only when the caller cannot supply
# a measured capacity. Matches gateway_service's derivation.
RATED_KWH_PER_APOWER = 13.6

# waveType → the suffix of its rate field, matching franklinwh_tou and the
# schedule editor. Duplicated deliberately: importing the pricing adapter here
# would drag a provider into what is otherwise pure arithmetic.
WAVE_RATE_SUFFIX = {0: "Valley", 1: "Shoulder", 2: "Peak", 3: "Sharp", 4: "SuperOffPeak"}

# A block within this margin of the day's cheapest import counts as "cheapest".
# Without it, two blocks differing by a hundredth of a cent would be treated as
# meaningfully different.
CHEAP_MARGIN = 1.02

# Charging from the grid only pays if a later block is dearer by more than the
# round-trip loss. 15% is a conservative stand-in for battery efficiency.
ARBITRAGE_MARGIN_C = 5.0
ROUND_TRIP_EFFICIENCY = 0.85

OBJECTIVES = ("lowest_bill", "max_export", "max_self_consumption")

# Solar above this, averaged across a block, is treated as enough to charge the
# battery without buying. Paying for grid energy in the middle of a sunny day
# is the most obvious way for a tariff-only optimiser to look foolish.
SOLAR_SELF_SUFFICIENT_KW = 1.0


@dataclass
class BlockDecision:
    start: str
    end: str
    wave_type: int
    dispatch_id: int
    import_c: float
    export_c: float
    reason: str
    solar_led: bool = False
    is_peak: bool = False
    min_discharge_soc: int = BATTERY_FLOOR_SOC
    max_charge_soc: int = 100

    @property
    def intent(self) -> str:
        """What the block is for, not merely which dispatch it selects."""
        if self.dispatch_id == DISPATCH_GRID_CHARGE:
            return INTENT_CHARGE_GRID
        if self.dispatch_id == DISPATCH_SOLAR_EXPORT:
            return INTENT_EXPORT_SOLAR
        if self.dispatch_id == DISPATCH_EXPORT:
            return INTENT_EXPORT
        if self.dispatch_id == DISPATCH_SELF and self.solar_led:
            return INTENT_CHARGE_SOLAR
        if self.dispatch_id == DISPATCH_SELF and self.is_peak:
            return INTENT_SELF_PEAK
        return INTENT_SELF

    def to_block(self) -> dict[str, Any]:
        """The preset schedule shape: what load_preset hands the editor.

        SOC bounds are written explicitly. The push path defaults them to
        100/0, and a 0 floor means the schedule will flatten the battery on the
        first day the forecast is wrong.
        """
        return {
            "startHourTime": self.start,
            "endHourTime": self.end,
            "waveType": self.wave_type,
            "name": self.intent,
            "dispatchId": self.dispatch_id,
            "minDischargeSoc": self.min_discharge_soc,
            "maxChargeSoc": self.max_charge_soc,
        }


@dataclass
class SeasonPlan:
    season_name: str
    months: str
    decisions: list[BlockDecision] = field(default_factory=list)
    soc: dict[str, Any] = field(default_factory=dict)


def _to_cents(value) -> float:
    """Gateway rates are $/kWh; this module works in cents.

    Guarded rather than multiplied blindly — both units appear depending on how
    a schedule was written, and inflating an already-cents value by 100 would
    make every decision nonsense in a plausible-looking way.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v * 100.0 if abs(v) < 10.0 else v


def _rates_for_wave(daytype: dict, wave) -> tuple[float, float]:
    suffix = WAVE_RATE_SUFFIX.get(int(wave) if wave is not None else 0, "Valley")
    return (
        _to_cents(daytype.get(f"eleticRate{suffix}")),
        _to_cents(daytype.get(f"eleticSell{suffix}")),
    )


def _decide(
    import_c: float,
    export_c: float,
    cheapest_import: float,
    dearest_import: float,
    objective: str,
    solar_kw: float = 0.0,
) -> tuple[int, str]:
    """One block's dispatch, and why.

    The ladder is ordered by how much money each rule is worth, so the first
    rule that fires is the one with the strongest claim on the battery.
    """
    # 1. Selling beats avoiding a purchase. The comparison is against *this*
    #    block's import rate, not the day's peak: a kWh exported now is a kWh
    #    not available to displace an import now.
    if export_c > import_c:
        # Export the sun, not the battery. Both earn the same rate per kWh, but
        # Grid Export ("aPower to home/grid") actively empties a battery that
        # has to carry the evening peak, whereas Home Loads ("aPower to home")
        # exports only the solar the house did not use. It still discharges to
        # cover home load — it is not a hold — but it never sends stored energy
        # to the grid. Only when there is no sun to sell does discharging
        # become the way to earn the export rate.
        if solar_kw >= SOLAR_SELF_SUFFICIENT_KW and objective != "max_export":
            return DISPATCH_SOLAR_EXPORT, (
                f"Export {export_c:.3g}c beats avoiding {import_c:.3g}c import, and "
                f"~{solar_kw:.3g} kW of solar is expected — sell the surplus rather "
                f"than discharging the battery to the grid"
            )
        return DISPATCH_EXPORT, (
            f"Export {export_c:.3g}c beats avoiding {import_c:.3g}c import"
        )

    if objective == "max_export" and export_c > 0:
        return DISPATCH_EXPORT, f"Export {export_c:.3g}c — maximising export revenue"

    # 2. Cheapest import of the day, and a later block dear enough to be worth
    #    the round trip. Charging costs import_c now to avoid dearest_import
    #    later, less what the battery loses storing it.
    if import_c <= cheapest_import * CHEAP_MARGIN:
        worth = dearest_import * ROUND_TRIP_EFFICIENCY - import_c

        # Do not buy what the roof is about to supply. Without this the
        # optimiser grid-charges straight through the middle of a sunny day
        # purely because the rate is low, which is both wasteful and the
        # quickest way to lose a user's trust in the suggestion.
        if solar_kw >= SOLAR_SELF_SUFFICIENT_KW:
            return DISPATCH_SELF, (
                f"Import is cheap ({import_c:.3g}c) but ~{solar_kw:.3g} kW of solar is "
                f"expected — charge from the roof, not the grid"
            )

        if worth > ARBITRAGE_MARGIN_C and objective != "max_self_consumption":
            return DISPATCH_GRID_CHARGE, (
                f"Cheapest import {import_c:.3g}c; fills before {dearest_import:.3g}c peak "
                f"(worth ~{worth:.3g}c/kWh after losses)"
            )
        # Previously Standby. Standby parks the battery — it will not supply
        # the house even when the house is drawing — so a cheap block spent in
        # Standby buys from the grid what solar or the battery could have
        # covered. Self Consumption is the correct "do nothing special": it
        # charges from surplus solar and covers load, without buying to store.
        return DISPATCH_SELF, (
            f"Cheapest import {import_c:.3g}c, but the {dearest_import:.3g}c peak "
            f"does not cover round-trip losses — self-consume rather than buy to store"
        )

    # 3. Expensive import: use stored energy rather than buy.
    return DISPATCH_SELF, (
        f"Avoid {import_c:.3g}c import; export only pays {export_c:.3g}c"
    )


def _solar_for_block(solar_by_hour: dict, start: str, end: str) -> float:
    """Mean expected solar across a block, in kW.

    `solar_by_hour` must be keyed by **local** hour, because schedule blocks are
    local-time boundaries. Passing UTC hours silently shifts the whole day —
    a Sydney site matched an overnight block against UTC 00:00-07:00, which is
    the middle of its afternoon, and concluded 4 kW of sun was expected at 2am.

    Mean rather than peak: a block sunny for one hour of eight is not
    self-sufficient for all eight.
    """
    if not solar_by_hour:
        return 0.0
    try:
        sh = int(str(start).split(":")[0])
        eh = int(str(end).split(":")[0])
    except (ValueError, AttributeError):
        return 0.0
    if eh <= sh:
        eh += 24
    hours = [h % 24 for h in range(sh, eh)]
    if not hours:
        return 0.0
    return sum(float(solar_by_hour.get(h, 0.0)) for h in hours) / len(hours)


def _block_hours(start: str, end: str) -> float:
    """Block duration in hours, handling the wrap past midnight."""
    try:
        sh, sm = (int(x) for x in str(start).split(":")[:2])
        eh, em = (int(x) for x in str(end).split(":")[:2])
    except (ValueError, AttributeError):
        return 0.0
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes <= 0:
        minutes += 24 * 60
    return minutes / 60.0


def _energy_kwh(load_by_hour: dict, start: str, end: str) -> float:
    """Forecast household consumption across a block, in kWh.

    Mean kW across the block times its length. `load_by_hour` is keyed by
    **local** hour, for the same reason the solar profile is — a UTC-keyed map
    plans the evening peak against the middle of the night.
    """
    if not load_by_hour:
        return 0.0
    try:
        sh = int(str(start).split(":")[0])
        eh = int(str(end).split(":")[0])
    except (ValueError, AttributeError):
        return 0.0
    if eh <= sh:
        eh += 24
    hours = [h % 24 for h in range(sh, eh)]
    if not hours:
        return 0.0
    mean_kw = sum(float(load_by_hour.get(h, 0.0)) for h in hours) / len(hours)
    return mean_kw * _block_hours(start, end)


def size_soc_bounds(
    decisions: list,
    load_by_hour: Optional[dict],
    battery_kwh: float,
) -> dict[str, Any]:
    """Set the SOC bounds so the battery actually covers the expensive blocks.

    The bound that matters is the one on the charge blocks. A schedule can name
    every block correctly and still import at peak, because the battery was
    never told to hold enough to get through it — the charge target defaults to
    100 and the discharge floor to 0, so the shape looks right and the reserve
    is accidental.

    So: forecast the household's consumption across the peak blocks, express it
    as a percentage of usable capacity, and make every charge block target that
    much plus a margin. The peak blocks themselves get the floor, because the
    whole point is that the battery is allowed to supply the house.

    When the forecast peak load exceeds what the battery can hold, that is
    reported rather than silently clamped: no schedule fixes it, and the honest
    answer is that peak will draw from the grid.
    """
    peaks = [d for d in decisions if d.is_peak]
    result: dict[str, Any] = {
        "peak_load_kwh": 0.0,
        "reserve_soc_pct": 0,
        "charge_target_soc": 100,
        "sufficient": True,
        "note": "",
    }
    if not peaks or not load_by_hour or battery_kwh <= 0:
        # Without a load history there is nothing to size against. Leave the
        # floor in place and say so, rather than inventing a target.
        result["note"] = (
            "No load history for this season — SOC bounds left at defaults "
            f"(floor {BATTERY_FLOOR_SOC}%)."
        )
        return result

    peak_kwh = sum(_energy_kwh(load_by_hour, d.start, d.end) for d in peaks)
    reserve_pct = peak_kwh / battery_kwh * 100.0
    target = BATTERY_FLOOR_SOC + reserve_pct + CHARGE_HEADROOM_PCT

    result["peak_load_kwh"] = round(peak_kwh, 3)
    result["reserve_soc_pct"] = int(round(reserve_pct))
    result["charge_target_soc"] = int(round(min(target, 100.0)))

    if target > 100.0:
        result["sufficient"] = False
        result["note"] = (
            f"Forecast peak load is {peak_kwh:.3g} kWh, which is more than "
            f"{battery_kwh:.3g} kWh of usable battery can cover between the "
            f"{BATTERY_FLOOR_SOC}% floor and full. Peak will draw from the grid "
            "for part of the window — shift load, or the schedule cannot fix it."
        )
    else:
        result["note"] = (
            f"Peak needs ~{peak_kwh:.3g} kWh ({result['reserve_soc_pct']}% of "
            f"{battery_kwh:.3g} kWh); charge blocks target "
            f"{result['charge_target_soc']}%."
        )

    for d in decisions:
        d.min_discharge_soc = BATTERY_FLOOR_SOC
        if d.dispatch_id == DISPATCH_GRID_CHARGE or (
            d.dispatch_id == DISPATCH_SELF and d.solar_led
        ):
            d.max_charge_soc = result["charge_target_soc"]
        else:
            d.max_charge_soc = 100

    return result


def optimise_season(
    season: dict,
    objective: str = "lowest_bill",
    solar_by_hour: Optional[dict] = None,
    load_by_hour: Optional[dict] = None,
    battery_kwh: float = 0.0,
) -> Optional[SeasonPlan]:
    """Decide a dispatch for every block in one season."""
    daytypes = season.get("dayTypeVoList") or []
    if not daytypes:
        return None

    daytype = daytypes[0]
    blocks = daytype.get("detailVoList") or []
    if not blocks:
        return None

    rates = [_rates_for_wave(daytype, b.get("waveType")) for b in blocks]
    imports = [r[0] for r in rates if r[0] > 0]
    if not imports:
        return None
    cheapest, dearest = min(imports), max(imports)

    plan = SeasonPlan(
        season_name=season.get("seasonName") or season.get("name") or "Season",
        months=str(season.get("month") or ""),
    )
    for blk, (import_c, export_c) in zip(blocks, rates):
        start = blk.get("startHourTime") or blk.get("startTime") or ""
        end = blk.get("endHourTime") or blk.get("endTime") or ""
        solar_kw = _solar_for_block(solar_by_hour or {}, start, end)
        dispatch, reason = _decide(
            import_c, export_c, cheapest, dearest, objective, solar_kw
        )
        # Belt and braces. The ladder no longer returns Standby, but a
        # generated schedule that parks the battery is bad enough to be worth
        # catching at the boundary rather than trusting every future branch.
        if dispatch in NEVER_EMIT:
            dispatch = DISPATCH_SELF
            reason += " (Standby is never generated on a price-based schedule)"

        plan.decisions.append(BlockDecision(
            start=start,
            end=end,
            wave_type=int(blk.get("waveType") or 0),
            dispatch_id=dispatch,
            import_c=import_c,
            export_c=export_c,
            reason=reason,
            solar_led=(dispatch == DISPATCH_SELF and solar_kw >= SOLAR_SELF_SUFFICIENT_KW),
            # "Peak" here is a price fact, not a waveType label: the blocks the
            # battery has to carry are the dear ones, whatever they are called.
            is_peak=(dispatch == DISPATCH_SELF
                     and import_c > cheapest * CHEAP_MARGIN
                     and solar_kw < SOLAR_SELF_SUFFICIENT_KW),
        ))

    plan.soc = size_soc_bounds(plan.decisions, load_by_hour, battery_kwh)
    return plan


def _active_plan(plans: list, day) -> Optional[SeasonPlan]:
    """The plan whose months contain this date, else the first.

    Months are a CSV and need not be contiguous, so this is membership rather
    than a range — AGL's peak season is Nov-Mar *and* Jun-Aug.
    """
    for plan in plans:
        months = {
            int(m) for m in str(plan.months or "").split(",") if m.strip().isdigit()
        }
        if day.month in months:
            return plan
    return plans[0] if plans else None


def optimise(
    strategy_list: list,
    objective: str = "lowest_bill",
    solar_by_hour: Optional[dict] = None,
    today=None,
    solar_by_season: Optional[dict] = None,
    load_by_hour: Optional[dict] = None,
    load_by_season: Optional[dict] = None,
    battery_kwh: float = 0.0,
) -> dict[str, Any]:
    """Propose a dispatch schedule for every season in a tariff.

    Returns the plan and the preset-shaped schedule separately: the caller
    shows the first and saves the second, and neither is applied here.
    """
    if objective not in OBJECTIVES:
        objective = "lowest_bill"

    # Per-season solar when history provides it, falling back to one profile
    # for all seasons. A season-specific figure matters: this site's shoulder
    # months peak at 4.55 kW and its peak season — which includes winter — at
    # 3.44 kW, so a single average would over-charge in one and under-charge in
    # the other.
    def _solar_for(season: dict) -> Optional[dict]:
        name = season.get("seasonName") or season.get("name")
        if solar_by_season and name in solar_by_season:
            return solar_by_season[name]
        return solar_by_hour

    def _load_for(season: dict) -> Optional[dict]:
        name = season.get("seasonName") or season.get("name")
        if load_by_season and name in load_by_season:
            return load_by_season[name]
        return load_by_hour

    plans = [
        p for p in (
            optimise_season(s, objective, _solar_for(s), _load_for(s), battery_kwh)
            for s in (strategy_list or [])
        ) if p
    ]
    active = _active_plan(plans, today or date.today())
    if not plans:
        return {"ok": False, "error": "No tariff blocks to optimise — set rates on the Schedule tab first.",
                "objective": objective, "seasons": [], "schedule": []}

    # The preset format carries one flat block list, so only one season's blocks
    # can be written. It must be the season that applies *now*, not simply the
    # first: loading a Peak-season plan during the shoulder months puts an
    # On-Peak block into a season that has no On-Peak rate, and the editor shows
    # a dash where the price should be. Every season's reasoning is still
    # returned, because comparing them is the point of showing the working.
    return {
        "ok": True,
        "objective": objective,
        "seasons": [
            {
                "season_name": p.season_name,
                "months": p.months,
                "soc": p.soc,
                "blocks": [
                    {
                        "start": d.start, "end": d.end,
                        "dispatch": DISPATCH_LABELS.get(d.dispatch_id, str(d.dispatch_id)),
                        "dispatch_id": d.dispatch_id,
                        "intent": d.intent,
                        "min_discharge_soc": d.min_discharge_soc,
                        "max_charge_soc": d.max_charge_soc,
                        "import_c": round(d.import_c, 4),
                        "export_c": round(d.export_c, 4),
                        "reason": d.reason,
                    }
                    for d in p.decisions
                ],
            }
            for p in plans
        ],
        "schedule": [d.to_block() for d in active.decisions] if active else [],
        "active_season": active.season_name if active else None,
        # Lifted to the top level because it is a finding about the site, not a
        # detail of one season: a battery that cannot carry the peak is the
        # single thing a user most needs told, and it must not be buried.
        "soc": active.soc if active else {},
        "soc_sufficient": bool(active.soc.get("sufficient", True)) if active else True,
    }
