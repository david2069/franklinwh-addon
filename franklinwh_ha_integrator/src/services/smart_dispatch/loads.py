"""Site season + Home Loads context builders.

Extracted from `smart_dispatch/__init__.py` in v0.2.3 (Phase 1 Stage B,
2026-08-05). Everything here is pure over its inputs — no engine state,
no coupling to `SmartDispatchEngine`. External consumers reach these
via `from src.services.smart_dispatch import get_site_season, ...` (see
mqtt_publisher.py, api_smart_dispatch.py, scheduler_core.py); the
package `__init__.py` re-exports them so no import site changes."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from src.services import db

logger = logging.getLogger(__name__)


# ── Seasonal & Schedule Context ──────────────────────────────────────────────

def get_site_season(lat: float, month: int) -> str:
    """Determine meteorological season based on latitude and month.
    Returns: 'summer', 'autumn', 'winter', or 'spring'."""
    is_northern = lat >= 0
    if month in (12, 1, 2):
        return "winter" if is_northern else "summer"
    elif month in (3, 4, 5):
        return "spring" if is_northern else "autumn"
    elif month in (6, 7, 8):
        return "summer" if is_northern else "winter"
    else:  # 9, 10, 11
        return "autumn" if is_northern else "spring"


async def resolve_site_season_for_gateway(
    gateway_id: str,
    registry=None,
    month: Optional[int] = None,
) -> str:
    """Resolve the current site season for a gateway, caching its latitude
    under `app_config[lat_{gateway_id}]`. Returns '' if the latitude
    cannot be determined. Shared by the dispatch engine and the
    forecast-loads API."""
    if not gateway_id:
        return ""
    if month is None:
        from datetime import datetime as _dt
        month = _dt.now().month
    try:
        cached_lat = await db.get_config_value(f"lat_{gateway_id}")
        lat = None
        if cached_lat:
            try:
                lat = float(cached_lat)
            except (TypeError, ValueError):
                lat = None
        if lat is None and registry is not None:
            try:
                gw_svc = registry.get_gateway(gateway_id)
                if gw_svc is None:
                    logger.warning(
                        f"resolve_site_season_for_gateway[{gateway_id}]: "
                        f"gateway not registered"
                    )
                else:
                    # GatewayService lazy-instantiates its franklinwh-cloud client.
                    client = await gw_svc._get_or_create_client()
                    loc_resp = await client.get_equipment_location()
                    if loc_resp and "latitude" in loc_resp:
                        lat = float(loc_resp["latitude"])
                        await db.set_config_value(f"lat_{gateway_id}", str(lat))
                    else:
                        logger.warning(
                            f"resolve_site_season_for_gateway[{gateway_id}]: "
                            f"get_equipment_location returned no 'latitude'. resp={loc_resp!r}"
                        )
            except Exception as exc:
                logger.warning(
                    f"resolve_site_season_for_gateway[{gateway_id}]: "
                    f"latitude lookup failed: {exc!r}"
                )
        if lat is not None:
            return get_site_season(lat, month)
    except Exception as exc:
        logger.warning(f"resolve_site_season_for_gateway[{gateway_id}]: outer failure: {exc!r}")
    return ""


# ── Home Loads context for Automations Builder ──────────────────────────────
# See docs/automation_builder_home_loads_exposure_plan.md for the full spec.

_DISPATCH_CATEGORY_KEYS = {
    "1-Critical Load":  "critical",
    "2-Essential Load": "essential",
    "3-Shed Load":      "shed",
    "4-Off Grid Load":  "off_grid",
}

_EQUIPMENT_CATEGORIES = ("ev", "hvac", "water_heater", "pool", "general")


def _slugify_load_name(name: str, fallback_id: str) -> str:
    """Derive a stable URL/dot-notation-safe slug from a load name."""
    import re as _re
    s = _re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or (fallback_id or "load")


def _dispatch_category_key(dc: str) -> str:
    """Map the DB dispatch_category string to a stable namespace key."""
    return _DISPATCH_CATEGORY_KEYS.get(dc, "essential")


def _empty_dispatch_bucket() -> dict:
    return {key: {"scheduled_kw": 0.0, "active_count": 0,
                  "controllable_count": 0, "controllable_total_kw": 0.0}
            for key in _DISPATCH_CATEGORY_KEYS.values()}


def _empty_category_bucket() -> dict:
    return {cat: {"scheduled_kw": 0.0, "active_count": 0,
                  "controllable_count": 0, "controllable_total_kw": 0.0}
            for cat in _EQUIPMENT_CATEGORIES}


def _empty_gateway_bucket(season: str) -> dict:
    return {
        "forecast_total_kw":       0.0,
        "forecast_active_count":   0,
        "season":                  season,
        "controllable_total_kw":   0.0,
        "sheddable_total_kw":      0.0,
        "absorbable_total_kw":     0.0,
        "by_dispatch_category":    _empty_dispatch_bucket(),
        "by_category":             _empty_category_bucket(),
    }


def _parse_schedule_json(raw) -> list:
    """schedule_json is stored as a JSON string; tolerate already-decoded lists."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def build_home_loads_context(
    loads: list[dict],
    target_time: datetime,
    seasons_by_gateway: dict[str, str],
    rule_gateway_scope: Optional[str] = None,
) -> dict:
    """Build the FHAI-native Home Loads context for the Automations Builder.

    Pure function over DB rows + current time + per-gateway site season.
    Never reads Home Assistant. Never exposes raw HA values. See
    docs/automation_builder_home_loads_exposure_plan.md for the contract.
    """
    def _in_scope(load: dict) -> bool:
        if rule_gateway_scope is None:
            return True
        gw = load.get("gateway_id") or "global"
        return gw == rule_gateway_scope or gw == "global"

    visible = [l for l in (loads or []) if _in_scope(l)]

    per_load: dict[str, dict[str, dict]] = {}
    aggregates_top = {
        "total_count":            0,
        "enabled_count":          0,
        "forecast_active_count":  0,
        "forecast_total_kw":      0.0,
        "controllable_count":     0,
        "controllable_total_kw":  0.0,
        "observable_count":       0,
        "sheddable_count":        0,
        "sheddable_total_kw":     0.0,
        "absorbable_count":       0,
        "absorbable_total_kw":    0.0,
        # Distinct in-use values for AB rules that want to ask
        # "what categories / priorities are configured at all?" without
        # enumerating per-load fields. Comma-separated for str.format
        # friendliness; the *_distinct_count companion fields give the
        # numeric count for threshold conditions.
        "dispatch_categories":         "",
        "dispatch_categories_count":   0,
        "categories":                  "",
        "categories_count":            0,
        "by_gateway":             {},
    }
    _seen_dc: set[str] = set()
    _seen_cat: set[str] = set()
    gw_buckets: dict[str, dict] = aggregates_top["by_gateway"]

    for load in visible:
        gw_key = load.get("gateway_id") or "global"
        season = seasons_by_gateway.get(gw_key, "")

        if gw_key not in gw_buckets:
            gw_buckets[gw_key] = _empty_gateway_bucket(season)
        if gw_key not in per_load:
            per_load[gw_key] = {}

        # Slug: prefer the stored DB value (stable across renames per the
        # v46 migration). Fall back to deriving from name for any row that
        # the migration hasn't backfilled.
        stored_slug = (load.get("slug") or "").strip()
        base_slug = stored_slug or _slugify_load_name(load.get("name", ""), load.get("id", ""))
        slug = base_slug
        n = 2
        while slug in per_load[gw_key]:
            slug = f"{base_slug}_{n}"
            n += 1

        is_controllable   = bool(load.get("ha_switch_entity_id"))
        is_observable     = bool(load.get("ha_binary_entity_id"))
        is_metered_power  = bool(load.get("ha_entity_id"))
        is_metered_energy = bool(load.get("ha_energy_entity_id"))
        if is_controllable and is_observable:
            controllability = "control+observe"
        elif is_controllable:
            controllability = "control_only"
        elif is_observable:
            controllability = "observe_only"
        else:
            controllability = "none"

        schedules = _parse_schedule_json(load.get("schedule_json"))
        schedule_active_now = any(
            is_schedule_active(s, target_time, season) for s in schedules
        )

        avg_kw  = float(load.get("avg_kw", 0.0) or 0.0)
        peak_kw = float(load.get("peak_kw", 0.0) or 0.0)
        enabled = bool(load.get("enabled", 1))
        dc      = load.get("dispatch_category", "2-Essential Load")
        cat     = load.get("category", "general")
        m_type  = load.get("measurement_type", "forecast")

        per_load[gw_key][slug] = {
            "name":                 load.get("name", ""),
            "slug":                 slug,
            "id":                   load.get("id", ""),
            "enabled":              enabled,
            "measurement_type":     m_type,
            "category":             cat,
            "dispatch_category":    dc,
            "gateway_id":           gw_key,
            "avg_kw":               avg_kw,
            "peak_kw":              peak_kw,
            "schedule_active_now":  schedule_active_now,
            "is_controllable":      is_controllable,
            "is_observable":        is_observable,
            "is_metered_power":     is_metered_power,
            "is_metered_energy":    is_metered_energy,
            "controllability":      controllability,
        }

        # Aggregates: top-level + per-gateway slice
        aggregates_top["total_count"] += 1
        if enabled:
            aggregates_top["enabled_count"] += 1
            _seen_dc.add(dc)
            _seen_cat.add(cat)

        # Forecast-active rollups (only forecast loads contribute scheduled
        # kW; 'now' loads have no schedule-driven kW because their power
        # comes from HA).
        forecast_contrib = enabled and m_type == "forecast" and schedule_active_now
        if forecast_contrib:
            aggregates_top["forecast_active_count"]            += 1
            aggregates_top["forecast_total_kw"]                += avg_kw
            gw_buckets[gw_key]["forecast_active_count"]        += 1
            gw_buckets[gw_key]["forecast_total_kw"]            += avg_kw

        if enabled and is_controllable:
            aggregates_top["controllable_count"]               += 1
            aggregates_top["controllable_total_kw"]            += avg_kw
            gw_buckets[gw_key]["controllable_total_kw"]        += avg_kw

            if dc == "3-Shed Load":
                aggregates_top["sheddable_count"]              += 1
                aggregates_top["sheddable_total_kw"]           += avg_kw
                gw_buckets[gw_key]["sheddable_total_kw"]       += avg_kw
            if dc != "1-Critical Load":
                aggregates_top["absorbable_count"]             += 1
                aggregates_top["absorbable_total_kw"]          += avg_kw
                gw_buckets[gw_key]["absorbable_total_kw"]      += avg_kw

        if enabled and is_observable:
            aggregates_top["observable_count"]                 += 1

        # Per-dispatch-category and per-category cross-cuts (enabled-only)
        if enabled:
            dc_key  = _dispatch_category_key(dc)
            cat_key = cat if cat in _EQUIPMENT_CATEGORIES else "general"
            dc_bucket  = gw_buckets[gw_key]["by_dispatch_category"][dc_key]
            cat_bucket = gw_buckets[gw_key]["by_category"][cat_key]

            if forecast_contrib:
                dc_bucket["scheduled_kw"]  += avg_kw
                dc_bucket["active_count"]  += 1
                cat_bucket["scheduled_kw"] += avg_kw
                cat_bucket["active_count"] += 1

            if is_controllable:
                dc_bucket["controllable_count"]     += 1
                dc_bucket["controllable_total_kw"]  += avg_kw
                cat_bucket["controllable_count"]    += 1
                cat_bucket["controllable_total_kw"] += avg_kw

    # Finalise distinct-list aggregates (sorted for stable rendering)
    aggregates_top["dispatch_categories"] = ", ".join(sorted(_seen_dc))
    aggregates_top["dispatch_categories_count"] = len(_seen_dc)
    aggregates_top["categories"] = ", ".join(sorted(_seen_cat))
    aggregates_top["categories_count"] = len(_seen_cat)

    return {"home_load": per_load, "home_loads": aggregates_top}


