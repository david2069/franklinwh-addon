"""
Phase 9 — Schedule, Battery, Profile, and Entities API routes.

Routes:
  GET /api/gateways/{id}/schedule       — current mode/storm/reserve from Cloud API (or cached)
  GET /api/gateways/{id}/batteries      — aPower batteries from DB
  GET /api/gateways/{id}/profile        — stored GatewayProfile dict
  GET /api/entities                     — full entity registry (aGate + accessory)
  GET /api/metrics                      — recent gateway_metrics rows (supports ?short_id=&limit=)
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.app_state import get_app_state
from src.services import dispatch_codes
from src.services.snapshot_to_preset import describe_projection, flatten_snapshot
from src.services import db
from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES

def _get_registry():
    return get_app_state().get("registry")


logger = logging.getLogger(__name__)
router = APIRouter(tags=["phase9"])


# ── Entity registry (static, served from Python model) ───────

@router.get("/entities")
async def list_entities():
    """Return full entity registry: all aGate entities + aPower accessory entities."""
    result = []
    for e in AGATE_ENTITIES:
        result.append({
            "slug": e.slug,
            "name": e.name,
            "ha_type": e.ha_type,
            "state_group": e.state_group,
            "unit": e.unit,
            "device_class": e.device_class,
            "icon": e.icon,
            "hw_requires": e.hw_requires,
            "is_control": e.is_control,
            "entity_category": e.entity_category,
            "is_accessory": False,
        })
    for e in BATTERY_ACCESSORY_ENTITIES:
        result.append({
            "slug": e.slug,
            "name": e.name,
            "ha_type": e.ha_type,
            "state_group": e.state_group,
            "unit": e.unit,
            "device_class": e.device_class,
            "icon": e.icon,
            "hw_requires": "",
            "is_control": False,
            "entity_category": "",
            "is_accessory": True,
        })
    return result


# ── Per-gateway: batteries ────────────────────────────────────

@router.get("/gateways/{short_id}/batteries")
async def gateway_batteries(short_id: str):
    """Return aPower batteries registered for a gateway, enriched with live hardware firmware and diagnostic payload."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")
        
    db_bats_raw = await db.get_batteries_for_gateway(short_id)
    db_bats = [dict(b) for b in db_bats_raw]
    
    registry = _get_registry()
    svc = registry.get_gateway(short_id) if registry else None
    
    if svc and svc._client:
        try:
            live_resp = await svc._client.get_apower_info()
            live_list = live_resp.get("result", []) if isinstance(live_resp, dict) else live_resp
            # Merge live data into db matching
            for db_bat in db_bats:
                db_bat["debug_live"] = live_list
                sn = db_bat.get("full_serial") or db_bat.get("short_id")
                if not sn: continue
                # find matching live payload
                for live in live_list:
                    if sn.endswith(live.get("apowerSn", "")):
                        db_bat.update(live)
                        break
        except Exception as e:
            if len(db_bats) > 0:
                db_bats[0]["debug_error"] = repr(e)
            logger.warning(f"Failed to fetch live get_apower_info for {short_id}: {e}")
            
    return db_bats

@router.get("/gateways/{short_id}/batteries/debug")
async def gateway_batteries_debug(short_id: str):
    registry = _get_registry()
    svc = registry.get_gateway(short_id)
    if not svc: return {"error": "no svc"}
    res = await svc._client.get_apower_info()
    return {"raw": res}


# ── Per-gateway: profile ──────────────────────────────────────

