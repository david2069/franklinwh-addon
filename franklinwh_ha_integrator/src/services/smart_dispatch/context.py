"""Evaluation context builders.

Extracted from `smart_dispatch/__init__.py` in v0.2.3 (Phase 1 Stage C,
2026-08-05). `_build_context` was a 220-line god function mixing
pricing, SOC, solar forecast, HA load reads, site telemetry, and
season into a single flat dict for the rule evaluator; `_build_evaluated_params`
is the small shim that formats those constraints for the UI overview
panel. Both are pure over their inputs — no engine state, no coupling
to `SmartDispatchEngine`. Re-exported from `__init__.py` under their
original leading-underscore names so internal callers keep working."""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.services import db
from src.services.pricing.base import PriceSnapshot
from src.services.smart_dispatch.loads import get_site_season, is_schedule_active

logger = logging.getLogger(__name__)


async def _build_context(
    snap: PriceSnapshot,
    soc_pct: Optional[float],
    dna: Optional[dict] = None,
    cfg: dict = {},
    site_snap: Optional[dict] = None,
    loads: Optional[list] = None,
    enphase_enabled: Optional[int] = None,
) -> dict[str, Any]:
    """Build the flat context dict passed to the condition evaluator.

    Uses the current (live) interval from the snapshot. Falls back to
    the snapshot-level fields if no current interval is identifiable."""
    # Prefer first forecast period flagged is_current; fall back to snapshot level
    current_period = next(
        (p for p in snap.forecast if getattr(p, "is_current", False)),
        None,
    )

    if current_period:
        descriptor    = getattr(current_period, "descriptor",    "neutral")
        spike_status  = getattr(current_period, "spike_status",  "none")
        tariff_period = getattr(current_period, "tariff_period", None)
        demand_window = getattr(current_period, "demand_window", False)
        renewables    = current_period.renewables_pct
    else:
        # Fall back to snapshot-level (PriceSnapshot carries spike_status directly)
        descriptor    = "neutral"
        spike_status  = (snap.spike_status or "NONE").lower()
        tariff_period = None
        demand_window = snap.demand_window
        renewables    = snap.renewables_pct

    # Attempt to fetch cached solar forecast
    try:
        from src.services.forecast_solar import manager as solar_manager
        solar_remaining = solar_manager.cached_forecast.get("forecast_kwh", 0.0)
    except Exception:
        solar_remaining = 0.0

    # Fetch Enphase enablement dynamically for the rules engine.
    # The caller (e.g. the SD forecast endpoint, which loops over many
    # periods) may pass enphase_enabled in to avoid 48 redundant DB hits
    # — one per period.
    if enphase_enabled is None:
        enphase_enabled = 0
        try:
            solar_cfg = await db.get_solar_forecast_config()
            enphase_enabled = solar_cfg.get("enphase_enabled", 0)
        except Exception:
            pass

    ctx = {
        "import_c_kwh":  snap.import_c_kwh,
        "export_c_kwh":  snap.export_c_kwh,
        "export_penalty_is_positive": snap.export_penalty_is_positive,
        "tariff_type":   snap.tariff_type,         # OFFPEAK | SHOULDER | PEAK | SPIKE
        "descriptor":    descriptor,                # negative | extremelyLow | … | spike
        "spike_status":  spike_status,              # none | potential | spike
        "tariff_period": tariff_period,             # offPeak | shoulder | solarSponge | peak | None
        "demand_window": demand_window,             # bool
        "renewables_pct": renewables,               # int or None
        "soc_pct":       soc_pct,                   # float or None (None = unknown)
        "solar_forecast_remaining_kwh": solar_remaining,
        "enphase_enabled": enphase_enabled,
        # SD Configuration Parameters (Integration)
        "cfg_min_soc":              cfg.get("min_soc", 20.0),
        "cfg_max_soc":              cfg.get("max_soc", 90.0),
        "cfg_max_charge_price":     cfg.get("max_charge_price", 0.0),
        "cfg_min_export_price":     cfg.get("min_export_price", 0.0),
        "cfg_export_bonus":         cfg.get("export_bonus_threshold", 5.0),
        "cfg_solar_curtail_entity": cfg.get("solar_curtail_entity", ""),
        "cfg_allow_auto_offgrid":   bool(cfg.get("allow_auto_offgrid", 0)),
    }

    if dna:
        # Flattened DNA fields for the evaluator
        ctx.update({
            "electricity_type": dna.get("electricity_type"),
            "grid_feed_max": dna.get("grid_feed_max"),
            "grid_max": dna.get("grid_max"),
            "not_control_export_solar": bool(dna.get("not_control_export_solar")),
            "is_three_phase": bool(dna.get("has_three_phase")),
        })

    # Inject Site-Level Telemetry (Virtual Site Meter)
    if site_snap:
        ctx["site"] = {
            "p_fhp":    site_snap.get("p_fhp", 0.0),
            "p_uti":    site_snap.get("p_uti", 0.0),
            "p_sun":    site_snap.get("p_sun", 0.0),
            "p_ld":     site_snap.get("p_ld", 0.0),
            "soc_avg":  site_snap.get("soc_avg", 0.0),
            "soc_min":  site_snap.get("soc_min", 0.0),
            "soc_max":  site_snap.get("soc_max", 0.0),
            "gw_count": site_snap.get("count", 0),
        }
        # Flat aliases for Strategy Matrix (until UI fully transitioned)
        ctx.update({
            "site_p_fhp":    site_snap.get("p_fhp", 0.0),
            "site_p_uti":    site_snap.get("p_uti", 0.0),
            "site_p_sun":    site_snap.get("p_sun", 0.0),
            "site_p_ld":     site_snap.get("p_ld", 0.0),
            "site_soc_avg":  site_snap.get("soc_avg", 0.0),
            "site_soc_min":  site_snap.get("soc_min", 0.0),
            "site_soc_max":  site_snap.get("soc_max", 0.0),
            "site_gw_count": site_snap.get("count", 0),
        })

    # Inject custom loads under loads.<slug>
    if loads is None:
        try:
            loads = await db.get_all_forecast_loads()
        except Exception:
            loads = []

    import re
    import datetime as _datetime

    current_season = "Summer"
    if lat_val := (cfg.get("lat") or (site_snap or {}).get("lat")):
        try:
            current_season = get_site_season(float(lat_val), _datetime.datetime.now().month)
        except Exception:
            pass

    target_time = _datetime.datetime.now()

    ha_states = {}
    has_now_loads = any(load.get("enabled", 1) and load.get("measurement_type") == "now" for load in (loads or []))
    if has_now_loads:
        from src.routes.api_ha import get_ha_state
        for load in (loads or []):
            if load.get("enabled", 1) and load.get("measurement_type") == "now":
                for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id", "ha_energy_entity_id"]:
                    ent_id = load.get(key)
                    if ent_id and ent_id not in ha_states:
                        try:
                            st = await get_ha_state(ent_id)
                            if st and "state" in st:
                                ha_states[ent_id] = st["state"]
                        except Exception:
                            pass

    ctx_loads = {}
    for load in (loads or []):
        raw_name = load.get("name") or f"load_{load['id']}"
        slug = re.sub(r'[^a-z0-9_]', '', raw_name.lower().replace(" ", "_"))
        if not slug:
            slug = f"load_{load['id']}"

        enabled = bool(load.get("enabled", 1))
        avg_kw = float(load.get("avg_kw", 0.0))
        m_type = load.get("measurement_type", "forecast")

        power_kw = 0.0
        is_active = False
        energy_kwh = 0.0

        if enabled:
            if m_type == "now":
                resolved = False
                power_entity = load.get("ha_entity_id")
                if power_entity and power_entity in ha_states:
                    state = ha_states[power_entity]
                    try:
                        power_kw = float(state)
                        resolved = True
                        if power_kw > 0.0:
                            is_active = True
                    except ValueError:
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True

                if not resolved:
                    switch_entity = load.get("ha_switch_entity_id")
                    if switch_entity and switch_entity in ha_states:
                        state = ha_states[switch_entity]
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True

                if not resolved:
                    binary_entity = load.get("ha_binary_entity_id")
                    if binary_entity and binary_entity in ha_states:
                        state = ha_states[binary_entity]
                        if str(state).lower() in ("on", "true", "running", "active", "charging"):
                            power_kw = avg_kw
                            is_active = True
                            resolved = True
            else:
                schedule_json = load.get("schedule_json", "[]")
                try:
                    import json
                    periods = json.loads(schedule_json)
                except Exception:
                    periods = []

                for p in periods:
                    if is_schedule_active(p, target_time, current_season):
                        is_active = True
                        break
                if is_active:
                    power_kw = avg_kw

            energy_entity = load.get("ha_energy_entity_id")
            if energy_entity and energy_entity in ha_states:
                try:
                    energy_kwh = float(ha_states[energy_entity])
                except ValueError:
                    pass

        ctx_loads[slug] = {
            "enabled": enabled,
            "active": is_active,
            "power_kw": power_kw,
            "energy_kwh": energy_kwh,
        }

    ctx["loads"] = ctx_loads
    return ctx