def is_schedule_active(sch: dict, target_time: datetime, current_season: str = "") -> bool:
    """Check if a TOU-style schedule period block is active at the given
    time. Supports everyday, weekdays, weekends, specific day of week, or
    day of month. Also supports season matching if `seasonName` is provided
    in the schedule."""
    if not sch.get("is_active", True):
        return False

    # Season check if schedule is season-bound
    sch_season = (sch.get("seasonName") or "").lower()
    if sch_season and current_season and sch_season != current_season:
        return False

    period = (sch.get("period") or "").lower()
    current_time_str = target_time.strftime("%H:%M")
    day_name = target_time.strftime("%A").lower()
    is_weekend = target_time.weekday() >= 5
    day_of_month = target_time.day

    match = False
    if period == "everyday": match = True
    elif period == "weekdays" and not is_weekend: match = True
    elif period == "weekends" and is_weekend: match = True
    elif period == day_name: match = True
    else:
        import re
        m = re.match(r'^(\d+)(st|nd|rd|th)?\s+of\s+month$', period)
        if m and int(m.group(1)) == day_of_month:
            match = True

    if not match:
        return False

    start_t = sch.get("start_time", "00:00")
    end_t = sch.get("end_time", "23:59")

    if start_t <= end_t:
        return start_t <= current_time_str <= end_t
    else:
        # Crosses midnight
        return current_time_str >= start_t or current_time_str <= end_t


