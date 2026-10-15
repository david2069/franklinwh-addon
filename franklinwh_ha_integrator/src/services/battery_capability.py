"""What the attached aPowers can actually do, summed across the fleet.

Power limits were constants. `_rated_power_per_unit = 5.0` and
`_rated_kwh_per_unit = 13.6` in gateway_service, `RATED_KWH_PER_APOWER = 13.6`
in the optimiser, `dispatchPowerKw: 2.0` against a hardcoded 5 kW slider in the
Control tab, and `upsert_battery(..., 5.0, 13.6, ...)` writing those same
literals into the batteries table. Every one of them is an aPower X spec
applied to whatever is installed:

    aPower X   5.0 kW   13.6 kWh
    aPower 2  10.0 kW   15.0 kWh
    aPower S  11.5 kW   15.0 kWh

so a site with two aPower 2 units had 20 kW of inverter reported as 10, a
Control slider that stopped at 5, and an optimiser sizing its charge target
against 27.2 kWh of battery it thought was 13.6.

**Why the registry rather than the API.** The cloud returns `ratedPower` and
`ratedCapacity` per unit, which covers the simple case. What it does not return
is capability *per mode* — an aPower S discharges at 11.5 kW, accepts 5 kW of
AC-coupled solar, and takes 15 kW of DC PV across four MPPTs. Those three
numbers are properties of the model, not of anything the API reports, which is
what the device registry exists to supply.

Mixed fleets are real: per FranklinWH's compatibility statement an aGate X
1.3/1.3.1 supports aPower X, aPower 2 and aPower S together, up to fifteen
units. So this sums per unit rather than multiplying one rating by a count.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Used only when a unit resolves to nothing at all. It is the oldest and
# smallest aPower, so an unknown unit under-promises rather than over-promises
# — a slider that stops short is a nuisance, one that offers power the
# hardware cannot deliver is a support case.
FALLBACK_KW = 5.0
FALLBACK_KWH = 13.6

SOURCE_REGISTRY = "registry"   # model matched in the device catalog
SOURCE_REPORTED = "reported"   # cloud's per-unit ratedPower/ratedCapacity
SOURCE_FALLBACK = "fallback"   # neither — assumed aPower X


@dataclass
class UnitCapability:
    serial: str
    model: str
    sku: str
    continuous_kw: float
    peak_kw: float
    usable_kwh: float
    ac_solar_kw: float
    dc_pv_kw: float
    mppt_count: int
    source: str


@dataclass
class FleetCapability:
    unit_count: int = 0
    models: list[str] = field(default_factory=list)
    mixed: bool = False
    usable_kwh: float = 0.0
    continuous_kw: float = 0.0
    peak_kw: float = 0.0
    ac_solar_kw: float = 0.0
    dc_pv_kw: float = 0.0
    mppt_count: int = 0
    sources: list[str] = field(default_factory=list)
    units: list[UnitCapability] = field(default_factory=list)

    @property
    def has_dc_pv(self) -> bool:
        """True when any unit is a hybrid — an aPower S takes panels directly,
        so PV can arrive at the battery rather than only via an inverter."""
        return self.dc_pv_kw > 0

    @property
    def resolved_from_registry(self) -> bool:
        return bool(self.sources) and all(s == SOURCE_REGISTRY for s in self.sources)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["has_dc_pv"] = self.has_dc_pv
        d["resolved_from_registry"] = self.resolved_from_registry
        return d


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_unit(unit: dict, catalog_models: list[dict]) -> UnitCapability:
    """One aPower's capability.

    `pe_hw_ver` is the cloud's peHwVersion and the catalog's hw_version_int —
    the only field that identifies which aPower a serial actually is.
    """
    serial = str(unit.get("serial") or unit.get("full_serial") or "")
    hw = _as_int(unit.get("pe_hw_ver"))

    row = None
    if hw is not None:
        row = next(
            (m for m in catalog_models
             if m.get("device_class") == "apower"
             and _as_int(m.get("hw_version_int")) == hw),
            None,
        )

    if row:
        return UnitCapability(
            serial=serial,
            model=row.get("model") or row.get("name") or "aPower",
            sku=row.get("sku") or "",
            continuous_kw=_as_float(row.get("nominal_kw"), FALLBACK_KW),
            peak_kw=_as_float(row.get("peak_kw")),
            usable_kwh=_as_float(row.get("rated_kwh"), FALLBACK_KWH),
            ac_solar_kw=_as_float(row.get("ac_solar_max_kw")),
            dc_pv_kw=_as_float(row.get("mppt_max_kw")),
            mppt_count=_as_int(row.get("mppt_count")) or 0,
            source=SOURCE_REGISTRY,
        )

    # The cloud's own numbers cover power and capacity but say nothing about
    # AC-solar or MPPT limits, so those stay zero rather than being guessed.
    reported_kw = _as_float(unit.get("rated_kw"))
    reported_kwh = _as_float(unit.get("rated_kwh"))
    if reported_kw or reported_kwh:
        return UnitCapability(
            serial=serial, model="aPower (unrecognised)", sku="",
            continuous_kw=reported_kw or FALLBACK_KW,
            peak_kw=0.0,
            usable_kwh=reported_kwh or FALLBACK_KWH,
            ac_solar_kw=0.0, dc_pv_kw=0.0, mppt_count=0,
            source=SOURCE_REPORTED,
        )

    logger.debug("aPower %s resolved to nothing — assuming aPower X", serial or "?")
    return UnitCapability(
        serial=serial, model="aPower (assumed X)", sku="",
        continuous_kw=FALLBACK_KW, peak_kw=0.0, usable_kwh=FALLBACK_KWH,
        ac_solar_kw=0.0, dc_pv_kw=0.0, mppt_count=0,
        source=SOURCE_FALLBACK,
    )


def fleet_capability(units: list[dict], catalog_models: list[dict]) -> FleetCapability:
    """Sum across every attached aPower.

    Summed, not multiplied: an aGate X 1.3 can carry aPower X, 2 and S at the
    same time, so there is no single per-unit rating to multiply by a count.
    """
    fleet = FleetCapability()
    if not units:
        return fleet

    for unit in units:
        if not isinstance(unit, dict):
            continue
        cap = resolve_unit(unit, catalog_models or [])
        fleet.units.append(cap)
        fleet.unit_count += 1
        fleet.usable_kwh += cap.usable_kwh
        fleet.continuous_kw += cap.continuous_kw
        fleet.peak_kw += cap.peak_kw
        fleet.ac_solar_kw += cap.ac_solar_kw
        fleet.dc_pv_kw += cap.dc_pv_kw
        fleet.mppt_count += cap.mppt_count
        fleet.sources.append(cap.source)
        if cap.model not in fleet.models:
            fleet.models.append(cap.model)

    fleet.mixed = len(fleet.models) > 1
    for attr in ("usable_kwh", "continuous_kw", "peak_kw", "ac_solar_kw", "dc_pv_kw"):
        setattr(fleet, attr, round(getattr(fleet, attr), 2))
    return fleet


def load_catalog_models() -> list[dict]:
    """aPower rows from the seeded device catalog."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "db" / "seed" / "device_catalog_seed.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        logger.warning("device catalog unreadable — power limits fall back to aPower X")
        return []
    models = data.get("device_models") or []
    return list(models.values()) if isinstance(models, dict) else list(models)
