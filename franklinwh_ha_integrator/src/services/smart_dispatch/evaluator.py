"""Rule evaluator + Condition DSL + StrategyMixer.

Extracted from `smart_dispatch/__init__.py` in v0.2.3 (Phase 1 Stage D,
2026-08-05). This is the biggest slice — the condition DSL, all eight
`_eval_*` category evaluators, the HA service caller, the dispatch
payload calculator, and the StrategyMixer class that resolves matrix
rows into an EvalDecision. All pure over `(ctx, cfg, ...)` except for
`_call_ha_service` and `StrategyMixer.evaluate` which touch db + HA.

Re-exports:
- `StrategyMixer` is imported by tests via `from src.services.smart_dispatch
  import StrategyMixer` — kept working via re-export in `__init__.py`.
- Everything else stays internal to the SD package."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from src.services import db
from src.services.notification_sender import _get_ha_credentials
from src.services.pricing.base import PriceSnapshot
from src.services.smart_dispatch.context import _build_context
from src.services.smart_dispatch.loads import is_schedule_active
from src.services.smart_dispatch.models import EvalDecision, RuleResult

logger = logging.getLogger(__name__)


# ── Condition DSL Evaluator ──────────────────────────────────────────────────

_OPERATOR_MAP = {
    "EQ":      lambda a, b: a == b,
    "NEQ":     lambda a, b: a != b,
    "LT":      lambda a, b: a is not None and a < b,
    "GT":      lambda a, b: a is not None and a > b,
    "LTE":     lambda a, b: a is not None and a <= b,
    "GTE":     lambda a, b: a is not None and a >= b,
    "IN":      lambda a, b: a in b,
    "NOT_IN":  lambda a, b: a not in b,
}


def _extract_field(ctx: dict[str, Any], field_name: str) -> Any:
    """
    Pull a field value from the evaluation context dict.
    Supports dot-notation for nested dicts (e.g. 'site.p_fhp').
    """
    if not field_name:
        return None
        
    if "." in field_name:
        parts = field_name.split(".")
        val = ctx
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return None
        return val
        
    return ctx.get(field_name)


def _eval_single(condition: dict, ctx: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate one leaf condition. Returns (matched, description_str)."""
    field_name  = condition.get("field", "")
    op_name     = condition.get("op", "EQ").upper()
    expected    = condition.get("value")
    
    value_ref   = condition.get("value_ref", "")
    if value_ref:
        resolved = _extract_field(ctx, value_ref)
        if resolved is not None:
            expected = resolved
            
    actual      = _extract_field(ctx, field_name)
    op_fn       = _OPERATOR_MAP.get(op_name)
    if op_fn is None:
        logger.warning(f"SmartDispatch: unknown operator '{op_name}' — skipping condition")
        return False, f"{field_name} {op_name} {expected!r} [UNKNOWN OP]"
    try:
        result = op_fn(actual, expected)
    except Exception as exc:
        logger.debug(f"SmartDispatch: condition eval error — {exc}")
        result = False
    desc = f"{field_name}={actual!r} {op_name} {expected!r}"
    return bool(result), desc