def is_heating_cooling_load(load_name: str) -> bool:
    """Check if the load name suggests a heating or cooling device."""
    name = (load_name or "").lower()
    keywords = ["hvac", "ac", "heater", "heating", "cooling", "aircon", "conditioner", "climate", "heatpump"]
    return any(k in name for k in keywords)


def get_active_forecast_load_kw(
    loads: list[dict],
    target_time: datetime,
    current_season: str,
    is_current_slot: bool = False,
    ha_states: dict = None,
    gateway_id: str = "global",
    weather_extreme_impact: bool = False,
    is_extreme_temp: bool = False,
) -> float:
    """Calculate the total kW to add to the baseline load for a given
    interval. If measurement_type == 'now' AND it is the current slot AND
    the HA entity is provided:
        - If switch/binary_sensor and ON -> use avg_kw (scaled if extreme weather)
        - If numeric sensor -> use live numeric kW value
    Otherwise, fall back to schedule-based `avg_kw`."""
    if ha_states is None:
        ha_states = {}

    total_kw = 0.0
    for load in loads:
        if not load.get("enabled", 1):
            continue

        load_gw = load.get("gateway_id", "global")
        if load_gw != "global" and gateway_id != "global" and load_gw != gateway_id:
            continue

        m_type = load.get("measurement_type", "forecast")
        avg_kw = float(load.get("avg_kw", 0.0))
        if weather_extreme_impact and is_extreme_temp and is_heating_cooling_load(load.get("name", "")):
            avg_kw *= 1.25

        # 1. Real-time 'now' logic for current interval
        if m_type == "now" and is_current_slot:
            # Try Power Sensor (ha_entity_id) first
            entity_id = load.get("ha_entity_id")
            resolved_power = False
            if entity_id and entity_id in ha_states:
                state = ha_states[entity_id]
                try:
                    val = float(state)
                    # Sanity check: filter out accumulator sensors that report
                    # total kWh (e.g. >100 kWh)
                    if val < 100.0:
                        total_kw += val
                        resolved_power = True
                    else:
                        logger.warning(
                            f"Ignoring entity {entity_id} value {val} as it exceeds "
                            f"the 100 kW sanity threshold (likely an energy accumulator/kWh)"
                        )
                except ValueError:
                    if str(state).lower() in ("on", "true", "running"):
                        total_kw += avg_kw
                        resolved_power = True

            # Fallback to Switch Toggle (ha_switch_entity_id)
            if not resolved_power:
                switch_id = load.get("ha_switch_entity_id")
                if switch_id and switch_id in ha_states:
                    state = ha_states[switch_id]
                    if str(state).lower() in ("on", "true", "running"):
                        total_kw += avg_kw
                        resolved_power = True

            # Fallback to Binary Active status (ha_binary_entity_id)
            if not resolved_power:
                binary_id = load.get("ha_binary_entity_id")
                if binary_id and binary_id in ha_states:
                    state = ha_states[binary_id]
                    if str(state).lower() in ("on", "true", "running", "active", "charging"):
                        total_kw += avg_kw
                        resolved_power = True

            if resolved_power:
                continue

        # 2. Schedule-based logic
        schedule_json = load.get("schedule_json", "[]")
        try:
            import json as _json
            periods = _json.loads(schedule_json)
        except Exception:
            periods = []

        is_active = False
        for p in periods:
            if is_schedule_active(p, target_time, current_season):
                is_active = True
                break

        if is_active:
            total_kw += avg_kw

    return total_kw