def _build_evaluated_params(ctx: dict, cfg: dict) -> list[dict]:
    """Helper to structure evaluated constraints for the frontend overview UI."""
    params = []
    soc = ctx.get("soc_pct") or 0.0
    import_c = ctx.get("import_c_kwh") or 0.0
    export_c = ctx.get("export_c_kwh") or 0.0

    # 1. Target Min SOC
    min_soc = cfg.get("min_soc", 20.0)
    status = "passed" if soc >= min_soc else "failed"
    desc = f"Current SOC ({soc:.0f}%) {'≥' if soc >= min_soc else '<'} Minimum ({min_soc:.0f}%)"
    params.append({"param": "Target Min SOC", "value": f"{min_soc}%", "status": status, "desc": desc, "icon": "fa-battery-empty"})

    # 2. Max Charge Price
    max_charge = cfg.get("max_charge_price", 0.0)
    if max_charge > 0:
        status = "passed" if import_c <= max_charge else "failed"
        desc = f"Import price ({import_c:.1f}¢) {'≤' if import_c <= max_charge else '>'} Max Charge ({max_charge:.1f}¢)"
        params.append({"param": "Max Charge Price", "value": f"{max_charge}¢", "status": status, "desc": desc, "icon": "fa-tag"})

    # 3. Min Export Price
    min_export = cfg.get("min_export_price", 0.0)
    if min_export != 0.0:
        status = "passed" if export_c >= min_export else "failed"
        desc = f"Export price ({export_c:.1f}¢) {'≥' if export_c >= min_export else '<'} Min Export Floor ({min_export:.1f}¢)"
        params.append({"param": "Min Export Price", "value": f"{min_export}¢", "status": status, "desc": desc, "icon": "fa-hand-holding-dollar"})

    # 4. Target Max SOC
    max_soc = cfg.get("max_soc", 90.0)
    status = "passed" if soc <= max_soc else "failed"
    desc = f"Current SOC ({soc:.0f}%) {'≤' if soc <= max_soc else '>'} Maximum ({max_soc:.0f}%)"
    params.append({"param": "Target Max SOC", "value": f"{max_soc}%", "status": status, "desc": desc, "icon": "fa-battery-full"})

    return params