@router.get("/gateways/{short_id}/profile")
async def gateway_profile(short_id: str):
    """Return the stored GatewayProfile dict for a gateway."""
    gw = await db.get_gateway_full(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")
    profile_json = gw.get("profile_json")
    if not profile_json:
        return {"error": "No profile data yet — gateway has not completed a Cloud API poll."}
    try:
        return json.loads(profile_json)
    except json.JSONDecodeError:
        return {"error": "Profile data is corrupt."}


@router.get("/gateways/{short_id}/summary")
async def gateway_setup_summary(short_id: str):
    """Plain-language account of how this gateway is set up.

    Reads the cached profile rather than calling the cloud — discovery is
    install-centric and a page load should not cost a multi-call round trip.
    Live mode and SoC are folded in from the running service when there is one.

    Always reports when the profile was captured: it refreshes on demand, not on
    a timer, so "when was this true" is part of the answer.
    """
    from src.services.setup_summary import build_summary

    gw = await db.get_gateway_full(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    live = {}
    registry = get_app_state().get("registry")
    svc = registry.get_gateway(short_id) if registry else None
    if svc is not None:
        try:
            live = svc.status.last_data or {}
        except Exception as exc:
            logger.debug(f"[{short_id}] summary: live state unavailable: {exc!r}")

    return {"ok": True, "short_id": short_id, "summary": build_summary(gw, live)}


@router.post("/gateways/{short_id}/profile/refresh")
async def gateway_profile_refresh(short_id: str):
    """Re-run discovery and rewrite the stored profile for this gateway.

    On demand rather than scheduled, because discover is install-centric: it
    describes what is physically fitted. Run it after an installer visit, or
    after adding solar, an aPower, an aGate or accessories.

    Returns the fields that actually changed, so the UI can report what moved
    rather than a bare success.
    """
    from src.services.integration_manager import refresh_gateway_profile
    try:
        return await refresh_gateway_profile(short_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.warning(f"[{short_id}] profile refresh failed: {exc!r}")
        raise HTTPException(status_code=502, detail=f"Discovery failed: {exc}")


@router.post("/gateways/{short_id}/profile/power")
async def gateway_profile_power_set(short_id: str, payload: dict):
    """
    Update local power control imports/exports
    Payload: {"exportMax": float, "importMax": float}
    """
    registry = _get_registry()
    svc = registry.get_gateway(short_id)
    if not svc:
        return {"error": "no svc"}
    res = await svc._client.set_power_control_settings(
        globalGridDischargeMax=payload.get("exportMax", 0),
        globalGridChargeMax=payload.get("importMax", 10)
    )
    return {"ok": True, "result": res}


@router.post("/gateways/{short_id}/profile/islanding")
async def gateway_profile_islanding_set(short_id: str, payload: dict):
    """
    Toggle Islanding.
    Payload: {"offgrid": bool, "soc": int}
    """
    from franklinwh_cloud.models import GridStatus
    registry = _get_registry()
    svc = registry.get_gateway(short_id)
    if not svc:
        return {"ok": False, "error": "Gateway not running — start it from the Gateways tab first."}

    going_offgrid = bool(payload.get("offgrid"))
    reconnect_soc = int(payload.get("soc") or 5)

    # Safety: reject off-grid if live battery SOC < 6% (matches FEM guard)
    if going_offgrid:
        try:
            status = registry.get_status(short_id)
            last_stats = _live_stats(status)
            live_soc = last_stats.get("battery_soc", 100)
            if live_soc is not None and int(live_soc) < 6:
                return {
                    "ok": False,
                    "error": f"Cannot go Off-Grid: battery SOC is {live_soc}% "
                             f"(minimum 6% required for safe islanding)."
                }
        except Exception as guard_exc:
            logger.warning(f"[{short_id}] SOC safety guard check failed: {guard_exc}")

    grid_status = GridStatus.OFF if going_offgrid else GridStatus.NORMAL
    try:
        res = await svc._client.set_grid_status(status=grid_status, soc=reconnect_soc)
    except Exception as exc:
        logger.error(f"[{short_id}] set_grid_status failed: {exc}")
        return {"ok": False, "error": str(exc)}

    # Inspect actual Cloud API response — don't always report success
    if isinstance(res, dict):
        api_ok = res.get("ok", res.get("success", res.get("code") == 0))
        if api_ok is False:
            err_msg = res.get("msg") or res.get("error") or res.get("message") or f"Cloud API rejected islanding command (raw: {res})"
            logger.warning(f"[{short_id}] set_grid_status API rejection: {err_msg}")
            return {"ok": False, "error": err_msg}

    logger.info(f"[{short_id}] set_grid_status({'OFF' if going_offgrid else 'NORMAL'}, soc={reconnect_soc}) -> {res}")
    return {"ok": True, "result": res}


# ── Per-gateway: schedule ─────────────────────────────────────

def _requires_multi(strategy_list: list) -> bool:
    """Return True if set_tou_schedule_multi() MUST be used to avoid data-loss.

    State machine:
      []                              → flat (no seasons)  → False (use flat)
      [1 season, dayType=3 everyday]  → flat-safe          → False (use flat)
      [1 season, dayType=1 OR 2]      → weekday/wknd split → True  (MUST use multi)
      [2+ seasons, any day types]     → multi-season       → True  (MUST use multi)
    """
    if not strategy_list:
        return False
    if len(strategy_list) > 1:
        return True
    # Single season — inspect day types
    day_types = strategy_list[0].get("dayTypeVoList", [])
    if len(day_types) > 1:
        return True
    # Single day type — is it explicitly weekday-only or weekend-only?
    if day_types and day_types[0].get("dayType") in (1, 2):
        return True
    return False  # dayType=3 (Everyday) — flat variant is safe


def _active_season_idx(strategy_list: list, today_month: int) -> int:
    """Return the index of the season that contains today's month."""
    for i, season in enumerate(strategy_list):
        months_str = season.get("month") or season.get("months") or ""
        try:
            months = {int(m.strip()) for m in months_str.split(",") if m.strip()}
            if today_month in months:
                return i
        except (ValueError, AttributeError):
            pass
    return 0  # fallback to first season


def _active_day_type(today_weekday: int) -> int:
    """Return 1 (weekday Mon–Fri) or 2 (weekend Sat–Sun) based on Python weekday()."""
    return 2 if today_weekday >= 5 else 1


def _derive_tou_periods(strategy_list: list, season_idx: int, day_type_int: int) -> list:
    """Extract the detailVoList for the active season/day-type as a flat tou_periods list.
    Falls back gracefully if indices are out of range.
    """
    try:
        season = strategy_list[season_idx]
        day_type_vos = season.get("dayTypeVoList", [])
        # Prefer the requested day type; fall back to first available
        target = next((d for d in day_type_vos if d.get("dayType") == day_type_int), None)
        if target is None and day_type_vos:
            target = day_type_vos[0]
        if target is None:
            return []
        return target.get("detailVoList", [])
    except (IndexError, TypeError, AttributeError):
        return []


def _live_stats(status: dict | None) -> dict:
    """The gateway's latest telemetry from a status payload.

    GatewayStatus.to_dict() emits "last_data". Several readers here asked for
    "last_stats", a key nothing produces, so they silently received {} — mode
    reported "Unknown" while the telemetry plainly said "Self-Consumption",
    grid charge/discharge were always None, and live SOC fell back to a
    hardcoded 100%.

    Both names are accepted because api_automation.py already does the same,
    with a comment noting the drift; one helper is better than the two
    divergent spellings that produced this.
    """
    status = status or {}
    return status.get("last_data") or status.get("last_stats") or {}


@router.get("/gateways/{short_id}/schedule")
async def gateway_schedule(short_id: str):
    """
    Return current schedule/mode data for a gateway.

    Always returns:
      - tou_periods       : flat list for the active season/day-type (backward compat)
      - strategy_list     : full raw strategyList (zero or more seasons)
      - is_multi_season   : True if set_tou_schedule_multi() must be used to avoid data-loss
      - active_season_idx : index of season containing today's month
      - active_day_type   : 1=weekday 2=weekend 3=everyday (based on today)
      - entrance_info     : tariffSettingFlag + pcsEntrance etc (Phase 3 pre-check)
    """
    import datetime
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    if not registry:
        return {"error": "Registry not initialised"}

    status = registry.get_status(short_id)
    if not status:
        return {"error": "Gateway not running — start it from the Gateways tab first."}

    # Pull mode info from last stats snapshot
    last_stats = _live_stats(status)
    mode_info = last_stats.get("mode", {})
    storm_info = last_stats.get("storm", {})

    # Today's calendar context for active season / day-type detection
    today = datetime.date.today()
    today_month = today.month
    today_weekday = today.weekday()  # 0=Mon … 6=Sun

    # Fetch live TOU detail from Cloud API
    tou_periods = []
    strategy_list = []
    is_multi = False
    active_season_idx = 0
    active_day_type = _active_day_type(today_weekday)
    note = "No TOU periods configured for today."

    gw_svc = registry.get_gateway(short_id)
    if gw_svc:
        raw_res = await gw_svc.get_tou_schedule()
        if raw_res.get("ok"):
            detail_obj = raw_res.get("detail") or {}
            res_data = detail_obj.get("result") or {}

            # ── Extract full strategyList (zero or more seasons) ──────────
            strategy_list = res_data.get("strategyList") or []
            is_multi = _requires_multi(strategy_list)

            if strategy_list:
                # Determine active season and day-type from today's date
                active_season_idx = _active_season_idx(strategy_list, today_month)
                # Derive tou_periods from the active season/day-type (backward compat)
                raw_periods = _derive_tou_periods(strategy_list, active_season_idx, active_day_type)
                for r in raw_periods:
                    tou_periods.append({
                        "start":                    r.get("startHourTime"),
                        "end":                      r.get("endHourTime"),
                        "startHourTime":            r.get("startHourTime"),
                        "endHourTime":              r.get("endHourTime"),
                        "name":                     r.get("name") or "Time Block",
                        "waveType":                 r.get("waveType", 0),
                        "dispatchId":               r.get("dispatchId", 1),
                        "maxChargeSoc":             r.get("maxChargeSoc", 100),
                        "minDischargeSoc":          r.get("minDischargeSoc", 0),
                        "gridChargeMax":            r.get("gridChargeMax", 5000),
                        "gridDischargeMax":         r.get("gridDischargeMax", 5000),
                        # Rate fields (API field names per RATE_FIELD_MAP in franklinwh_cloud)
                        "eleticRatePeak":           r.get("eleticRatePeak"),
                        "eleticRateShoulder":       r.get("eleticRateShoulder"),
                        "eleticRateValley":         r.get("eleticRateValley"),
                        "eleticRateSuperOffPeak":   r.get("eleticRateSuperOffPeak"),
                        "eleticRateSharp":          r.get("eleticRateSharp"),
                        "eleticRateGridFee":        r.get("eleticRateGridFee"),
                        "eleticSellPeak":           r.get("eleticSellPeak"),
                        "eleticSellShoulder":       r.get("eleticSellShoulder"),
                        "eleticSellValley":         r.get("eleticSellValley"),
                        "eleticSellSuperOffPeak":   r.get("eleticSellSuperOffPeak"),
                        "eleticSellSharp":          r.get("eleticSellSharp"),
                    })
            else:
                # No strategyList — try legacy detailDefaultVo path
                default_vo = res_data.get("detailDefaultVo") or {}
                for r in (default_vo.get("touDispatchList") or []):
                    tou_periods.append({
                        "start":                    r.get("startHourTime"),
                        "end":                      r.get("endHourTime"),
                        "startHourTime":            r.get("startHourTime"),
                        "endHourTime":              r.get("endHourTime"),
                        "name":                     r.get("name") or "Time Block",
                        "waveType":                 r.get("waveType", 0),
                        "dispatchId":               r.get("dispatchId", 1),
                        "maxChargeSoc":             r.get("maxChargeSoc", 100),
                        "minDischargeSoc":          r.get("minDischargeSoc", 0),
                        "gridChargeMax":            r.get("gridChargeMax", 5000),
                        "gridDischargeMax":         r.get("gridDischargeMax", 5000),
                        # Rate fields (API field names per RATE_FIELD_MAP in franklinwh_cloud)
                        "eleticRatePeak":           r.get("eleticRatePeak"),
                        "eleticRateShoulder":       r.get("eleticRateShoulder"),
                        "eleticRateValley":         r.get("eleticRateValley"),
                        "eleticRateSuperOffPeak":   r.get("eleticRateSuperOffPeak"),
                        "eleticRateSharp":          r.get("eleticRateSharp"),
                        "eleticRateGridFee":        r.get("eleticRateGridFee"),
                        "eleticSellPeak":           r.get("eleticSellPeak"),
                        "eleticSellShoulder":       r.get("eleticSellShoulder"),
                        "eleticSellValley":         r.get("eleticSellValley"),
                        "eleticSellSuperOffPeak":   r.get("eleticSellSuperOffPeak"),
                        "eleticSellSharp":          r.get("eleticSellSharp"),
                    })

            season_count = len(strategy_list)
            note = (f"Loaded {len(tou_periods)} active TOU periods "
                    f"({season_count} season{'s' if season_count != 1 else ''}, "
                    f"{'multi' if is_multi else 'flat'} mode).")
        else:
            note = f"Failed to fetch TOU data from cloud: {raw_res.get('error')}"

    # ── Phase 3: entrance_info pre-check ─────────────────────────────
    # Fetch cached entrance_info from last_data so we don't make extra API calls.
    # tariffSettingFlag=False means TOU endpoints are not available on this gateway.
    entrance_info = {}
    try:
        entrance_raw = (status.get("last_data") or {}).get("entrance_info") or {}
        entrance_info = {
            "tariff_setting_flag": bool(entrance_raw.get("tariffSettingFlag", True)),
            "pcs_entrance":        bool(entrance_raw.get("pcsEntrance", True)),
            "bb_entrance":         bool(entrance_raw.get("bbEntrance", False)),
        }
    except Exception:
        entrance_info = {"tariff_setting_flag": True}  # assume available if unknown

    return {
        "short_id":         short_id,
        "mode":             mode_info.get("work_mode_desc", "Unknown"),
        "storm_hedge":      storm_info.get("enabled", False),
        "backup_reserve":   mode_info.get("backup_soc"),
        "grid_charge":      last_stats.get("grid", {}).get("charge_enabled"),
        "grid_discharge":   last_stats.get("grid", {}).get("discharge_enabled"),
        "tou_periods":      tou_periods,          # backward-compat: active season/day-type flat list
        "strategy_list":    strategy_list,         # full multi-season structure (may be [])
        "is_multi_season":  is_multi,              # True → must use set_tou_schedule_multi on save
        "active_season_idx": active_season_idx,   # index into strategy_list for today
        "active_day_type":  active_day_type,       # 1=weekday 2=weekend
        "entrance_info":    entrance_info,         # Phase 3: tariff availability flags
        "note":             note,
    }



@router.post("/gateways/{short_id}/schedule/set")
async def gateway_schedule_set(short_id: str, payload: dict):
    """
    Submits a new TOU schedule to the gateway via the Cloud API.

    Routing logic (BKL-SCHED-01 Phase 1 — data-loss fix):
      - If payload contains 'strategy_list' (non-empty list):
          → calls set_tou_schedule_multi() to preserve multi-season/day-type structure
      - Otherwise:
          → calls set_tou_schedule() (flat, single-season — backward compat)

    The caller (JS) must always echo back the full strategy_list received from
    GET /schedule so we round-trip safely rather than silently flattening.
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")

    gw_svc = registry.get_gateway(short_id)
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running — start it from the Gateways tab first.")

    strategy_list = payload.get("strategy_list")
    if strategy_list and isinstance(strategy_list, list) and len(strategy_list) > 0:
        # Multi-season round-trip — preserves all seasons and day-types exactly
        logger.info(f"[{short_id}] schedule/set: using set_tou_schedule_multi ({len(strategy_list)} seasons)")
        res = await gw_svc.set_tou_schedule_multi(strategy_list)
    else:
        # Flat single-season path — backward compat for simple configs
        logger.info(f"[{short_id}] schedule/set: using set_tou_schedule (flat)")
        res = await gw_svc.set_tou_schedule(payload)

    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Unknown API error"))

    # The push is the moment the battery's behaviour changes, and it was
    # recorded only as a logger.info plus a snapshot row. A snapshot says what
    # was sent; the audit says that a send happened, which is the question
    # asked when something changes unexpectedly.
    try:
        await db.log_admin_audit(
            event="Schedule:Pushed_To_Gateway", source="ui",
            details=(
                f"[{short_id}] TOU schedule uploaded to the cloud — "
                f"{len(strategy_list or [])} season(s), "
                f"{'multi-season' if strategy_list else 'flat'} payload"
            ),
        )
    except Exception:
        logger.debug(f"[{short_id}] push audit failed", exc_info=True)

    # ── Auto-snapshot: record every successful push for audit/restore ──
    try:
        snap_strategy = strategy_list if (strategy_list and isinstance(strategy_list, list)) else []
        await db.insert_tou_snapshot(
            short_id=short_id,
            strategy_list=snap_strategy,
            source="gateway_save",
        )
    except Exception as snap_err:
        logger.warning(f"[{short_id}] Failed to write TOU snapshot: {snap_err}")

    return {"ok": True, "result": res.get("detail")}



# ── Phase 3: Usage Type + Tariff Identity + Utility Wizard ──────────────────

@router.post("/gateways/{short_id}/schedule/usage-type")
async def gateway_schedule_set_usage_type(short_id: str, payload: dict):
    """
    Store the user's schedule usage type preference in app_config.
    Payload: { "usage_type": 1 | 2 | 3 }
      1 = Scheduling Only
      2 = Schedule & Pricing (static rates)
      3 = Schedule & Dynamic Pricing (live provider rates)
    """
    usage_type = payload.get("usage_type")
    if usage_type not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="usage_type must be 1, 2, or 3")
    from src.services.db import set_config_value
    await set_config_value(f"schedule_usage_type_{short_id}", str(usage_type))
    logger.info(f"[{short_id}] schedule usage_type set to {usage_type}")
    return {"ok": True, "usage_type": usage_type}


@router.get("/gateways/{short_id}/schedule/usage-type")
async def gateway_schedule_get_usage_type(short_id: str):
    """Return the stored usage type for a gateway (null if never set = show prompt)."""
    from src.services.db import get_config_value
    raw = await get_config_value(f"schedule_usage_type_{short_id}", None)
    return {
        "usage_type": int(raw) if raw is not None else None,
        "prompt_required": raw is None,
    }


@router.get("/gateways/{short_id}/schedule/tariff")
async def gateway_schedule_tariff(short_id: str):
    """
    Return the active tariff identity for a gateway (name, utility, type).
    Sourced from get_tou_dispatch_detail cached at startup in last_data.
    """
    registry = get_app_state().get("registry")
    if not registry:
        return {"error": "Registry not initialised"}
    status = registry.get_status(short_id)
    if not status:
        return {"error": "Gateway not running"}

    gw_svc = registry.get_gateway(short_id)
    if not gw_svc:
        return {"error": "Gateway service not found"}

    try:
        raw = await gw_svc.get_tou_schedule()
        if not raw.get("ok"):
            return {"tariff": None, "error": raw.get("error")}
        res_data = (raw.get("detail") or {}).get("result") or {}
        template = res_data.get("template") or {}
        return {
            "tariff": {
                "name":             template.get("name") or template.get("tariffName") or "Custom",
                "utility":          template.get("companyName") or template.get("utility") or "",
                "country":          template.get("country") or "",
                "electricity_type": template.get("electricityType") or 1,  # 1=TOU, 2=Flat, 3=Tiered
                "tariff_id":        template.get("id") or template.get("tariffId") or None,
                "is_nbt":           bool(template.get("isNbt") or template.get("isNBT")),
                "work_mode":        template.get("workMode") or "",
                "nem_type":         template.get("nemType") or "",
            }
        }
    except Exception as exc:
        logger.warning(f"[{short_id}] schedule/tariff fetch failed: {exc}")
        # Fall back to locally stored tariff metadata (from custom setup)
        from src.services.db import get_config_value
        local_utility = await get_config_value(f"schedule_tariff_utility_{short_id}", None)
        local_name = await get_config_value(f"schedule_tariff_name_{short_id}", None)
        local_type = await get_config_value(f"schedule_tariff_type_{short_id}", "1")
        local_nbt = await get_config_value(f"schedule_tariff_is_nbt_{short_id}", "false")
        if local_name:
            return {
                "tariff": {
                    "name": local_name,
                    "utility": local_utility or "",
                    "electricity_type": int(local_type or 1),
                    "is_nbt": local_nbt == "true",
                    "source": "local"
                }
            }
        return {"tariff": None, "error": str(exc)}


@router.post("/gateways/{short_id}/schedule/tariff")
async def gateway_schedule_tariff_set(short_id: str, payload: dict):
    """
    Store custom tariff identity metadata in app_config for display in the
    Tariff Identity Card. Called by the Tariff Setup wizard (Phase 3).
    Payload: { utility, name, electricity_type, is_nbt }
    """
    utility = payload.get("utility", "").strip()
    name = payload.get("name", "").strip()
    electricity_type = int(payload.get("electricity_type", 1))
    is_nbt = bool(payload.get("is_nbt", False))

    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if electricity_type not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="electricity_type must be 1 (TOU), 2 (Fixed), or 3 (Tiered)")

    from src.services.db import set_config_value
    await set_config_value(f"schedule_tariff_utility_{short_id}", utility)
    await set_config_value(f"schedule_tariff_name_{short_id}", name)
    await set_config_value(f"schedule_tariff_type_{short_id}", str(electricity_type))
    await set_config_value(f"schedule_tariff_is_nbt_{short_id}", "true" if is_nbt else "false")

    logger.info(f"[{short_id}] schedule/tariff custom set: {name!r} ({['', 'TOU', 'Fixed', 'Tiered'][electricity_type]}) utility={utility!r} nbt={is_nbt}")
    return {
        "ok": True,
        "tariff": {
            "name": name,
            "utility": utility,
            "electricity_type": electricity_type,
            "is_nbt": is_nbt,
            "source": "local"
        }
    }


@router.get("/gateways/{short_id}/schedule/utilities")
async def gateway_schedule_utilities(
    short_id: str,
    country_id: int = Query(default=1, description="Country ID (1=US, 2=AU)"),
    province_id: int = Query(default=0, description="US State/Province ID (0=list all)"),
    search: str = Query(default="", description="Filter by utility name"),
):
    """
    Search utility companies for the US Utility Lookup wizard.
    Proxies get_utility_companies(country_id, province_id) from the Cloud API.
    NOTE: AU utilities are not in FranklinWH's database — AU users should use Custom Tariff.
    """
    registry = get_app_state().get("registry")
    gw_svc = registry.get_gateway(short_id) if registry else None
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running")
    try:
        client = await gw_svc._get_or_create_client()
        raw = await client.get_utility_companies(country_id=country_id, province_id=province_id)
        companies = (raw or {}).get("dataList") or (raw if isinstance(raw, list) else [])
        if search:
            lo = search.lower()
            companies = [c for c in companies if lo in (c.get("companyName") or "").lower()]
        return {"companies": companies, "count": len(companies)}
    except Exception as exc:
        logger.warning(f"[{short_id}] get_utility_companies failed: {exc}")
        return {"companies": [], "error": str(exc)}


@router.get("/gateways/{short_id}/schedule/tariff-list")
async def gateway_schedule_tariff_list(
    short_id: str,
    company_id: int = Query(..., description="Utility company ID from /utilities"),
):
    """
    Return available tariff plans for a utility company.
    Proxies get_tariff_list(company_id) from the Cloud API.
    """
    registry = get_app_state().get("registry")
    gw_svc = registry.get_gateway(short_id) if registry else None
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running")
    try:
        client = await gw_svc._get_or_create_client()
        raw = await client.get_tariff_list(company_id=company_id)
        tariffs = (raw or {}).get("dataList") or (raw if isinstance(raw, list) else [])
        return {"tariffs": tariffs, "count": len(tariffs)}
    except Exception as exc:
        logger.warning(f"[{short_id}] get_tariff_list failed: {exc}")
        return {"tariffs": [], "error": str(exc)}


@router.get("/gateways/{short_id}/schedule/tariff-detail")
async def gateway_schedule_tariff_detail(
    short_id: str,
    tariff_id: int = Query(..., description="Tariff ID from /tariff-list"),
):
    """
    Return full tariff detail (rates, strategy list, metadata) for a tariff plan.
    Proxies get_tariff_detail(tariff_id) from the Cloud API (read-only).
    """
    registry = get_app_state().get("registry")
    gw_svc = registry.get_gateway(short_id) if registry else None
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running")
    try:
        client = await gw_svc._get_or_create_client()
        raw = await client.get_tariff_detail(tariff_id=tariff_id)
        return {"detail": raw}
    except Exception as exc:
        logger.warning(f"[{short_id}] get_tariff_detail failed: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/gateways/{short_id}/schedule/apply-tariff")
async def gateway_schedule_apply_tariff(short_id: str, payload: dict):
    """
    Apply a FranklinWH tariff template to this gateway.
    This is a WRITE operation that replaces the active TOU schedule.

    ⚠ Warning: This overwrites the current schedule with the server-side template.
    The UI must show a clear confirmation before calling this endpoint.

    Payload: { "tariff_id": int, "name": str }
    """
    tariff_id = payload.get("tariff_id")
    name = payload.get("name", "")
    if not tariff_id:
        raise HTTPException(status_code=400, detail="tariff_id is required")

    registry = get_app_state().get("registry")
    gw_svc = registry.get_gateway(short_id) if registry else None
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running")
    try:
        res = await gw_svc.apply_tariff_template(tariff_id=tariff_id, name=name)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error", "apply_tariff_template failed"))
        logger.info(f"[{short_id}] Tariff template applied: id={tariff_id} name='{name}'")
        return {"ok": True, "result": res.get("detail")}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[{short_id}] apply_tariff_template failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Metrics with optional short_id filter ────────────────────

@router.get("/metrics")
async def metrics(
    short_id: str = Query(default=None),
    limit: int = Query(default=20, le=100),
):
    """Return recent gateway_metrics rows (optionally filtered by gateway)."""
    return await db.get_recent_metrics(short_id=short_id, limit=limit)


# ── Schedule Presets (cross-gateway shared) ───────────────────
# Presets are a cross-gateway preset library — not scoped to a single gateway.
# Users can include a gateway name in the preset name string for organisation.

def _get_presets_mgr():
    """Retrieve the SchedulePresets singleton from app_state."""
    mgr = get_app_state().get("schedule_presets")
    if mgr is None:
        raise HTTPException(status_code=503, detail="Schedule presets not initialised")
    return mgr


@router.get("/dispatch-codes")
async def list_dispatch_codes():
    """The dispatch codes with what each one does.

    Served so the UI reads the same catalogue the backend does. The id->label
    map was previously written out in three separate places in the front end
    alone, none of which carried a description — so a dropdown reading
    "Home Loads (1)" gave no way to know it leaves the battery alone and sends
    surplus solar to the grid.
    """
    return {"codes": dispatch_codes.as_list()}


@router.get("/gateways/schedule/presets")
async def list_schedule_presets():
    """List all saved TOU schedule presets (summary only — no full schedule data)."""
    mgr = _get_presets_mgr()
    return {"presets": mgr.list_presets()}


@router.post("/gateways/schedule/presets")
async def save_schedule_preset(payload: dict):
    """Save the current schedule as a named preset.

    Body: { "name": str, "description": str (optional), "schedule": [...] }
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required.")
    description = payload.get("description", "")
    schedule = payload.get("schedule", [])
    if not isinstance(schedule, list):
        raise HTTPException(status_code=400, detail="'schedule' must be a list.")
    unverified = bool(payload.get("unverified", False))
    mgr = _get_presets_mgr()
    result = mgr.save_preset(name, description, schedule, unverified=unverified)
    # Presets are how a schedule gets reused, switched by an automation, or
    # pushed to a battery. Their lifecycle had no audit at all — only a
    # logger.info — so there was no way to answer "who changed this and when".
    await db.log_admin_audit(
        event="Schedule:Preset_Saved", source="ui",
        details=f"Preset {name!r} saved with {len(schedule or [])} blocks"
                f"{' (unverified)' if unverified else ''}",
    )
    return result


@router.post("/gateways/{short_id}/schedule/optimise")
async def optimise_schedule(
    short_id: str,
    objective: str = "lowest_bill",
    save_as: str | None = None,
):
    """Propose a dispatch schedule derived from the gateway's tariff.

    Proposes. Nothing is written to the battery — with `save_as` the result is
    stored as a preset, which is the same load/inspect/discard path as the
    built-ins and is selectable later by an automation or the
    tou_saved_dispatches entity. A schedule inferred from a rate table and
    applied silently would be a change to how a house runs that nobody asked
    for.

    Every block comes back with the reason it was chosen, because a schedule
    nobody can interrogate is one they will undo.
    """
    from datetime import datetime
    from src.services.schedule_optimiser import RATED_KWH_PER_APOWER, optimise
    from src.services.db import get_latest_tou_strategy

    strategy_list = await get_latest_tou_strategy(short_id)
    if not strategy_list:
        return {"ok": False, "error": "No TOU schedule found for this gateway — "
                                      "set one on the Schedule tab first."}

    # Solar keyed by LOCAL hour: schedule blocks are local-time boundaries, and
    # the forecast arrives in UTC. Getting this wrong shifted a Sydney site's
    # overnight block onto its own afternoon and concluded 4 kW of sun was
    # expected at 2am.
    solar_by_hour: dict[int, float] = {}
    try:
        from pathlib import Path
        from src.services.solar import SolarForecastManager
        from src.config.manager import AppConfig

        cfg = dict(await db.get_solar_forecast_config() or {})
        data_dir = AppConfig.load().data_dir
        cfg["providers"] = {
            "forecast_solar": {"api_key": cfg.get("forecast_solar_api_key") or ""},
            "solcast": {"api_key": cfg.get("solcast_api_key") or "",
                        "site_id": cfg.get("solcast_site_id") or "",
                        "_data_dir": str(data_dir)},
        }
        buckets: dict[int, list[float]] = {}
        for slot in SolarForecastManager(config=cfg, data_dir=Path(data_dir)).get_forecast():
            ts = datetime.fromisoformat(slot["timestamp"]).astimezone()
            buckets.setdefault(ts.hour, []).append(float(slot.get("pv_kw") or 0.0))
        solar_by_hour = {h: sum(v) / len(v) for h, v in buckets.items() if v}
    except Exception:
        # No solar forecast is a supported state — the tariff alone still
        # produces a usable plan, it just cannot avoid charging through sun.
        logger.debug("optimise: solar forecast unavailable", exc_info=True)

    # Seasonal history first. A TOU schedule is a repeating daily pattern that
    # lives on the gateway for months, so it should be planned against a
    # *typical* day for the season — not today's or tomorrow's weather, which
    # would bake one fortnight's cloud cover into a season-long rule. The live
    # forecast is the fallback for a site too new to have history.
    solar_by_season: dict[str, dict[int, float]] = {}
    load_by_season: dict[str, dict[int, float]] = {}
    profile_source = "forecast" if solar_by_hour else "none"
    try:
        from src.services.site_profile import build_profile, merge_day_types, is_usable

        rows = await db.get_gateway_metric_rows(short_id)
        if rows:
            for season in strategy_list:
                months = {
                    int(m) for m in str(season.get("month") or "").split(",")
                    if m.strip().isdigit()
                }
                profile = build_profile(rows, months or None)
                if is_usable(profile):
                    name = season.get("seasonName") or season.get("name")
                    solar_by_season[name] = merge_day_types(profile, "solar_kw")
                    # The same history answers "how much will the house draw
                    # through peak", which is what the charge target has to
                    # cover. Without it the target defaults to 100% and the
                    # reserve is accidental.
                    load_by_season[name] = merge_day_types(profile, "home_kw")
            if solar_by_season:
                profile_source = "history"
    except Exception:
        logger.debug("optimise: seasonal profile unavailable", exc_info=True)

    # Usable battery energy. The cloud poll rarely returns total_capacity, so
    # gateway_service derives it from the aPower count; the same fallback
    # applies here rather than leaving the sizing unsolvable.
    battery_kwh = 0.0
    try:
        registry = _get_registry()
        stats = _live_stats(registry.get_status(short_id)) if registry else {}
        cap = stats.get("capacity") or {}
        # capacity.total is now summed per unit from the device registry, so
        # the count-times-13.6 fallback is only reached when nothing identified
        # the hardware at all. It assumes aPower X, which under-states a fleet
        # of aPower 2 or S rather than over-stating it.
        battery_kwh = float(
            cap.get("total")
            or (float(cap.get("battery_count") or 0) * RATED_KWH_PER_APOWER)
        )
    except Exception:
        logger.debug("optimise: battery capacity unavailable", exc_info=True)

    result = optimise(strategy_list, objective, solar_by_hour,
                      solar_by_season=solar_by_season or None,
                      load_by_season=load_by_season or None,
                      battery_kwh=battery_kwh)
    result["battery_kwh"] = battery_kwh
    result["solar_informed"] = bool(solar_by_season or solar_by_hour)
    result["solar_source"] = profile_source

    # Default the preset name to the tariff it was derived from, so a user with
    # several plans can tell the generated schedules apart — "Optimised" alone
    # stops meaning anything the moment there are two.
    if result.get("ok") and save_as is None:
        try:
            svc = await db.get_utility_service_for_gateway(short_id) or {}
            plan = (svc.get("tariff_company_name") or svc.get("name") or "").strip()
            save_as = f"{plan} Optimised" if plan else "Optimised"
        except Exception:
            save_as = "Optimised"
    result["suggested_name"] = save_as

    if result.get("ok") and save_as:
        mgr = _get_presets_mgr()
        saved = mgr.save_preset(
            save_as,
            f"Generated from tariff — objective: {objective}"
            + ("" if solar_by_hour else " (no solar forecast available)"),
            result["schedule"],
            unverified=True,   # generated, not yet run — keep it out of the
                               # MQTT select until a human has looked at it
        )
        result["saved_as"] = save_as
        result["save_result"] = saved

    return result


@router.post("/gateways/schedule/presets/{name}/load")
async def load_schedule_preset(name: str):
    """Load a preset by name and return its full schedule list."""
    mgr = _get_presets_mgr()
    result = mgr.load_preset(name)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Preset not found."))
    # Loading only populates the editor — Save to Gateway is what reaches the
    # battery — but it is the step that decides what gets pushed, so it belongs
    # in the trail.
    await db.log_admin_audit(
        event="Schedule:Preset_Loaded", source="ui",
        details=f"Preset {name!r} loaded into the editor "
                f"({len(result.get('schedule') or [])} blocks). Not yet applied.",
    )
    return result


@router.delete("/gateways/schedule/presets/{name}")
async def delete_schedule_preset(name: str):
    """Delete a preset by name."""
    mgr = _get_presets_mgr()
    result = mgr.delete_preset(name)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Preset not found."))
    await db.log_admin_audit(
        event="Schedule:Preset_Deleted", source="ui",
        details=f"Preset {name!r} deleted",
    )
    return result


# ── TOU Snapshot / Audit Trail API ─────────────────────────────────────

@router.get("/gateways/{short_id}/schedule/snapshots")
async def list_tou_snapshots(short_id: str, limit: int = 50, offset: int = 0):
    """Push history for a gateway, newest first.

    Paged. The previous hard cap of 10 meant a gateway with 93 snapshots
    offered ten of them and the rest were reachable only by querying the
    database for an id — a restore point you cannot find is not a restore
    point.
    """
    rows = await db.get_tou_snapshots(short_id, limit=limit, offset=offset)
    total = await db.count_tou_snapshots(short_id)
    return {
        "ok": True,
        "snapshots": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/gateways/{short_id}/schedule/snapshots/{snapshot_id}")
async def delete_tou_snapshot(short_id: str, snapshot_id: int):
    """Delete one snapshot.

    There was no way to remove one at all. Audited, because deleting a restore
    point is exactly the kind of thing someone wants explained later.
    """
    removed = await db.delete_tou_snapshot(short_id, snapshot_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id} not found for {short_id}")
    await db.log_admin_audit(
        event="Schedule:Snapshot_Deleted", source="ui",
        details=f"[{short_id}] Snapshot {snapshot_id} deleted",
    )
    return {"ok": True, "deleted": snapshot_id}


@router.post("/gateways/{short_id}/schedule/snapshots/prune")
async def prune_tou_snapshots(short_id: str, keep: int = 200):
    """Trim history to the newest `keep` snapshots.

    Manual, not scheduled. These are restore points and the useful one may be
    months old, so nothing should quietly discard them — count-based, invoked
    deliberately, and audited.
    """
    if keep < 1:
        raise HTTPException(status_code=400, detail="keep must be at least 1")
    before = await db.count_tou_snapshots(short_id)
    removed = await db.prune_tou_snapshots(short_id, keep=keep)
    if removed:
        await db.log_admin_audit(
            event="Schedule:Snapshots_Pruned", source="ui",
            details=f"[{short_id}] Pruned {removed} snapshot(s), keeping the newest {keep} of {before}",
        )
    return {"ok": True, "removed": removed, "kept": min(before, keep), "before": before}


@router.post("/gateways/{short_id}/schedule/snapshots/{snapshot_id}/restore")
async def restore_tou_snapshot(short_id: str, snapshot_id: int, payload: dict = None):
    """
    Restore a prior TOU snapshot into the in-memory session for review.
    Does NOT push to the gateway — user must still hit 'Save to Gateway'.
    Writes a 'restore' audit snapshot so restores are traceable.
    """
    snap = await db.get_tou_snapshot_by_id(snapshot_id)
    if not snap or snap.get("short_id") != short_id:
        raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id} not found for gateway {short_id}")
    try:
        strategy_list = json.loads(snap["strategy_json"])
    except Exception:
        raise HTTPException(status_code=500, detail="Snapshot data corrupt")

    # Record the restore event as a new snapshot so it's auditable
    await db.insert_tou_snapshot(
        short_id=short_id,
        strategy_list=strategy_list,
        label=f"Restored from snapshot #{snapshot_id} ({snap['ts']})",
        source="restore",
    )
    return {"ok": True, "strategy_list": strategy_list, "restored_from": snapshot_id}


@router.post("/gateways/{short_id}/schedule/snapshots/{snapshot_id}/save-as-preset")
async def save_snapshot_as_preset(short_id: str, snapshot_id: int, payload: dict = None):
    """Turn a snapshot into a named preset.

    Snapshots and presets held the same schedules in different shapes with no
    way across, so a known-good schedule captured before a change could be
    restored but never promoted into something an automation or the MQTT select
    could switch to. This is that bridge.

    The projection is lossy for a multi-season or multi-day-type snapshot — a
    preset is one flat day. Anything dropped is named in the response, in the
    preset description and in the audit trail rather than left for the user to
    discover on the battery.
    """
    payload = payload or {}
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required.")

    snap = await db.get_tou_snapshot_by_id(snapshot_id)
    if not snap or snap.get("short_id") != short_id:
        raise HTTPException(
            status_code=404,
            detail=f"Snapshot {snapshot_id} not found for gateway {short_id}",
        )
    try:
        strategy_list = json.loads(snap["strategy_json"])
    except Exception:
        raise HTTPException(status_code=500, detail="Snapshot data corrupt")

    result = flatten_snapshot(
        strategy_list,
        season=payload.get("season"),
        day_type=payload.get("day_type"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "Cannot convert snapshot."))

    summary = describe_projection(result)
    description = (payload.get("description") or "").strip() or (
        f"From snapshot #{snapshot_id} ({snap['ts']}) — {summary}"
    )

    # A narrowed schedule has never run in that form, so it stays out of the
    # MQTT select until a human has looked at it. An exact one-season,
    # one-day-type snapshot did run on the gateway, so it does not.
    mgr = _get_presets_mgr()
    saved = mgr.save_preset(name, description, result["blocks"], unverified=result["lossy"])

    await db.log_admin_audit(
        event="Schedule:Preset_Saved_From_Snapshot", source="ui",
        details=f"Preset {name!r} created from snapshot #{snapshot_id} "
                f"({snap['ts']}) — {summary}"
                + (" (unverified: narrowed)" if result["lossy"] else ""),
    )
    return {
        "ok": True,
        "preset": name,
        "from_snapshot": snapshot_id,
        "summary": summary,
        "save_result": saved,
        **{k: result[k] for k in
           ("blocks", "season", "day_type", "dropped_seasons", "dropped_day_types", "lossy")},
    }


@router.post("/gateways/{short_id}/schedule/calculate-savings")
async def calculate_tou_savings(short_id: str, payload: dict):
    """
    Proxy to FranklinWH calculate_expected_earnings — READ ONLY, no gateway write.
    Returns estimated monthly and annual savings for the provided strategyList + rates.
    Payload: { "strategy_list": [...] }
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")

    gw_svc = registry.get_gateway(short_id)
    if not gw_svc or not getattr(gw_svc, "_client", None):
        raise HTTPException(status_code=503, detail="Gateway client offline")

    strategy_list = payload.get("strategy_list", [])
    try:
        # Build the template payload expected by calculate_expected_earnings
        template = {
            "strategyList": strategy_list,
        }
        result = await gw_svc._client.calculate_expected_earnings(template)
        # Normalise the response: API returns estimatedSavings30 / estimatedSavings365
        raw = result.get("result") or result
        savings = {
            "savings30":  float(raw.get("estimatedSavings30", 0) or 0),
            "savings365": float(raw.get("estimatedSavings365", 0) or 0),
        }
        return {"ok": True, "data": savings}
    except AttributeError:
        # Library version may not expose calculate_expected_earnings
        raise HTTPException(status_code=501, detail="calculate_expected_earnings not available in this library version")
    except Exception as exc:
        logger.warning(f"[{short_id}] calculate_expected_earnings failed: {exc}")
        raise HTTPException(status_code=502, detail=f"FranklinWH API error: {exc}")


# ── TOU Execution Health Check ──────────────────────────────────────────────

@router.get("/gateways/{short_id}/schedule/health")
async def gateway_tou_health(short_id: str, strict: bool = False):
    """
    TOU Execution Health Check — read-only.

    Cross-references three existing API calls to determine whether the gateway
    is actively executing the current TOU schedule block as expected:
      1. getGatewayTouListV2  — active mode, cloud sync status (touSendStatus/touAlertMessage)
      2. get_tou_info(1)      — active schedule block + expected dispatch action
      3. last_data from poll  — live run_status + grid_kw (no extra API call)

    Returns a structured health dict with health_status:
      HEALTHY  — gateway executing as scheduled
      DEGRADED — partial execution (SOC guard, stopMode, partial grid flow)
      FAULT    — clear mismatch between scheduled action and actual run_status
      UNKNOWN  — gateway not in TOU mode, or no schedule block found for today

    Query params:
      strict (bool) — if True, Standby during GRID_CHARGE/GRID_EXPORT = FAULT
                      default False (= DEGRADED, acknowledges SOC guards)
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")

    gw_svc = registry.get_gateway(short_id)
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running — start it from the Gateways tab first.")

    client = getattr(gw_svc, "_client", None)
    if not client:
        raise HTTPException(status_code=503, detail="Gateway client not initialised")

    # Pass last_data from the poll cache so get_tou_health() skips the get_stats() call
    live_stats = gw_svc.status.last_data if gw_svc.status else None

    try:
        result = await client.get_tou_health(live_stats=live_stats, strict=strict)
        return result
    except Exception as exc:
        logger.error(f"[{short_id}] get_tou_health failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/gateways/{short_id}/schedule/health/reset")
async def gateway_tou_reset(short_id: str, payload: dict = None):
    """
    TOU Mode Reset — write operation, requires explicit user confirmation.

    Performs a deliberate Self-Consumption → Time-of-Use mode toggle to force
    the gateway firmware to re-read its local TOU schedule database and restart
    schedule execution.

    Only call after presenting the fault to the user and receiving confirmation.

    Payload: { "confirmed": true, "min_soc_pct": 10 }
      confirmed   — REQUIRED: must be true; prevents accidental triggers
      min_soc_pct — optional SOC guard threshold (default: 10%)
    """
    payload = payload or {}
    if not payload.get("confirmed"):
        raise HTTPException(
            status_code=400,
            detail="Reset requires explicit confirmation. Send { \"confirmed\": true } in the request body."
        )

    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")

    gw_svc = registry.get_gateway(short_id)
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running — start it from the Gateways tab first.")

    client = getattr(gw_svc, "_client", None)
    if not client:
        raise HTTPException(status_code=503, detail="Gateway client not initialised")

    min_soc             = int(payload.get("min_soc_pct", 10))
    max_verify_attempts = int(payload.get("max_verify_attempts", 4))
    verify_interval_s   = int(payload.get("verify_interval_s", 15))
    try:
        # Initial audit log for the reset attempt
        await db.log_admin_audit(
            event="Gateway:TOU_Reset:Started",
            source=f"API:Phase9:{short_id}",
            user="system",
            details=f"Initiating manual TOU mode reset toggle. min_soc={min_soc}%, max_attempts={max_verify_attempts}"
        )

        result = await client.reset_tou_mode(
            min_soc_pct=min_soc,
            max_verify_attempts=max_verify_attempts,
            verify_interval_s=verify_interval_s,
        )

        if not result.get("ok"):
            # Log the failure in audit log
            await db.log_admin_audit(
                event="Gateway:TOU_Reset:Failed",
                source=f"API:Phase9:{short_id}",
                user="system",
                details=f"Reset rejected or failed: {result.get('error', 'Unknown error')}"
            )
            # Reset rejected (SOC guard or step failure) — return 400 with detail
            raise HTTPException(status_code=400, detail=result.get("error", "Reset failed"))

        sync_cleared = result.get("sync_cleared", False)
        logger.info(
            f"[{short_id}] TOU mode reset completed. sync_cleared={sync_cleared}. "
            f"final_send_status={result.get('final_send_status')}. "
            f"Steps: {result['steps']}"
        )

        # Log successful completion in audit log
        await db.log_admin_audit(
            event="Gateway:TOU_Reset:Success",
            source=f"API:Phase9:{short_id}",
            user="system",
            details=f"TOU mode reset completed successfully. sync_cleared={sync_cleared}. Steps: {len(result['steps'])}"
        )

        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[{short_id}] reset_tou_mode failed: {exc}")
        await db.log_admin_audit(
            event="Gateway:TOU_Reset:Error",
            source=f"API:Phase9:{short_id}",
            user="system",
            details=f"Unexpected error during TOU reset: {exc}"
        )
        raise HTTPException(status_code=500, detail=str(exc))


# ── Ph-2a: Tariff Profile endpoint ──────────────────────────────────────────

@router.get("/gateways/{short_id}/tariff-profile")
async def gateway_tariff_profile(short_id: str):
    """Return hardware-validated TOU plan summary for a gateway.

    Reads from the in-memory last_data.tou_schedule cache — no live Cloud API
    call required.  Runs the full TOU validation suite and returns both raw
    metadata and a structured validation result.

    Used by the Utility Service modal (Section A — FranklinWH TOU Profile).
    """
    from src.services.tou_validator import validate_tou_plan

    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    status   = registry.get_status(short_id) if registry else None

    # Try cached last_data first — fastest path, no Cloud API call
    tou_schedule = None
    source       = "unavailable"
    if status:
        last_data    = status.get("last_data") or {}
        tou_schedule = last_data.get("tou_schedule")
        if tou_schedule:
            source = "cached"

    # Fall back to live fetch if not cached yet
    if not tou_schedule:
        gw_svc = registry.get_gateway(short_id) if registry else None
        if gw_svc:
            try:
                raw = await gw_svc.get_tou_schedule()
                if raw.get("ok"):
                    tou_schedule = (raw.get("detail") or {})
                    source       = "live"
            except Exception as exc:
                logger.warning(f"[{short_id}] tariff-profile live fetch failed: {exc}")

    validation = validate_tou_plan(tou_schedule)

    # Also pull profile_json for site context
    profile: dict = json.loads(gw.get("profile_json") or "{}")

    return {
        "ok":              True,
        "gateway_id":      short_id,
        "source":          source,
        "fwh_site_id":     profile.get("site_id") or gw.get("site_id"),
        "fwh_site_name":   profile.get("site_name", ""),
        "validation":      validation,
        # Convenience top-level fields for UI
        "tariff_type":          validation["tariff_type"],
        "tariff_type_label":    validation["tariff_type_label"],
        "nem_type":             validation["nem_type"],
        "nem_type_label":       validation["nem_type_label"],
        "season_count":         validation["season_count"],
        "tariff_company":       validation["tariff_company"],
        "tariff_company_id":    validation["tariff_company_id"],
        "valid":                validation["valid"],
        "issues":               validation["issues"],
    }


# ── Ph-2b: VPP Status endpoint ────────────────────────────────────────────

@router.get("/gateways/{short_id}/vpp-status")
async def gateway_vpp_status(short_id: str):
    """Return VPP (Virtual Power Plant) enrolment status for a gateway.

    Calls get_programme_info() — the authoritative FranklinWH source for VPP
    programme enrolment. Amber and LocalVolts are pricing providers only and
    are NOT FWH VPP participants. The utility company is typically the VPP
    programme provider.

    Note: get_bonus_info() is for TOU bonus/incentive info and belongs in
    the Tariff Profile section (Section A), not here.
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    registry = get_app_state().get("registry")
    gw_svc   = registry.get_gateway(short_id) if registry else None
    if not gw_svc:
        raise HTTPException(status_code=400, detail="Gateway not running — start it first.")

    try:
        client = await gw_svc._get_or_create_client()
        raw    = await client.get_programme_info()
        result = raw.get("result") or raw

        # flag=0 means not enrolled; programId present or flag!=0 means enrolled
        flag       = result.get("flag", 0) or 0
        program_id = result.get("programId")
        enrolled   = bool(program_id or flag != 0)

        return {
            "ok":           True,
            "flag":         flag,
            "vpp_enrolled": enrolled,
            "program_id":   program_id,
            "program_name": result.get("programName"),
            "partner_name": result.get("partnerName"),
            "partner_id":   result.get("partnerId"),
            "postcode":     result.get("postcode"),
            "province":     result.get("province"),
            "city":         result.get("city"),
            "register_start": result.get("registerStartDate"),
            "register_end":   result.get("registerEndDate"),
            "show_banner":    result.get("showRegisterBanner", False),
        }
    except Exception as exc:
        logger.warning(f"[{short_id}] get_programme_info failed: {exc}")
        return {"ok": False, "error": str(exc), "vpp_enrolled": None}