def evaluate_condition(condition_json: str, ctx: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Evaluate a DSL condition string against the context dict.
    Returns (matched: bool, descriptions: list[str]).

    Empty condition {} → always True (catch-all).
    """
    if not condition_json or condition_json.strip() in ("{}", ""):
        return True, ["(catch-all)"]

    try:
        cond = json.loads(condition_json)
    except Exception:
        logger.error(f"SmartDispatch: malformed condition JSON: {condition_json!r}")
        return False, ["[invalid JSON]"]

    if not cond:  # empty dict
        return True, ["(catch-all)"]

    operator = cond.get("operator", "AND").upper()
    sub_conditions = cond.get("conditions", [])

    if not sub_conditions:
        # Single leaf condition (no wrapper)
        matched, desc = _eval_single(cond, ctx)
        return matched, [desc]

    results = [_eval_single(c, ctx) for c in sub_conditions]
    descriptions = [d for _, d in results]
    matched_flags = [m for m, _ in results]

    if operator == "AND":
        final = all(matched_flags)
    elif operator == "OR":
        final = any(matched_flags)
    else:
        logger.warning(f"SmartDispatch: unknown logical operator '{operator}' — defaulting AND")
        final = all(matched_flags)

    return final, descriptions


# ── 6-Rule Category Evaluators ───────────────────────────────────────────────
# Each evaluator takes (ctx, engine_cfg, baseline_ok) and returns EvalDecision|None.
# Evaluated in priority order: first match wins.

from datetime import datetime

def _eval_time_schedules(ctx: dict, cfg: dict, baseline_ok: bool, schedules: list[dict]) -> Optional[EvalDecision]:
    """
    Priority 0 — Scheduled Time-Based SOC Override.
    Checks if current local time falls into any active schedule window.
    If SOC < schedule.min_soc, forces GRID_CHARGE.
    """
    if not schedules:
        return None
        
    soc = ctx.get("soc_pct")
    if soc is None:
        return None
        
    snap = ctx.get("snapshot")
    now = snap.valid_until if snap and hasattr(snap, "valid_until") else datetime.now()
    
    current_time_str = now.strftime("%H:%M")
    day_name = now.strftime("%A").lower()  # monday, tuesday, etc.
    is_weekend = now.weekday() >= 5
    day_of_month = now.day

    for sch in schedules:
        if not is_schedule_active(sch, now):
            continue
            
        start_t = sch.get("start_time", "00:00")
        end_t = sch.get("end_time", "23:59")
        period = sch.get("period", "").lower()
        target_soc = float(sch.get("min_soc", 0.0))
        action_intent = sch.get("action", "CHARGE").upper()
        
        if action_intent == "CHARGE":
            if soc < target_soc:
                return EvalDecision(
                    action="GRID_CHARGE",
                    preset_name=None,
                    rule_id=f"__schedule_{sch.get('id', 'temp')}__",
                    rule_name="Scheduled Charge",
                    priority=5,
                    reason=f"Period={period}, Window={start_t}-{end_t}, SOC={soc or 0.0:.1f}% < Target={target_soc:.1f}%",
                    conditions_met=["time_window_active", f"soc < {target_soc}"],
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                    trigger_category="time_schedule",
                    dispatch_summary=f"Scheduled Time window active. Forcing charge to {target_soc}%."
                )
            else:
                return EvalDecision(
                    action="HOLD",
                    preset_name=None,
                    rule_id=f"__schedule_{sch.get('id', 'temp')}__",
                    rule_name="Scheduled Charge Target Met",
                    priority=5,
                    reason=f"Period={period}, Window={start_t}-{end_t}, SOC={soc or 0.0:.1f}% >= Target={target_soc:.1f}%",
                    conditions_met=["time_window_active", f"soc >= {target_soc}"],
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                    trigger_category="time_schedule",
                    dispatch_summary=f"Scheduled Time window active. Target {target_soc}% met, holding."
                )
        elif action_intent == "DISCHARGE":
            if soc > target_soc:
                return EvalDecision(
                    action="GRID_EXPORT",
                    preset_name=None,
                    rule_id=f"__schedule_{sch.get('id', 'temp')}__",
                    rule_name="Scheduled Discharge",
                    priority=5,
                    reason=f"Period={period}, Window={start_t}-{end_t}, SOC={soc or 0.0:.1f}% > Target={target_soc:.1f}%",
                    conditions_met=["time_window_active", f"soc > {target_soc}"],
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                    trigger_category="time_schedule",
                    dispatch_summary=f"Scheduled Time window active. Forcing discharge to {target_soc}%."
                )
            else:
                # Discharge target already reached — no override needed, native mode continues
                return EvalDecision(
                    action="HOLD",
                    preset_name=None,
                    rule_id=f"__schedule_{sch.get('id', 'temp')}__",
                    rule_name="Scheduled Discharge Target Met",
                    priority=5,
                    reason=f"Period={period}, Window={start_t}-{end_t}, SOC={soc or 0.0:.1f}% <= Target={target_soc:.1f}% — target met, native mode continues",
                    conditions_met=["time_window_active", f"soc <= {target_soc}"],
                    can_execute=False,  # HOLD is a no-op — no hardware command issued
                    baseline_missing=not baseline_ok,
                    trigger_category="time_schedule",
                    dispatch_summary=f"Scheduled Time window active. Discharge target {target_soc}% reached — no override issued, gateway operates normally."
                )
        elif action_intent == "HOLD":
                return EvalDecision(
                    action="HOLD",
                    preset_name=None,
                    rule_id=f"__schedule_{sch.get('id', 'temp')}__",
                    rule_name="Scheduled Hold",
                    priority=5,
                    reason=f"Period={period}, Window={start_t}-{end_t}",
                    conditions_met=["time_window_active"],
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                    trigger_category="time_schedule",
                    dispatch_summary=f"Scheduled Time window active. Enforcing strict hold mode."
                )
    return None
    
def _eval_demand_charge(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 1 — Demand Charge Protection.
    Fires when demand_window=True AND SOC is below min_soc.
    Action: apply force-standby TOU (dispatchId=5 = Backup/Hold) to block grid imports.
    """
    if not ctx.get("demand_window"):
        return None
    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    min_soc = cfg.get("min_soc", 20.0)
    if soc >= min_soc:
        return None  # Plenty of battery capacity — no need to force hold, other rules can export
        
    summary = f"Demand window active — holding battery (SOC {soc:.0f}% < min {min_soc:.0f}%)"
    return EvalDecision(
        action="HOLD",
        preset_name=None,
        rule_id="__demand_charge__",
        rule_name="Demand Charge Protection",
        priority=10,
        reason=f"demand_window=True, soc={soc:.0f}% < min_soc_threshold={min_soc:.0f}%",
        conditions_met=["demand_window=True", f"soc={soc:.0f}% < {min_soc:.0f}%"],
        can_execute=True,
        baseline_missing=False,
        trigger_category="demand_charge",
        dispatch_summary=summary,
        requires_approval=False,
    )


def _eval_negative_export(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 2 — Negative Export / Export Penalty.
    Fires when export price is considered a penalty (grid charges you for exporting).
    Preferred: disable solar via HA entity. Fallback: actionable notification for off-grid.
    allow_auto_offgrid=True enables automatic off-grid (advanced users with UPS only).
    """
    export_c = ctx.get("export_c_kwh")
    export_penalty_is_positive = ctx.get("export_penalty_is_positive", False)
    soc = ctx.get("soc_pct")
    max_soc = cfg.get("max_soc", 90.0)

    if export_c is None:
        return None
        
    is_penalty = (export_c > 0) if export_penalty_is_positive else (export_c < 0)
    if not is_penalty:
        return None
    
    # If battery isn't full, it will absorb the excess solar instead of exporting
    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    export_c = ctx.get("export_c_kwh") or 0.0
    
    condition_str = f"export_c_kwh={export_c:.2f} > 0" if export_penalty_is_positive else f"export_c_kwh={export_c:.2f} < 0"

    if soc < max_soc:
        return EvalDecision(
            action="NONE",
            preset_name=None,
            rule_id="__negative_export_absorbing__",
            rule_name="Negative Export — Battery Absorbing",
            priority=20,
            reason=f"export_c_kwh={export_c:.2f} (penalty threshold), soc={soc:.0f}% < max_soc_threshold={max_soc:.0f}%",
            conditions_met=[condition_str, f"soc < max_soc"],
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
            trigger_category="negative_export_advisory",
            dispatch_summary=f"Export penalty {export_c:.2f}¢/kWh, but battery is absorbing solar (SOC {soc:.0f}% < {max_soc:.0f}%)",
            requires_approval=False,
        )

    solar_entity = cfg.get("solar_curtail_entity")
    allow_offgrid = bool(cfg.get("allow_auto_offgrid", 0))
    enphase_enabled = ctx.get("enphase_enabled", 0)

    if enphase_enabled in (1, 2):
        summary = f"Export penalty {export_c:.2f}¢/kWh — engaging Enphase DPEL curtailment"
        return EvalDecision(
            action="ENPHASE_CURTAIL",
            preset_name=None,
            rule_id="__negative_export_enphase__",
            rule_name="Negative Export — Enphase DPEL",
            priority=20,
            reason=f"export_c_kwh={export_c:.2f} (penalty limit reached), soc={soc:.0f}% >= max_soc={max_soc:.0f}%",
            conditions_met=[condition_str],
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
            trigger_category="negative_export",
            dispatch_summary=summary,
            requires_approval=False,
        )

    if solar_entity:
        # Preferred: disable solar production via HA entity
        summary = f"Export penalty {export_c:.2f}¢/kWh — disabling solar ({solar_entity})"
        return EvalDecision(
            action="HA_ENTITY_CONTROL",
            preset_name=None,
            rule_id="__negative_export_solar__",
            rule_name="Negative Export — Disable Solar",
            priority=20,
            reason=f"export_c_kwh={export_c:.2f} (penalty limit reached), soc={soc:.0f}% >= max_soc={max_soc:.0f}%",
            conditions_met=[condition_str],
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
            trigger_category="negative_export",
            dispatch_summary=summary,
            requires_approval=False,
            ha_entity_action=solar_entity,
            ha_entity_state="off",
        )
    elif allow_offgrid:
        # Advanced: auto off-grid (users who explicitly enabled this)
        summary = f"Export penalty {export_c:.2f}¢/kWh — disconnecting from grid (auto)"
        return EvalDecision(
            action="SET_OFFGRID",
            preset_name=None,
            rule_id="__negative_export_offgrid_auto__",
            rule_name="Negative Export — Auto Off-Grid",
            priority=20,
            reason=f"export_c_kwh={export_c:.2f} (penalty), soc={soc:.0f}% >= max_soc={max_soc:.0f}%, allow_auto_offgrid=True",
            conditions_met=[condition_str, "allow_auto_offgrid=True"],
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
            trigger_category="negative_export",
            dispatch_summary=summary,
            requires_approval=False,
        )
    else:
        # No solar entity, no auto-offgrid — system is already in Self-Consumption which
        # does not export, so this is ADVISORY only. The engine cannot take corrective action.
        # Colour it distinctly from a real dispatch action so the forecast map isn't all red.
        summary = (
            f"Export penalty {export_c:.2f}¢/kWh — SC mode won't export. "
            f"Configure a solar entity or enable off-grid to act."
        )
        return EvalDecision(
            action="ADVISE_CURTAIL_PV",
            preset_name=None,
            rule_id="__negative_export_advisory__",
            rule_name="Negative Export — Advisory (SC Active)",
            priority=20,
            reason=f"export_c_kwh={export_c:.2f} (penalty), soc={soc:.0f}% >= max_soc={max_soc:.0f}%, no solar entity, allow_auto_offgrid=False",
            conditions_met=[condition_str],
            can_execute=False,   # SC already prevents export — no action needed
            baseline_missing=not baseline_ok,
            trigger_category="negative_export_advisory",   # muted colour on forecast map
            dispatch_summary=summary,
            requires_approval=False,   # don't spam notification — it's informational
        )


def _eval_price_spike(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 3 — Price Spike.
    Fires when spike_status IN [spike, potential], OR export_c_kwh >= 100.0, OR import_c_kwh >= 100.0.
    Looks at the price values as positive/negative:
      - Export HIGH (positive credit >= 100.0 c/kWh): Export to grid if SOC > min_soc.
      - Import HIGH (positive cost >= 100.0 c/kWh): Hold/avoid import (protect charge).
    """
    spike = (ctx.get("spike_status") or "none").lower()
    import_c = ctx.get("import_c_kwh") or 0.0
    export_c = ctx.get("export_c_kwh") or 0.0
    
    is_spike_status = spike in ("spike", "potential")
    is_export_high = export_c >= 100.0
    is_import_high = import_c >= 100.0
    
    if not (is_spike_status or is_export_high or is_import_high):
        return None
        
    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    min_soc = cfg.get("min_soc", 20.0)
    min_export = cfg.get("min_export_price", 0.0)
    export_ok = (min_export == 0.0 or export_c >= min_export)

    # 1. Export HIGH (Credit / Earning)
    if (is_export_high or (is_spike_status and export_c > 0)) and export_ok:
        if soc > min_soc:
            summary = f"Price spike export HIGH ({export_c:.2f}¢/kWh) — discharging to grid (SOC {soc:.0f}% > min {min_soc:.0f}%)"
            return EvalDecision(
                action="GRID_EXPORT",
                preset_name=None,
                rule_id="__price_spike_export__",
                rule_name="Price Spike — Export High Earning",
                priority=30,
                reason=f"export_c_kwh={export_c:.2f} >= 100.0 or spike_status={spike}, soc={soc:.0f}% > min_soc={min_soc:.0f}%",
                conditions_met=[f"export_price_spike"],
                can_execute=True,
                baseline_missing=False,
                trigger_category="price_spike",
                dispatch_summary=summary,
                requires_approval=False,
            )

    # 2. Import HIGH (High Cost / Avoid Import)
    if is_import_high or is_spike_status:
        summary = f"Price spike import HIGH ({import_c:.2f}¢/kWh) — holding battery to avoid grid imports"
        return EvalDecision(
            action="HOLD",
            preset_name=None,
            rule_id="__price_spike_hold__",
            rule_name="Price Spike — High Import Hold",
            priority=31,
            reason=f"import_c_kwh={import_c:.2f} >= 100.0 or spike_status={spike}",
            conditions_met=[f"import_price_spike"],
            can_execute=True,
            baseline_missing=False,
            trigger_category="price_spike",
            dispatch_summary=summary,
            requires_approval=False,
        )

    return None


def _eval_export_bonus(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 4 — Export Bonus.
    export_c_kwh <= export_bonus_threshold (negative=earning) AND soc_pct > min_soc.
    Also fires on tariff_period=solarSponge.
    """
    export_c = ctx.get("export_c_kwh") or 0.0
    threshold = cfg.get("export_bonus_threshold", 5.0)
    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    min_soc = cfg.get("min_soc", 20.0)
    tariff_period = (ctx.get("tariff_period") or "").lower()

    solar_sponge = tariff_period == "solarsponge"
    # threshold == 0 means disabled, unless it's solarsponge
    threshold_disabled = (threshold == 0.0)
    bonus_active = not threshold_disabled and export_c >= threshold
    soc_ok = soc > min_soc   # skip if SOC too low

    if not (solar_sponge or bonus_active) or not soc_ok:
        return None

    summary = (
        f"Solar Sponge export bonus — discharging to earn {export_c:.2f}¢/kWh"
        if solar_sponge
        else f"Export bonus {export_c:.2f}¢/kWh ≥ threshold {threshold:.2f}¢ — discharging"
    )
    return EvalDecision(
        action="GRID_EXPORT",
        preset_name=None,
        rule_id="__export_bonus__",
        rule_name="Export Bonus — Discharge to Grid",
        priority=40,
        reason=f"export_c_kwh={export_c:.2f}, export_bonus_threshold={threshold:.2f}, soc={soc:.0f}%, min_soc_threshold={min_soc:.0f}%, solar_sponge={solar_sponge}",
        conditions_met=[f"export_c_kwh={export_c:.2f} >= {threshold:.2f}"],
        can_execute=True,
        baseline_missing=False,
        trigger_category="export_bonus",
        dispatch_summary=summary,
        requires_approval=False,
    )


def _eval_force_export(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 4.5 — Force Export (Manual override / strict floor).
    Triggers export if user set min_export_price AND export_c_kwh <= min_export_price (negative=credit) AND SOC > min_soc.
    """
    min_export_price = cfg.get("min_export_price", 0.0)
    # The floor price must be strictly positive to trigger force export.
    # If the user configured a negative price (e.g. to configure negative export curtailment threshold),
    # it must not trigger force export.
    if min_export_price <= 0.0:
        return None

    export_c = ctx.get("export_c_kwh")
    if export_c is None or export_c < 0.0 or export_c < min_export_price:
        return None   # price is negative or below floor — don't export

    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    min_soc = cfg.get("min_soc", 20.0)
    if soc <= min_soc:
        return None   # battery depleted below reserve — protect SOC floor

    summary = (
        f"Force Export — export_c_kwh {export_c:.2f}¢ ≥ floor {min_export_price:.2f}¢ "
        f"| SOC {soc:.0f}% > min {min_soc:.0f}% — exporting to grid"
    )
    return EvalDecision(
        action="GRID_EXPORT",
        preset_name=None,
        rule_id="__force_export__",
        rule_name="Force Grid Export — Price Floor",
        priority=45,
        reason=(
            f"export_c_kwh={export_c:.2f} >= min_export_price={min_export_price:.2f}, "
            f"soc={soc:.0f}%, min_soc_threshold={min_soc:.0f}%"
        ),
        conditions_met=[
            f"export_c_kwh={export_c:.2f} >= min_export_price={min_export_price:.2f}",
            f"soc={soc:.0f}% > min_soc={min_soc:.0f}%",
        ],
        can_execute=True,
        baseline_missing=False,
        trigger_category="force_export",
        dispatch_summary=summary,
        requires_approval=False,
    )


def _eval_earnings_target(
    ctx: dict, cfg: dict, baseline_ok: bool,
    daily_earnings: float = 0.0,
    monthly_earnings: float = 0.0,
    billing_configured: bool = False,
    snap: Optional[PriceSnapshot] = None,
) -> Optional[EvalDecision]:
    """
    Priority 5 — Earnings Target.
    Only evaluated when billing cycle is configured in Pricing & Billing.
    Fires when daily OR monthly earnings are below target.
    Triggers export if export_c_kwh > 0 and SOC > min_soc.
    """
    if not billing_configured:
        return None

    daily_target   = cfg.get("daily_earnings_target", 0.0)
    monthly_target = cfg.get("monthly_earnings_target", 0.0)

    daily_gap   = max(0.0, daily_target   - daily_earnings)   if daily_target   > 0 else 0.0
    monthly_gap = max(0.0, monthly_target - monthly_earnings) if monthly_target > 0 else 0.0

    if daily_gap <= 0.0 and monthly_gap <= 0.0:
        return None

    export_c = ctx.get("export_c_kwh", 0.0) or 0.0
    soc = ctx.get("soc_pct", 0)
    min_soc = cfg.get("min_soc", 20.0)
    
    # NEW: Ensure export price is strictly >= the user's min_export_price floor!
    min_export = max(0.0, cfg.get("min_export_price", 0.0))

    if export_c < min_export or (soc is not None and soc <= min_soc):
        return None   # can't earn right now — wait for better conditions

    # NEW: Wait for the maximum/optimal price slot if there is a significantly higher price coming up
    if snap and snap.forecast:
        max_forecast_export = max((p.export_c_kwh or 0.0 for p in snap.forecast), default=0.0)
        if export_c < max_forecast_export * 0.95 and max_forecast_export > min_export:
            return None   # wait for the highest price slot to maximize revenue

    gap_desc = []
    if daily_gap > 0:
        gap_desc.append(f"daily gap ${daily_gap:.2f}")
    if monthly_gap > 0:
        gap_desc.append(f"monthly gap ${monthly_gap:.2f}")
    summary = f"Earnings target behind ({', '.join(gap_desc)}) — exporting at {export_c:.2f}¢/kWh (≥ floor {min_export:.2f}¢)"

    return EvalDecision(
        action="GRID_EXPORT",
        preset_name=None,
        rule_id="__earnings_target__",
        rule_name="Earnings Target — Export to Close Gap",
        priority=50,
        reason=f"daily_gap=${daily_gap:.2f}, monthly_gap=${monthly_gap:.2f}, export_c_kwh={export_c:.2f}, min_export_price={min_export:.2f}",
        conditions_met=[f"earnings_gap > 0", f"export_c_kwh={export_c:.2f} >= min_export_price={min_export:.2f}"],
        can_execute=True,
        baseline_missing=False,
        trigger_category="earnings_target",
        dispatch_summary=summary,
        requires_approval=False,
    )


def _eval_force_charge(ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
    """
    Priority 6 — Force Charge (Cheap/Negative Import).
    descriptor IN [negative, extremelyLow] AND soc_pct < max_soc.
    """
    descriptor = (ctx.get("descriptor") or "neutral").lower()
    import_c = ctx.get("import_c_kwh", 0.0)
    if import_c is None:
        import_c = 0.0
    max_charge_price = cfg.get("max_charge_price", 0.0)
    
    # NEW: Cease charging if price is too high, regardless of descriptor
    if max_charge_price > 0 and import_c > max_charge_price:
        return EvalDecision(
            action="HOLD",
            preset_name=None,
            rule_id="__cease_charge_limit__",
            rule_name="Max Price — Cease Charging",
            priority=60,
            reason=f"import_c_kwh={import_c:.2f} > max_charge_price={max_charge_price:.2f}",
            conditions_met=[f"import_c_kwh > max_charge_price"],
            can_execute=True,
            baseline_missing=False,
            trigger_category="price_spike",
            dispatch_summary=f"Import price {import_c:.2f}¢/kWh exceeds max limit ({max_charge_price:.2f}¢/kWh) — holding charge",
            requires_approval=False,
        )

    if descriptor not in ("negative", "extremelylow"):
        return None
    soc = ctx.get("soc_pct")
    if soc is None:
        soc = 0.0
    max_soc = cfg.get("max_soc", 90.0)
    if soc >= max_soc:
        return None   # already full — no point charging

    # Predictive Charge Optimization: Check if Solar will fill the gap
    solar_remaining_kwh = ctx.get("solar_forecast_remaining_kwh", 0.0)
    if solar_remaining_kwh > 0.0 and soc is not None:
        # Approximate capacity if not in context (standard FranklinWH aGate is 13.6 kWh usable)
        capacity_kwh = ctx.get("battery_capacity_kwh", 13.6)
        required_kwh = ((max_soc - soc) / 100.0) * capacity_kwh
        
        # If solar forecast is 120% of required energy, cancel the grid charge
        if solar_remaining_kwh >= required_kwh * 1.2:
            return EvalDecision(
                action="HOLD",
                preset_name=None,
                rule_id="__predictive_solar_hold__",
                rule_name="Predictive Solar Optimization",
                priority=61,
                reason=f"solar_remaining={solar_remaining_kwh:.2f}kWh > required={required_kwh:.2f}kWh",
                conditions_met=["solar_forecast_sufficient=true"],
                can_execute=True,
                baseline_missing=False,
                trigger_category="solar_optimization",
                dispatch_summary=f"Canceling grid charge: Solar forecast ({solar_remaining_kwh:.2f} kWh) is sufficient to fill the {required_kwh:.2f} kWh gap today",
                requires_approval=False,
            )

    summary = f"Cheap import {import_c:.2f}¢/kWh ({descriptor}) — force charging battery"
    return EvalDecision(
        action="GRID_CHARGE",
        preset_name=None,
        rule_id="__force_charge__",
        rule_name="Force Charge — Cheap Window",
        priority=65,
        reason=f"descriptor={descriptor}, import_c_kwh={import_c:.2f}, soc={soc}%, max_soc={max_soc}%",
        conditions_met=[f"descriptor={descriptor}"],
        can_execute=True,
        baseline_missing=False,
        trigger_category="force_charge",
        dispatch_summary=summary,
        requires_approval=False,
    )


# ── Self-Consumption Fallback TOU Payload ────────────────────────────────────
# Per TOU Schedule Guide: Custom TOU must specify default_mode explicitly.
# Without it the hardware may revert to an unexpected default.
SC_FALLBACK_TOU = {
    "schedule": [],          # empty = use device default schedule
    "operation": 0,
    "default_mode": "SELF",  # explicit Self-Consumption
    "default_tariff": "OFF_PEAK",
}


# ── Engine ───────────────────────────────────────────────────────────────────


async def _call_ha_service(domain: str, service: str, payload: dict) -> bool:
    ha_host, ha_token = await _get_ha_credentials()
    if not ha_host or not ha_token:
        logger.error("SmartDispatch: Cannot call HA service, missing HA credentials.")
        return False
    url = f"{ha_host.rstrip('/')}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type":  "application/json",
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code in (200, 201):
            return True
        else:
            logger.error(f"SmartDispatch: HA service {domain}.{service} failed with {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as exc:
        logger.error(f"SmartDispatch: Exception calling HA service {domain}.{service} - {exc}")
        return False


def _calc_dispatch_payload(
    action: str,
    snap: "PriceSnapshot",
    last_data: dict,
    cfg: dict,
) -> dict:
    """Calculate SD-enriched action parameters for the sd_signal payload.

    Returns a dict with:
        power_kw      – net available inverter kW (nameplate − home_load [− solar for charge])
        duration_mins – consecutive minutes in the forecast where the price condition holds
        target_soc    – SOC limit from SD config (max_soc for GRID_CHARGE, min_soc for GRID_EXPORT)
        calc_basis    – human-readable derivation string for transparency / audit

    Called only for GRID_CHARGE and GRID_EXPORT; returns an empty dict for NONE/HOLD/PAUSED.
    """
    if action not in ("GRID_CHARGE", "GRID_EXPORT"):
        return {}

    # ── Power calculation ────────────────────────────────────────────────────
    cap = last_data.get("capacity", {}) if last_data else {}
    home_kw  = float(last_data.get("home_kw",  0.0) or 0.0) if last_data else 0.0
    solar_kw = float(last_data.get("solar_kw", 0.0) or 0.0) if last_data else 0.0

    if action == "GRID_EXPORT":
        nameplate_kw = float(cap.get("max_discharge_kw") or 5.0)
        # Export: battery → grid. Home load competes with export capacity.
        # Solar during export goes directly to home/grid — not deducted from battery.
        net_kw = max(0.5, nameplate_kw - home_kw)
        target_soc = int(cfg.get("min_soc", 20))
        power_basis = f"nameplate={nameplate_kw:.1f} home={home_kw:.1f}"
    else:  # GRID_CHARGE
        nameplate_kw = float(cap.get("max_charge_kw") or 5.0)
        # Charge: grid → battery. Solar already filling battery — reduce grid draw.
        net_kw = max(0.5, nameplate_kw - home_kw - solar_kw)
        target_soc = int(cfg.get("max_soc", 90))
        power_basis = f"nameplate={nameplate_kw:.1f} home={home_kw:.1f} solar={solar_kw:.1f}"

    power_kw = round(min(nameplate_kw, net_kw), 2)

    # ── Duration calculation from price window forecast ──────────────────────
    now_import_threshold = float(cfg.get("max_charge_price", 15.0))
    now_export_threshold = float(cfg.get("min_export_price", 5.0))
    interval_min = int(snap.interval_min or 30)
    duration_mins = 0
    window_end: str = ""

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    # Walk forecast forward, counting consecutive intervals where price holds
    for period in sorted(snap.forecast, key=lambda p: p.start):
        if period.start < now:
            continue  # skip past intervals
        if action == "GRID_EXPORT":
            price = period.export_c_kwh
            holds = (price is not None) and (price >= now_export_threshold)
        else:  # GRID_CHARGE
            holds = period.import_c_kwh <= now_import_threshold

        if holds:
            duration_mins += interval_min
            window_end = period.start.strftime("%H:%M")
        else:
            break  # window closed — stop counting

    # Minimum 1 interval; cap at 4 hours
    duration_mins = max(interval_min, min(duration_mins, 240))

    calc_basis = (
        f"{power_basis} → {power_kw:.1f}kW | "
        f"window={interval_min}min×{duration_mins//interval_min if interval_min else 1}intv "
        f"({duration_mins}min) "
        f"thr={'≥' if action == 'GRID_EXPORT' else '≤'}"
        f"{now_export_threshold if action == 'GRID_EXPORT' else now_import_threshold:.2f}¢"
    )
    if window_end:
        calc_basis += f" ends≈{window_end}"

    return {
        "power_kw":     power_kw,
        "duration_mins": duration_mins,
        "target_soc":   target_soc,
        "calc_basis":   calc_basis,
    }


class StrategyMixer:
    """
    Orchestrates the evaluation of the sd_strategy_matrix.
    Combines Site DNA, Price Signals, and Strategy Rows into a final Dispatch Intent.
    """

    def __init__(self, registry=None):
        self._registry = registry

    # ── Per-conflict dedup for the WARNING log at :_check_and_resolve_conflicts ──
    # Class-level (not instance) because `SmartDispatchEngine` builds a
    # fresh `StrategyMixer(...)` per evaluation call — instance state
    # would reset every tick and the dedup would be useless. The set of
    # rule conflicts is a function of the rulebook (DB), not tick state,
    # so re-logging every 30 s MicroTicker fire is pure noise. Cache the
    # last-emit unix ts per (short_id, local_id, global_id, reason) and
    # only re-emit after LOG_CONFLICT_INTERVAL_SECS.
    _conflict_last_logged: dict[tuple, float] = {}
    LOG_CONFLICT_INTERVAL_SECS = 3600  # re-log same conflict at most once/hour

    async def evaluate(
        self, 
        full_serial: str, 
        snap: PriceSnapshot, 
        soc_pct: float,
        site_snap: Optional[dict] = None
    ) -> Optional[EvalDecision]:
        # 1. Resolve Site DNA
        dna = await db.get_gateway_by_full_serial(full_serial)
        if not dna:
            # Fallback: try short_id lookup
            dna = await db.get_gateway(full_serial)
            
        if not dna:
            logger.warning(f"StrategyMixer: gateway {full_serial} not found in DB — skipping")
            return None

        # 2. Fetch Config & Matrix Rows
        short_id = dna.get("short_id")
        cfg = await db.get_smart_dispatch_config(short_id)
        raw_rows = await db.get_sd_strategy_matrix(short_id)
        
        # Check overlaps and resolve conflicts enforcing local precedence
        rows = self._check_and_resolve_conflicts(raw_rows, short_id)
        
        # 3. Build Context (Site DNA + Pricing + Config)
        ctx = await _build_context(snap, soc_pct, dna, cfg, site_snap=site_snap)
        
        if not rows:
            return None
            
        baseline_ok = True # TODO: refined check
        
        # 4. Evaluate Rows in Order
        matched_decisions = []
        for row in rows:
            if not row.get("enabled", 1):
                continue
            
            res = await self._eval_row(row, ctx, cfg, baseline_ok)
            if res:
                matched_decisions.append(res)

        if not matched_decisions:
            return None

        # Sort matched decisions by priority (eval_order)
        matched_decisions.sort(key=lambda x: x.priority)
        winner = matched_decisions[0]

        shadowed_rules = []
        for shadowed in matched_decisions[1:]:
            # Rate-limit: shadow relationships are a function of the
            # rulebook, not tick state — re-logging every 30 s MicroTicker
            # fire is noise. Reuses the same class-level dedup as the
            # conflict warning (below). Downgraded to DEBUG on the
            # rate-limited path so the shadow entry still lands in
            # `automation_notification_log` for audit but doesn't clutter
            # the app log at INFO.
            import time as _t
            _skey = ("shadow", full_serial, shadowed.rule_id, winner.rule_id)
            _snow = _t.time()
            if _snow - StrategyMixer._conflict_last_logged.get(_skey, 0.0) >= StrategyMixer.LOG_CONFLICT_INTERVAL_SECS:
                StrategyMixer._conflict_last_logged[_skey] = _snow
                logger.info(f"StrategyMixer [{full_serial}]: Rule '{shadowed.rule_name}' (P{shadowed.priority}) shadowed by '{winner.rule_name}' (P{winner.priority})")
            shadowed_rules.append({
                "rule_name": shadowed.rule_name,
                "action": shadowed.action,
                "priority": shadowed.priority,
                "reason": shadowed.reason,
                "trigger_category": shadowed.trigger_category
            })

        if shadowed_rules:
            if not winner.action_payload:
                winner.action_payload = {}
            winner.action_payload["_shadowed_rules"] = shadowed_rules

        return winner

    def _check_and_resolve_conflicts(self, rows: list[dict], short_id: str) -> list[dict]:
        """
        Detects conflicts between Global (gateway_id='all') and Gateway-Specific rows.
        Flags overlaps and enforces Gateway-Specific precedence.
        """
        local_rows = [r for r in rows if r.get("gateway_id") == short_id]
        global_rows = [r for r in rows if r.get("gateway_id") == "all"]

        for l_row in local_rows:
            if not l_row.get("enabled", 1):
                continue
            for g_row in global_rows:
                if not g_row.get("enabled", 1):
                    continue
                # Overlap conditions:
                # 1. Exact same eval_order (priority conflict)
                # 2. Or same trigger_category with potentially overlapping conditions
                has_conflict = False
                reason = ""
                if l_row.get("eval_order") == g_row.get("eval_order"):
                    has_conflict = True
                    reason = f"identical eval_order ({l_row.get('eval_order')})"
                elif l_row.get("trigger_category") == g_row.get("trigger_category"):
                    # check if conditions_json has overlapping keys
                    try:
                        l_cond = json.loads(l_row.get("conditions_json", "{}"))
                        g_cond = json.loads(g_row.get("conditions_json", "{}"))
                        common_keys = set(l_cond.keys()) & set(g_cond.keys())
                        if common_keys:
                            has_conflict = True
                            reason = f"overlapping condition keys {list(common_keys)} in category '{l_row.get('trigger_category')}'"
                    except Exception:
                        pass
                
                if has_conflict:
                    # Rate-limit: same conflict logs at most once per hour
                    # per gateway. See __init__ note — this used to fire
                    # on every 30 s MicroTicker fire (60 warnings / 10 min
                    # on gateway 99900001 alone).
                    import time as _t
                    _key = (short_id, l_row.get("id"), g_row.get("id"), reason)
                    _now = _t.time()
                    if _now - StrategyMixer._conflict_last_logged.get(_key, 0.0) >= StrategyMixer.LOG_CONFLICT_INTERVAL_SECS:
                        StrategyMixer._conflict_last_logged[_key] = _now
                        logger.warning(
                            f"[SmartDispatch] Conflict detected for gateway '{short_id}': "
                            f"Global row {g_row.get('id')} ('{g_row.get('strategy_name')}') "
                            f"overlaps with Gateway-Specific row {l_row.get('id')} ('{l_row.get('strategy_name')}') "
                            f"due to {reason}. Gateway-Specific rule takes precedence."
                        )

        # Sort: priority (eval_order) first, then local (0) before global (1)
        sorted_rows = sorted(
            rows,
            key=lambda r: (r.get("eval_order", 100), 0 if r.get("gateway_id") == short_id else 1)
        )
        return sorted_rows

    async def _eval_row(self, row: dict, ctx: dict, cfg: dict, baseline_ok: bool) -> Optional[EvalDecision]:
        """Evaluate a single strategy row against the context."""
        cond_json = row.get("conditions_json", "{}")
        matched, descriptions = evaluate_condition(cond_json, ctx)
        
        if not matched:
            return None
            
        # Parse signals
        try:
            signals = json.loads(row.get("signals_json", "[]"))
        except Exception:
            logger.error(f"StrategyMixer: malformed signals JSON in row {row.get('id')}")
            return None
            
        if not signals:
            return None
            
        # 1. Strategy Matrix logic (first signal wins for now)
        sig = signals[0]
        action = sig.get("action", "HOLD")
        
        # Apply forecast weight if applicable
        weight = row.get("forecast_weight", 1.0)
        if weight < 1.0:
            # TODO: probabilistic or threshold-based suppression?
            # For now, just logging it.
            logger.debug(f"StrategyMixer: row {row.get('id')} has weight {weight}")

        duration = row.get("intent_duration_mins") or 5
        
        # Build EvalDecision
        decision = EvalDecision(
            action=action,
            preset_name=sig.get("preset"),
            rule_id=f"matrix_{row.get('id')}",
            rule_name=row.get("strategy_name", "Unnamed Strategy"),
            priority=row.get("eval_order", 100),
            reason=f"Matrix match: {descriptions[0]}",
            conditions_met=descriptions,
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
            trigger_category=row.get("trigger_category", "custom"),
            dispatch_summary=f"Strategy Matrix: {row.get('strategy_name')}",
            requires_approval=False,
            action_payload={
                "duration_mins": duration,
                "weight": weight
            }
        )
        return decision