"""
api_smart_dispatch.py — REST endpoints for the Smart Dispatch Engine.

Endpoints:
  GET  /api/smart_dispatch/config       — read engine config (with defaults)
  PUT  /api/smart_dispatch/config       — save engine config
  GET  /api/smart_dispatch/eval         — latest evaluation decision
  POST /api/smart_dispatch/eval/force   — trigger immediate re-evaluation
  GET  /api/smart_dispatch/log          — paginated eval log (last 200)
  GET  /api/smart_dispatch/earnings     — daily + monthly earnings summary
  GET  /api/smart_dispatch/forecast     — 24h rule-based decision map per interval
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from datetime import datetime, timezone, timedelta
from src.services import db
from src.services.earnings_tracker import get_earnings_summary
from src.services.pin_service import generate_pin, validate_pin_or_session, validate_session_token
from src.app_state import get_app_state

logger = logging.getLogger(__name__)

# Bump this whenever the engine colour/category logic changes so the client
# can detect stale cached slots and immediately re-render.
FORECAST_ENGINE_VERSION = "4"   # incremented: negative_export_advisory category added

router = APIRouter(tags=["smart-dispatch"])


# ── Request / Response models ─────────────────────────────────────────────────

class SDUnlockRequest(BaseModel):
    pin: str = ""
    session_token: Optional[str] = None
    gateway_id: str = "global"

class EngineConfigUpdate(BaseModel):
    strategy_mode:       Optional[str] = Field(None, pattern="^(disabled|info|auto|user_approval|passive|active|proactive)$")
    min_soc:             Optional[float] = Field(None, ge=0, le=100)
    max_soc:             Optional[float] = Field(None, ge=0, le=100)
    max_charge_price:    Optional[float] = Field(None, ge=0)
    min_export_price:    Optional[float] = Field(None, ge=-50) # allow negative in case they want a negative floor
    export_bonus_threshold:    Optional[float] = Field(None, ge=0)
    daily_earnings_target:     Optional[float] = Field(None, ge=0)
    monthly_earnings_target:   Optional[float] = Field(None, ge=0)
    solar_curtail_entity:           Optional[str]   = None
    allow_auto_offgrid:        Optional[int]   = Field(None, ge=0, le=1)
    notification_mode:         Optional[str]   = Field(None, pattern="^(ask|auto|silent)$")
    notify_on_demand_charge:   Optional[int]   = Field(None, ge=0, le=1)
    notify_on_negative_export: Optional[int]   = Field(None, ge=0, le=1)
    notify_on_spike:           Optional[int]   = Field(None, ge=0, le=1)
    notify_on_export_bonus:    Optional[int]   = Field(None, ge=0, le=1)
    notify_on_earnings:        Optional[int]   = Field(None, ge=0, le=1)
    notify_on_force_charge:    Optional[int]   = Field(None, ge=0, le=1)
    info_notify_targets:       Optional[str]   = None
    weather_load_influence:    Optional[bool]  = None
    min_peak_window_soc:       Optional[float] = Field(None, ge=0, le=100)
    max_peak_window_soc:       Optional[float] = Field(None, ge=0, le=100)
    shadow_mode:               Optional[int]   = Field(None, ge=0, le=1)
    weather_extreme_impact:    Optional[int]   = Field(None, ge=0, le=1)
    site_has_high_loads:       Optional[int]   = Field(None, ge=0, le=1)
    multi_utility_service:     Optional[int]   = Field(None, ge=0, le=1)
    utility_export_limit_w:    Optional[int]   = Field(None, ge=0)
    has_apbox_excess_solar:    Optional[int]   = Field(None, ge=0, le=1)
    apower_s_mppt:             Optional[int]   = Field(None, ge=0, le=1)
    strategy_priorities_json:  Optional[str]   = None
    last_full_generation_time: Optional[str]   = None
    default_operating_mode:    Optional[str]   = None
    # rampTime constraint relaxed 2026-06-17: 99 is a sentinel value used
    # throughout the codebase to mark HEMS_OVERRIDE_ACTIVE (see db.py:512
    # default, scheduler_core.py:550 fallback, gateway_service.py:595,602
    # checks). The UI slider lets users pick 1-60; the sentinel 99 comes
    # from the system. The old `le=60` rejected the sentinel value, which
    # caused every save to fail 422 with "Input should be less than or
    # equal to 60" — even when the user changed nothing.
    rampTime:                  Optional[int]   = Field(None, ge=1, le=99)
    maxChargeSoc:              Optional[int]   = Field(None, ge=20, le=100)
    minDischargeSoc:           Optional[int]   = Field(None, ge=0, le=100)
    chargePower:               Optional[int]   = Field(None, ge=0)
    dischargePower:            Optional[int]   = Field(None, ge=0)
    # Fields the UI sends but the schema previously didn't declare.
    # Pydantic dropped them on save (data-loss). Added 2026-06-17 to align
    # the schema with what tabs/smart_dispatch.html actually posts.
    # All have matching columns in smart_dispatch_config — see db.py:475-478,
    # the v38 migration block, and the lookahead_minutes ALTER at db.py:1077.
    lookahead_minutes:         Optional[int]   = Field(None, ge=0, le=180)
    actionable_rate_limit:     Optional[int]   = Field(None, ge=1, le=12)
    charge_power_mode:         Optional[str]   = None
    charge_power_value:        Optional[float] = Field(None, ge=0)
    discharge_power_mode:      Optional[str]   = None
    discharge_power_value:     Optional[float] = Field(None, ge=0)
    no_reply_action:           Optional[str]   = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _daily_outlook(solar_slots: list, price_slots: list) -> list[dict]:
    """Per-day solar and what the tariff makes of it.

    The slot list is keyed to TOU blocks, so a seven-hour block carries a
    single point sample of solar — fine for deciding that block, useless for
    seeing what the weather is doing. This summarises the solar series itself,
    which is half-hourly, so the day-to-day swing is visible.

    That swing is the point. On this site the forecast runs eight days from
    6.9 to 39.9 kWh — a 5.8x spread — and a schedule that looks comfortable
    against a 40 kWh day does not survive a 7 kWh one. Nothing in the UI showed
    that, because the preview stopped at 26 hours.
    """
    from collections import defaultdict
    from datetime import datetime

    solar_kwh: dict[str, float] = defaultdict(float)
    for slot in solar_slots or []:
        try:
            when = datetime.fromisoformat(slot["timestamp"]).astimezone()
            period_h = float(slot.get("period_mins") or 30) / 60.0
            solar_kwh[when.date().isoformat()] += float(slot.get("pv_kw") or 0.0) * period_h
        except (KeyError, TypeError, ValueError):
            continue

    if not solar_kwh:
        return []

    # Cheapest import seen in the projection, as the yardstick for what a day
    # of poor generation is likely to cost to cover.
    imports = [float(p.get("import_c") or 0.0) for p in (price_slots or [])]
    cheapest_import_c = min([i for i in imports if i > 0], default=0.0)

    days = sorted(solar_kwh)
    best = max(solar_kwh.values())

    outlook = []
    for day in days:
        generated = round(solar_kwh[day], 2)
        shortfall = max(0.0, best - generated)
        outlook.append({
            "date": day,
            "solar_kwh": generated,
            # Against the best day in the window, not against demand — this is
            # "how much worse is this day", not a bill estimate, and saying so
            # is better than implying a precision the data does not support.
            "shortfall_vs_best_kwh": round(shortfall, 2),
            "shortfall_cost_aud": round(shortfall * cheapest_import_c / 100.0, 2)
            if cheapest_import_c else None,
            "relative": round(generated / best, 3) if best else None,
        })
    return outlook


def cluster_epoch_segments(slots: list[dict]) -> list[dict]:
    """Cluster contiguous 30-min forecast slots into descriptive chronological periods."""
    if not slots:
        return []
        
    segments = []
    current_segment = None
    
    from datetime import datetime
    
    for slot in slots:
        # Peak check: demand window, spike status, or high import price (> 35c)
        is_peak = bool(slot.get("demand_window")) or slot.get("spike_status") in ("SPIKE", "DEVELOPING") or float(slot.get("import_c", 0.0)) > 35.0
        is_solar = float(slot.get("pv_kw", 0.0)) > 0.05
        
        category = "PEAK" if is_peak else ("SOLAR" if is_solar else "OFFPEAK")
        
        if current_segment is None or current_segment["category"] != category:
            # Save previous segment
            if current_segment:
                import_prices = [s.get("import_c", 0.0) for s in current_segment["slots"]]
                export_prices = [s.get("export_c", 0.0) for s in current_segment["slots"]]
                pv_kws = [s.get("pv_kw", 0.0) for s in current_segment["slots"]]
                load_kws = [s.get("home_load_kw", 0.0) for s in current_segment["slots"]]
                
                est_batt_delta = sum((s.get("charge_kw", 0.0) - s.get("discharge_kw", 0.0)) * 0.5 for s in current_segment["slots"])
                
                current_segment["avg_import_c"] = round(sum(import_prices) / len(import_prices), 1)
                current_segment["avg_export_c"] = round(sum(export_prices) / len(export_prices), 1)
                current_segment["avg_pv_kw"] = round(sum(pv_kws) / len(pv_kws), 2)
                current_segment["avg_load_kw"] = round(sum(load_kws) / len(load_kws), 2)
                current_segment["est_battery_kwh"] = round(est_batt_delta, 1)
                
                first_start = datetime.fromisoformat(current_segment["slots"][0]["start"]).astimezone()
                last_end = datetime.fromisoformat(current_segment["slots"][-1]["end"]).astimezone()
                current_segment["time_label"] = f"{first_start.strftime('%I:%M %p')} - {last_end.strftime('%I:%M %p')}".replace(" 0", " ")
                segments.append(current_segment)
                
            # Create new segment
            label = "🔴 Peak Demand Period" if category == "PEAK" else ("☀️ Daytime Solar Period" if category == "SOLAR" else "🌙 Off-Peak / Evening Period")
            current_segment = {
                "category": category,
                "label": label,
                "slots": [slot],
            }
        else:
            current_segment["slots"].append(slot)
            
    # Save the last segment
    if current_segment:
        import_prices = [s.get("import_c", 0.0) for s in current_segment["slots"]]
        export_prices = [s.get("export_c", 0.0) for s in current_segment["slots"]]
        pv_kws = [s.get("pv_kw", 0.0) for s in current_segment["slots"]]
        load_kws = [s.get("home_load_kw", 0.0) for s in current_segment["slots"]]
        
        est_batt_delta = sum((s.get("charge_kw", 0.0) - s.get("discharge_kw", 0.0)) * 0.5 for s in current_segment["slots"])
        
        current_segment["avg_import_c"] = round(sum(import_prices) / len(import_prices), 1)
        current_segment["avg_export_c"] = round(sum(export_prices) / len(export_prices), 1)
        current_segment["avg_pv_kw"] = round(sum(pv_kws) / len(pv_kws), 2)
        current_segment["avg_load_kw"] = round(sum(load_kws) / len(load_kws), 2)
        current_segment["est_battery_kwh"] = round(est_batt_delta, 1)
        
        first_start = datetime.fromisoformat(current_segment["slots"][0]["start"]).astimezone()
        last_end = datetime.fromisoformat(current_segment["slots"][-1]["end"]).astimezone()
        current_segment["time_label"] = f"{first_start.strftime('%I:%M %p')} - {last_end.strftime('%I:%M %p')}".replace(" 0", " ")
        segments.append(current_segment)
        
    return segments




# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/generate-pin")
async def sd_generate_pin():
    """
    Generate a fresh OTP PIN for unlocking the smart dispatch rules editor.
    Returns the raw 6-digit PIN.
    """
    raw_pin = await generate_pin("smart_dispatch")
    return {"pin": raw_pin, "ttl_minutes": 10}


@router.post("/unlock")
async def sd_unlock(req: SDUnlockRequest):
    """
    Unlock system protective rules. Requires a valid UI PIN or active 24h session token.
    On success: creates a secure override session in DB and returns it.
    """
    valid, result = await validate_pin_or_session("smart_dispatch", req.pin, req.session_token)
    if not valid:
        raise HTTPException(status_code=403, detail=result)

    # PIN validated or session accepted. Now record it in sd_security_sessions
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_iso = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    
    await db.create_security_session(
        gateway_id=req.gateway_id,
        unlocked_at=now_iso,
        expires_at=expires_iso,
        token=result
    )

    logger.info(f"Smart Dispatch safeguards unlocked for gateway '{req.gateway_id}' via UI PIN")
    return {
        "ok": True,
        "message": "Smart Dispatch system safeguards unlocked",
        "session_token": result,
        "expires_at": expires_iso
    }


@router.get("/unlock/status")
async def sd_unlock_status(gateway_id: str = "global"):
    """
    Return whether smart dispatch safeguards are currently unlocked.
    """
    session = await db.get_active_security_session(gateway_id)
    if not session:
        return {"unlocked": False, "seconds_remaining": 0, "expires_at": None}

    try:
        expires_at = datetime.fromisoformat(session["expires_at"])
        now = datetime.now(timezone.utc)
        remaining = int((expires_at - now).total_seconds())
        return {
            "unlocked": True,
            "seconds_remaining": max(0, remaining),
            "expires_at": session["expires_at"]
        }
    except Exception:
        return {"unlocked": False, "seconds_remaining": 0, "expires_at": None}


@router.post("/lock")
async def sd_lock(req: SDUnlockRequest):
    """
    Manually lock system protective rules, deleting the active session.
    """
    gateway_id = req.gateway_id or "global"
    if gateway_id == "all":
        gateway_id = "global"
    await db.delete_active_security_sessions(gateway_id)
    # Trigger reversion immediately so the database/rules are cleaned up straight away
    await db.check_and_trigger_session_reversion(gateway_id)
    logger.info(f"Smart Dispatch safeguards manually locked for gateway '{gateway_id}'")
    return {"ok": True, "message": "Smart Dispatch system safeguards locked"}



@router.get("/config")
async def get_engine_config(gateway_id: str = "global"):
    """Return engine configuration for a gateway (defaults if unconfigured)."""
    cfg = await db.get_smart_dispatch_config(gateway_id)
    return {"ok": True, "gateway_id": gateway_id, "config": cfg}


@router.put("/config")
async def update_engine_config(body: EngineConfigUpdate, gateway_id: str = "global"):
    """Persist engine configuration for a gateway."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": True, "gateway_id": gateway_id, "updated": 0}
        
    # Synchronize notification_mode and strategy_mode
    if "notification_mode" in updates and "strategy_mode" not in updates:
        notif_mode = updates["notification_mode"]
        if notif_mode == "ask":
            updates["strategy_mode"] = "user_approval"
        elif notif_mode == "auto":
            updates["strategy_mode"] = "auto"
        elif notif_mode == "silent":
            updates["strategy_mode"] = "info"
            
    elif "strategy_mode" in updates and "notification_mode" not in updates:
        strat_mode = updates["strategy_mode"]
        if strat_mode == "user_approval":
            updates["notification_mode"] = "ask"
        elif strat_mode == "auto":
            updates["notification_mode"] = "auto"
        elif strat_mode in ("info", "disabled", "passive"):
            updates["notification_mode"] = "silent"

    await db.upsert_smart_dispatch_config(gateway_id, **updates)

    # ── Auto-enable global actionable when switching to user_approval ────────
    # There's no coherent combination of strategy_mode='user_approval' (or
    # notification_mode='ask') AND notification_settings.actionable=0 — the
    # user_approval flow's whole purpose is to prompt for approval, and the
    # FWH_APPROVE / FWH_SKIP buttons are the mechanism. If they're disabled,
    # user_approval mode sends bare text and does nothing actionable.
    # This block flips actionable=1 to keep the two settings coherent.
    # We do NOT auto-disable it on other mode transitions — users may keep
    # the buttons armed for future modes. Root cause of P1 cluster #4,
    # fixed 2026-07-08.
    if updates.get("strategy_mode") == "user_approval" or updates.get("notification_mode") == "ask":
        try:
            notif = await db.get_notification_settings() or {}
            if not int(notif.get("actionable", 0)):
                await db.upsert_notification_settings({
                    "enabled":        bool(notif.get("enabled", False)),
                    "ha_target":      notif.get("ha_target", "") or "",
                    "triggers":       notif.get("triggers", []) or [],
                    "actionable":     True,
                    "actionable_ttl": int(notif.get("actionable_ttl", 1800) or 1800),
                })
                logger.info(
                    f"update_engine_config: strategy→user_approval; "
                    f"auto-enabled notification_settings.actionable=1 for coherence"
                )
        except Exception as exc:
            logger.warning(f"update_engine_config: could not auto-enable actionable — {exc}")

    cfg = await db.get_smart_dispatch_config(gateway_id)
    return {"ok": True, "gateway_id": gateway_id, "config": cfg, "updated": len(updates)}


class ScheduleCreate(BaseModel):
    gateway_id: str
    period: str
    start_time: str
    end_time: str
    min_soc: float
    max_kw_percent: float | None = None
    action: str = "CHARGE"

@router.get("/schedules")
async def get_schedules(gateway_id: str):
    """Get time-based schedules for a gateway (includes global)."""
    schedules = await db.get_smart_dispatch_schedules(gateway_id)
    return {"ok": True, "schedules": schedules}

@router.post("/schedules")
async def add_schedule(body: ScheduleCreate):
    """Add a new time-based schedule."""
    schedule_id = await db.add_smart_dispatch_schedule(
        body.gateway_id, body.period, body.start_time, body.end_time, body.min_soc, body.max_kw_percent, body.action
    )
    return {"ok": True, "id": schedule_id}

@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """Delete a time-based schedule."""
    deleted = await db.delete_smart_dispatch_schedule(schedule_id)
    return {"ok": deleted}


# ── Strategy Matrix (Phase 4) ──────────────────────────────────────────────────

@router.get("/matrix")
async def get_strategy_matrix(gateway_id: str = "all"):
    """Fetch the strategy matrix rows."""
    rows = await db.get_sd_strategy_matrix(gateway_id, include_disabled=True)
    return {"ok": True, "rows": rows}


@router.post("/matrix")
async def add_strategy_row(row: dict, request: Request):
    """Add or update a strategy row."""
    row_id = row.get("id")
    if row_id:
        # Fetch existing row for diffing and locking checks
        old_row = await db.get_sd_strategy_row(row_id)
        if not old_row:
            raise HTTPException(status_code=404, detail="Strategy row not found.")

        if old_row.get("system_immutable") == 1:
            # Locked system safeguard — check if non-toggle fields changed
            locked_fields = ["strategy_name", "trigger_category", "conditions_json", "signals_json", "eval_order"]
            has_locked_changes = False
            for field in locked_fields:
                if row.get(field) is not None and row.get(field) != old_row.get(field):
                    has_locked_changes = True
                    break

            if has_locked_changes:
                # Check for bypass token in payload or header
                session_token = row.get("session_token") or request.headers.get("x-session-token")
                gateway_id = old_row.get("gateway_id") or "global"
                if gateway_id == "all":
                    gateway_id = "global"

                is_unlocked = False
                if session_token:
                    active_session = await db.get_active_security_session(gateway_id)
                    if active_session and active_session["session_token"] == session_token:
                        is_unlocked = True

                if not is_unlocked:
                    raise HTTPException(
                        status_code=400,
                        detail="System protective safeguards are locked. Please provide a valid session PIN token to override."
                    )

            # Ensure system_immutable remains 1
            row["system_immutable"] = 1
        
        saved_id = await db.upsert_sd_strategy_row(row)
        
        changes = []
        if old_row.get("strategy_name") != row.get("strategy_name"):
            changes.append(f"name: '{old_row.get('strategy_name')}' -> '{row.get('strategy_name')}'")
        if old_row.get("trigger_category") != row.get("trigger_category"):
            changes.append(f"category: '{old_row.get('trigger_category')}' -> '{row.get('trigger_category')}'")
        
        old_enabled = int(old_row.get("enabled", 1))
        new_enabled = int(row.get("enabled", 1))
        if old_enabled != new_enabled:
            action_str = "Resumed" if new_enabled == 1 else "Paused"
            changes.append(f"state: {action_str} (enabled: {old_enabled} -> {new_enabled})")
        
        if old_row.get("eval_order") != row.get("eval_order"):
            changes.append(f"priority: {old_row.get('eval_order')} -> {row.get('eval_order')}")
        if old_row.get("forecast_weight") != row.get("forecast_weight"):
            changes.append(f"forecast_weight: {old_row.get('forecast_weight')} -> {row.get('forecast_weight')}")
        if old_row.get("intent_duration_mins") != row.get("intent_duration_mins"):
            changes.append(f"intent_duration_mins: {old_row.get('intent_duration_mins')} -> {row.get('intent_duration_mins')}")
        if old_row.get("conditions_json") != row.get("conditions_json"):
            changes.append("conditions_json modified")
        if old_row.get("signals_json") != row.get("signals_json"):
            changes.append("signals_json modified")
        
        if changes:
            change_detail = ", ".join(changes)
            event_type = "MIXER_MOD"
            if len(changes) == 1 and "state:" in changes[0]:
                event_type = "MIXER_RESUME" if new_enabled == 1 else "MIXER_PAUSE"
            
            await db.log_admin_audit(
                event=event_type,
                source="smart_dispatch",
                user="admin",
                details=f"Modified strategy rule '{row.get('strategy_name')}' ID {row_id}: {change_detail}"
            )
    else:
        # Adding a new row — force system_immutable to 0 so users cannot create locked system rules
        row["system_immutable"] = 0
        saved_id = await db.upsert_sd_strategy_row(row)
        await db.log_admin_audit(
            event="MIXER_ADD",
            source="smart_dispatch",
            user="admin",
            details=f"Added strategy rule '{row.get('strategy_name')}' ID {saved_id} (category: {row.get('trigger_category')}, enabled: {row.get('enabled', 1)})"
        )
    return {"ok": True, "id": saved_id}


@router.delete("/matrix/{row_id}")
async def delete_strategy_row(row_id: int):
    """Delete a strategy row."""
    old_row = await db.get_sd_strategy_row(row_id)
    if not old_row:
        raise HTTPException(status_code=404, detail="Strategy row not found.")

    if old_row.get("system_immutable") == 1:
        raise HTTPException(
            status_code=400,
            detail="System protective safeguard rules are locked and cannot be deleted."
        )

    await db.delete_sd_strategy_row(row_id)
    await db.log_admin_audit(
        event="MIXER_DEL",
        source="smart_dispatch",
        user="admin",
        details=f"Deleted strategy rule '{old_row.get('strategy_name')}' ID {row_id} (category: {old_row.get('trigger_category')})"
    )
    return {"ok": True}


# ── Actuators ───────────────────────────────────────────────────────────────

@router.get("/actuators")
async def get_actuators():
    """Fetch the actuator mappings."""
    rows = await db.get_sd_actuators()
    return {"ok": True, "rows": rows}


@router.post("/actuators")
async def update_actuator(row: dict):
    """Update an actuator mapping."""
    signal = await db.upsert_sd_actuator(row)
    return {"ok": True, "sd_signal": signal}


@router.get("/eval")
async def get_latest_eval(gateway_id: str, mock_multi_gateway: int = 0):
    """Return the most recent engine evaluation decision and fleet status."""
    row = await db.get_latest_pricing_eval(gateway_id)
    
    fleet = []
    if row:
        import copy
        import json as _json
        try:
            row["shadowed_rules"] = _json.loads(row.get("shadowed_rules_json") or "[]")
        except:
            row["shadowed_rules"] = []
            
        try:
            cfg = await db.get_smart_dispatch_config(gateway_id)
            
            params = []
            soc = row.get("soc_pct")
            if soc is None:
                try:
                    registry = get_app_state().get("registry")
                    if registry:
                        live = registry.get_status(gateway_id)
                        if live and live.get("last_data"):
                            soc = live["last_data"].get("battery_soc")
                except Exception:
                    pass
            soc = soc if soc is not None else 0.0

            import_c = row.get("import_c_kwh") or 0.0
            export_c = row.get("export_c_kwh") or 0.0

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

            # 4. Export Bonus
            bonus = cfg.get("export_bonus_threshold", 0.0)
            if bonus != 0.0:
                status = "passed" if export_c >= bonus else "failed"
                desc = f"Export price ({export_c:.1f}¢) {'≥' if export_c >= bonus else '<'} Bonus Threshold ({bonus:.1f}¢)"
                params.append({"param": "Export Bonus", "value": f"{bonus}¢", "status": status, "desc": desc, "icon": "fa-sun"})

            # 5. Target Max SOC
            max_soc = cfg.get("max_soc", 90.0)
            status = "passed" if soc <= max_soc else "failed"
            desc = f"Current SOC ({soc:.0f}%) {'≤' if soc <= max_soc else '>'} Maximum ({max_soc:.0f}%)"
            params.append({"param": "Target Max SOC", "value": f"{max_soc}%", "status": status, "desc": desc, "icon": "fa-battery-full"})
            
            row["evaluated_params"] = params
        except Exception as e:
            row["evaluated_params"] = []
            
        # Base real gateway
        soc = row.get("soc_pct") or 50  # Provide default if missing
        fleet.append({
            "gateway_id": gateway_id,
            "name": f"Gateway ({gateway_id[-4:]}) Phase A",
            "soc_pct": soc,
            "decision": row
        })
        
        if mock_multi_gateway == 1:
            # Phase B Mock - High SOC, Exporting
            dec_b = copy.deepcopy(row)
            dec_b["soc_pct"] = 98
            dec_b["action"] = "GRID_EXPORT"
            dec_b["trigger_category"] = "export_bonus"
            dec_b["dispatch_summary"] = "High SOC — exporting to capitalize on bonus."
            fleet.append({
                "gateway_id": "mock_gw_b",
                "name": "Phase B Gateway",
                "soc_pct": 98,
                "decision": dec_b
            })
            
            # Phase C Mock - Low SOC, Charging
            dec_c = copy.deepcopy(row)
            dec_c["soc_pct"] = 15
            dec_c["action"] = "GRID_CHARGE"
            dec_c["trigger_category"] = "force_charge"
            dec_c["dispatch_summary"] = "Low SOC — force charging to reach target."
            fleet.append({
                "gateway_id": "mock_gw_c",
                "name": "Phase C Gateway",
                "soc_pct": 15,
                "decision": dec_c
            })

    return {
        "ok": True, 
        "gateway_id": gateway_id, 
        "decision": row,
        "fleet": fleet,
        "mock_multi_gateway": bool(mock_multi_gateway)
    }


@router.post("/eval/force")
async def force_eval(gateway_id: str):
    """
    Trigger an immediate re-evaluation of the engine for a gateway.
    Finds the active pricing service and runs evaluate_and_log().
    """

    # Find the active pricing snapshot from the service layer
    try:
        from src.services.pricing.service import pricing_registry
        from src.services.smart_dispatch import smart_dispatch_engine

        # Get the utility service linked to this gateway
        utility_service = await db.get_utility_service_for_gateway(gateway_id)
        if not utility_service:
            # Fallback to primary
            pricing_svc = pricing_registry.get_primary_service()
        else:
            pricing_svc = pricing_registry.get_service(utility_service["id"])

        if pricing_svc is None:
            return {"ok": False, "error": "Pricing service not available"}

        snap = pricing_svc.get_snapshot()

        # Try to get current SoC from the gateway registry
        soc_pct = None
        try:
            reg = smart_dispatch_engine._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    soc_pct = gw.status.last_data.get("battery_soc")
        except Exception:
            pass

        result = await smart_dispatch_engine.evaluate_and_log(
            snap, soc_pct=soc_pct, gateway_serial=gateway_id
        )
        return {"ok": True, "gateway_id": gateway_id, "decision": result.to_dict()}

    except Exception as exc:
        logger.error(f"force_eval [{gateway_id}]: {exc}")
        return {"ok": False, "error": str(exc)}


@router.get("/decision_chain")
async def get_decision_chain(gateway_id: str):
    """Read-only diagnostic surface for the Engine Diagnostics view.

    Returns everything needed to visualise the SD decision precedence
    chain (v0.5.2, Phase 3.C, 2026-08-06):

      1. Winner — the rule that actually won this tick (from the latest
         pricing_eval_log row).
      2. LP output — most recent 24 h plan's slot-0 recommendation
         (from sd_forecast_history). May differ from the winner if a
         higher-precedence step overrode it.
      3. Precedence chain — the 6 ordered steps (matrix → time schedules
         → custom automations → LP fallback → legacy rulebook) with a
         static description each; the API doesn't re-run them, just
         reports which step produced the winner.
      4. Engine config — the knobs governing behaviour: strategy_mode,
         notification_mode, use_lp_optimizer, sd_use_micro_ticker, +
         key thresholds.
      5. Strategy matrix rows — enumerated (global + gateway-specific)
         so users can see the rulebook the mixer works with.

    Everything is read from tables the SD engine writes to. No engine
    invocation, no mutations. Safe to poll from the UI at high cadence."""
    import json as _json

    latest_eval = await db.get_latest_pricing_eval(gateway_id) or {}
    try:
        latest_eval["shadowed_rules"] = _json.loads(latest_eval.get("shadowed_rules_json") or "[]")
    except Exception:
        latest_eval["shadowed_rules"] = []

    cfg = await db.get_smart_dispatch_config(gateway_id) or {}

    # LP / Greedy plan (whichever was configured for this gateway last run)
    forecast = await db.get_latest_forecast_history()
    lp_plan_first_slot = None
    lp_plan_horizon = None
    if forecast:
        try:
            plan = _json.loads(forecast.get("plan_json") or "[]")
            if plan:
                lp_plan_first_slot = plan[0]
                lp_plan_horizon = {
                    "slots":           len(plan),
                    "generated_at":    forecast.get("generated_at"),
                    "horizon_start":   forecast.get("plan_horizon_start"),
                    "horizon_end":     forecast.get("plan_horizon_end"),
                }
        except Exception:
            pass

    # Matrix rules (both global scope 'all' and gateway-specific)
    matrix = await db.get_sd_strategy_matrix(gateway_id=gateway_id, include_disabled=True) or []

    # Static precedence chain description — matches
    # SmartDispatchEngine.evaluate_rules order at src/services/smart_dispatch/__init__.py:895.
    precedence_chain = [
        {
            "step":  1,
            "id":    "strategy_mixer",
            "name":  "Strategy Matrix",
            "role":  "Primary — user-declared rules with priority-ordered eval",
            "wins_when":  "any matrix row's conditions match the live context",
        },
        {
            "step":  2,
            "id":    "time_schedules",
            "name":  "Time Schedules",
            "role":  "User time-bound overrides (e.g. “charge 2–6 am”)",
            "wins_when":  "current wall-clock is inside an active schedule window AND no matrix rule fired",
        },
        {
            "step":  3,
            "id":    "custom_automations",
            "name":  "Automation Builder (custom)",
            "role":  "User-authored rules from the Automation Builder tab",
            "wins_when":  "any custom rule's conditions match AND steps 1–2 were silent",
        },
        {
            "step":  4,
            "id":    "lp_fallback",
            "name":  "LP / Greedy Optimiser (fallback)",
            "role":  "Cost-minimising 24 h dispatch plan; LP (PuLP+CBC) if use_lp_optimizer=1, else greedy",
            "wins_when":  "optimiser returns GRID_CHARGE or GRID_EXPORT AND steps 1–3 were silent",
        },
        {
            "step":  5,
            "id":    "legacy_rulebook",
            "name":  "Legacy Rulebook",
            "role":  "Amber baseline rulebook (spike/force-charge/etc.)",
            "wins_when":  "steps 1–4 silent AND legacy rule matches",
        },
        {
            "step":  6,
            "id":    "default_hold",
            "name":  "Default HOLD",
            "role":  "No override — gateway continues in native mode",
            "wins_when":  "all above silent (nothing to do this tick)",
        },
    ]

    # Determine which step the current winner came from. pricing_eval_log
    # doesn't have a rule_id column, so key off trigger_category + the
    # reason string prefix (which reliably carries a step marker).
    winner_category = (latest_eval.get("trigger_category") or "") if latest_eval else ""
    winner_reason   = (latest_eval.get("reason") or "") if latest_eval else ""
    winner_rulename = (latest_eval.get("rule_name") or "") if latest_eval else ""
    if not latest_eval:
        winner_step_id = None
    elif winner_reason.startswith("Matrix match"):
        winner_step_id = "strategy_mixer"
    elif winner_category == "time_schedule" or winner_rulename.startswith("Scheduled "):
        winner_step_id = "time_schedules"
    elif winner_category == "active_arbitrage" or "Solver optimized action" in winner_reason:
        winner_step_id = "lp_fallback"
    elif winner_rulename.startswith("Amber ") or winner_category == "legacy_rule":
        winner_step_id = "legacy_rulebook"
    elif winner_rulename == "No Override (Catch-All)":
        winner_step_id = "default_hold"
    else:
        # Anything else (custom Automation Builder rule, or unmatched)
        winner_step_id = "custom_automations" if winner_rulename else "default_hold"

    return {
        "ok":               True,
        "gateway_id":       gateway_id,
        "winner":           latest_eval or None,
        "winner_step_id":   winner_step_id,
        "lp_plan_first_slot": lp_plan_first_slot,
        "lp_plan_horizon":  lp_plan_horizon,
        "precedence_chain": precedence_chain,
        "engine_config": {
            "strategy_mode":         cfg.get("strategy_mode"),
            "notification_mode":     cfg.get("notification_mode"),
            "use_lp_optimizer":      cfg.get("use_lp_optimizer", 0),
            "sd_use_micro_ticker":   cfg.get("sd_use_micro_ticker", 0),
            "min_soc":               cfg.get("min_soc"),
            "max_soc":               cfg.get("max_soc"),
            "max_charge_price":      cfg.get("max_charge_price"),
            "min_export_price":      cfg.get("min_export_price"),
            "export_bonus_threshold": cfg.get("export_bonus_threshold"),
            "price_spike_threshold": cfg.get("price_spike_threshold"),
        },
        "strategy_matrix":  matrix,
        "matrix_count":     len(matrix),
    }


@router.get("/log")
async def get_eval_log(gateway_id: str, limit: int = 50):
    """Return paginated evaluation log, newest first (max 200)."""
    limit = min(limit, 200)
    rows = await db.get_pricing_eval_log(gateway_id, limit=limit)
    import json as _json
    for r in rows:
        try:
            r["shadowed_rules"] = _json.loads(r.get("shadowed_rules_json") or "[]")
        except:
            r["shadowed_rules"] = []
    return {"ok": True, "gateway_id": gateway_id, "log": rows, "count": len(rows)}


@router.get("/earnings")
async def get_earnings(gateway_id: str):
    """Return daily + monthly earnings summary using Amber usage cache."""
    summary = await get_earnings_summary(gateway_id)
    return {"ok": True, "gateway_id": gateway_id, **summary}


@router.get("/simulate")
async def simulate_dispatch(gateway_id: str):
    """
    Run the Smart Dispatch engine in dry-run mode and return the matched rule and shadowed rules.
    """
    
    state = get_app_state()
    engine = state.get("smart_dispatch")
    registry = state.get("registry")
    provider = state.get("pricing_provider")
    
    if not engine or not registry or not provider:
        return {"error": "Engine dependencies not initialized"}
        
    gw = registry.get_gateway(gateway_id)
    if not gw:
        return {"error": "Gateway not found"}
        
    snap = provider.get_latest_snapshot(gateway_id)
    if not snap:
        return {"error": "No pricing snapshot available"}
        
    soc_pct = None
    if gw.status and gw.status.last_data:
        soc_pct = gw.status.last_data.get("battery_soc")
        
    try:
        decision = await engine.evaluate_rules(snap, soc_pct, gateway_id, provider.provider_name, verbose=False)
        
        shadowed_rules = []
        if getattr(decision, "action_payload", None) and "_shadowed_rules" in decision.action_payload:
            shadowed_rules = decision.action_payload["_shadowed_rules"]
            
        return {
            "ok": True,
            "decision": {
                "rule_name": decision.rule_name,
                "action": decision.action,
                "priority": decision.priority,
                "reason": decision.reason,
                "trigger_category": decision.trigger_category,
                "conditions_met": decision.conditions_met,
            },
            "shadowed_rules": shadowed_rules,
            "soc_pct": soc_pct,
            "snapshot": snap.model_dump(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── /forecast response cache ────────────────────────────────────────────────
# The full 24h evaluation costs ~20s end-to-end (48 periods × _build_context
# + 7 evaluators per period, with DB lookups and HA state reads). The UI was
# auto-refreshing every 5 min plus calling the endpoint on every tab open —
# fix one slow call by caching the response per-gateway for 10 min. Manual
# refresh button bypasses the cache via ?force=true. Per user directive
# (2026-06-17): SD forecast should be re-evaluated on-demand or daily.
_FORECAST_CACHE: dict[str, tuple[float, dict]] = {}  # gateway_id → (ts, response)
_FORECAST_CACHE_TTL_S = 600  # 10 minutes


@router.get("/forecast")
async def get_forecast_map(
    gateway_id: str,
    soc_pct: Optional[float] = None,
    force: bool = False,
):
    """
    24-hour forecast decision map (cached 10 min; `?force=true` bypasses).

    Runs the 6-rule engine against every interval in the live price forecast
    (typically 48 × 30-min Amber intervals = next 24 hours).

    Returns a list of slots:
      { start, end, import_c, export_c, descriptor, tariff_type, tariff_period,
        demand_window, spike_status, rule_name, trigger_category, preset_name,
        action, is_current, projected_soc }

    SOC is projected using solar forecast data when available (SolarForecastManager);
    falls back to flat current SOC when solar is not configured.
    """
    if not gateway_id or gateway_id == "null":
        return {"ok": False, "error": "Gateway ID is required", "slots": []}

    import time as _t
    if not force:
        cached = _FORECAST_CACHE.get(gateway_id)
        if cached and (_t.time() - cached[0]) < _FORECAST_CACHE_TTL_S:
            age_s = int(_t.time() - cached[0])
            resp = dict(cached[1])
            resp["_cached"] = True
            resp["_cache_age_s"] = age_s
            return resp


    try:
        from src.services.pricing.service import pricing_registry
        from src.services.smart_dispatch import (
            smart_dispatch_engine,
            _build_context,
            _eval_demand_charge,
            _eval_negative_export,
            _eval_price_spike,
            _eval_export_bonus,
            _eval_force_charge,
            _eval_time_schedules,
            _eval_earnings_target,
            is_schedule_active,
        )
        from src.services.pricing.base import PriceSnapshot

        # Get the utility service linked to this gateway
        utility_service = await db.get_utility_service_for_gateway(gateway_id)
        if not utility_service:
            pricing_svc = pricing_registry.get_primary_service()
        else:
            pricing_svc = pricing_registry.get_service(utility_service["id"])
            
        snap = pricing_svc.get_snapshot() if pricing_svc else None
        if snap is None:
            return {
                "ok": False,
                "error": "No price snapshot available yet — pricing service starting",
                "slots": [],
            }

        # Engine config + baseline check
        cfg = await db.get_smart_dispatch_config(gateway_id) or {}
        dna = await db.get_gateway_by_full_serial(gateway_id) if gateway_id else None
        baseline_ok = smart_dispatch_engine._baseline_exists()

        try:
            schedules = await db.get_smart_dispatch_schedules(gateway_id)
        except Exception:
            schedules = []

        daily_earnings = 0.0
        monthly_earnings = 0.0
        billing_configured = False
        try:
            from src.services.earnings_tracker import get_daily_earnings, get_monthly_earnings
            daily_earnings = await get_daily_earnings(gateway_id)
            monthly_earnings, billing_configured = await get_monthly_earnings(gateway_id)
        except Exception:
            pass

        # Current SOC
        soc_pct = None
        backup_reserve_soc = 20.0
        grid_connected = True
        generator_enabled = False
        live_solar_kw = 0.0
        grid_relay = None
        generator_relay = None
        solar_relay_1 = None
        solar_relay_2 = None
        try:
            reg = smart_dispatch_engine._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    soc_pct = gw.status.last_data.get("battery_soc")
                    backup_reserve_soc = gw.status.last_data.get("backup_reserve_soc", 20.0)
                    grid_connected = gw.status.last_data.get("grid_connected", True)
                    
                    # Relays
                    grid_relay = gw.status.last_data.get("relay_grid")
                    generator_relay = gw.status.last_data.get("relay_generator")
                    solar_relay_1 = gw.status.last_data.get("relay_solar")
                    solar_relay_2 = gw.status.last_data.get("relay_pv2")

                    # Live Solar Fallback
                    live_solar_kw = float(gw.status.last_data.get("solar_kw", 0.0))

                    # Use profile has_generator or live generator_enabled
                    profile_acc = json.loads(gw.profile_json or "{}") if hasattr(gw, "profile_json") else {}
                    generator_enabled = profile_acc.get("has_generator", False) or gw.status.last_data.get("generator_enabled", False)
        except Exception:
            pass

        # ── Solar SOC projection ──────────────────────────────────────────────
        # Build per-slot projected SOC using solar forecast manager.
        # Solar Setup tab is now the source of truth — consume the cache whenever
        # a source and location are configured, regardless of any legacy 'enabled' flag.
        solar_slots: list = []
        solar_source = None
        try:
            import os
            from pathlib import Path
            from src.services.solar import SolarForecastManager
            from src.services.db import get_solar_forecast_config, get_config_value

            solar_cfg = await get_solar_forecast_config()
            _source = solar_cfg.get("source") or ""
            _lat    = solar_cfg.get("lat")
            _lng    = solar_cfg.get("lng")

            # Only proceed if a real source + coordinates are configured
            if _source and _source != "none" and _lat and _lng:
                ha_url   = await get_config_value("ha_host",  "") or os.environ.get("HA_HOST",  "")
                ha_token = await get_config_value("ha_token", "") or os.environ.get("HA_TOKEN", "") \
                           or os.environ.get("SUPERVISOR_TOKEN", "")
                data_dir = Path(os.environ.get("DATA_DIR", "./data"))
                solar_cfg["ha"] = {
                    "ha_url":   ha_url,
                    "ha_token": ha_token,
                    "solar_actual_entity":   solar_cfg.get("ha_solar_actual_entity") or "",
                    "solar_forecast_entity": solar_cfg.get("ha_solar_forecast_entity") or "",
                }
                solar_cfg["installation"] = {
                    "lat":     _lat,
                    "lng":     _lng,
                    "azimuth": solar_cfg.get("azimuth", 180.0),
                    "tilt":    solar_cfg.get("tilt", 22.5),
                    "kwp":     solar_cfg.get("kwp", 5.0),
                }
                solar_cfg["providers"] = {
                    "forecast_solar": {
                        "api_key": solar_cfg.get("forecast_solar_api_key") or "",
                    },
                    "solcast": {
                        "api_key":  solar_cfg.get("solcast_api_key") or "",
                        "site_id":  solar_cfg.get("solcast_site_id") or "",
                        "_data_dir": str(data_dir),
                    },
                }
                mgr = SolarForecastManager(config=solar_cfg, data_dir=data_dir)
                solar_slots = mgr.get_forecast()
                solar_source = mgr.get_status().get("active_source")
                logger.debug(
                    f"SD forecast: solar source={solar_source!r} slots={len(solar_slots)}"
                )
        except Exception as solar_err:
            logger.debug(f"Solar forecast unavailable for SD projection: {solar_err}")

        # Build projected_soc list (one value per slot)
        # Formula: delta_kwh = (pv_kw - home_load_kw) * 0.5 per 30-min slot
        base_home_load_kw = 0.5  # default assumption
        try:
            from src.services.db import get_solar_forecast_config as _sc
            _sc2 = await _sc()
            base_home_load_kw = float(_sc2.get("home_load_assumption_kw") or 0.5)
        except Exception:
            pass
            
        # Fetch forecast loads
        try:
            from src.services.db import get_all_forecast_loads
            forecast_loads = await get_all_forecast_loads()
        except Exception:
            forecast_loads = []
            
        # Determine Site Season via get_equipment_location (cached locally)
        site_season = ""
        try:
            from src.services.db import get_config_value, set_config_value
            from datetime import datetime
            from src.services.smart_dispatch import get_site_season
            
            cached_lat = await get_config_value(f"lat_{gateway_id}")
            lat = None
            if cached_lat:
                lat = float(cached_lat)
            else:
                try:
                    from src.services.gateway_service import gateway_registry
                    gw_svc = gateway_registry.get_gateway(gateway_id)
                    if gw_svc and gw_svc.client:
                        # get_equipment_location() takes no arguments and reads the serial off
                        # the client; `raw` is not a method the client has at all.
                        loc_resp = await gw_svc.client.get_equipment_location()
                        if loc_resp and "latitude" in loc_resp:
                            lat = float(loc_resp["latitude"])
                            await set_config_value(f"lat_{gateway_id}", str(lat))
                except Exception as loc_e:
                    logger.debug(f"Failed to fetch equipment location: {loc_e}")
                    
            if lat is not None:
                site_season = get_site_season(lat, datetime.now().month)
        except Exception:
            pass
            
        # Fetch HA states for 'now' loads
        ha_states = {}
        try:
            from src.routes.api_ha import get_ha_state
            for fl in forecast_loads:
                if fl.get("enabled", 1) and fl.get("measurement_type") == "now":
                    for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                        ent_id = fl.get(key)
                        if ent_id:
                            st = await get_ha_state(ent_id)
                            if st and "state" in st:
                                ha_states[ent_id] = st["state"]
        except Exception as e:
            logger.debug(f"Could not fetch HA states for forecast loads: {e}")

        # Battery capacity — try to read from gateway, default 13.6 kWh
        battery_kwh = 13.6
        try:
            reg = smart_dispatch_engine._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    cap = gw.status.last_data.get("battery_capacity_kwh")
                    if cap and float(cap) > 0:
                        battery_kwh = float(cap)
        except Exception:
            pass

        # Determine Site Profile & Details
        gateway_count = 1
        dc_coupled_count = 0
        ac_coupled_count = 0
        remote_pv_count = 0
        try:
            from src.services.db import get_db
            async with get_db() as conn:
                # 1. Count enabled gateways
                async with conn.execute("SELECT COUNT(*) FROM gateways WHERE enabled = 1") as cur:
                    row = await cur.fetchone()
                    if row:
                        gateway_count = max(1, int(row[0]))
                
                # 2. Count and classify solar sources
                async with conn.execute(
                    "SELECT source_type, inverter_type FROM gateway_solar_sources WHERE gateway_id = ?",
                    (gateway_id,)
                ) as cur:
                    rows = await cur.fetchall()
                    for r_type, r_inv in rows:
                        r_type_lower = (r_type or "").lower()
                        if "dc_coupled" in r_type_lower or "mppt" in r_type_lower:
                            dc_coupled_count += 1
                        elif "remote_pv" in r_type_lower or "ahub" in r_type_lower or "apbox" in r_type_lower:
                            remote_pv_count += 1
                        else:
                            ac_coupled_count += 1
        except Exception as e:
            logger.debug(f"Failed to query database for detailed site parameters: {e}")

        # Each aGate has a 5.0 kW continuous inverter rating
        inverter_capacity_kw = 5.0 * gateway_count

        utility_name = None
        tariff_plan = None
        try:
            svc = await db.get_utility_service_for_gateway(gateway_id) or {}
            utility_name = (svc.get("retailer_name") or svc.get("name") or "").strip() or None
            tariff_plan = (svc.get("tariff_company_name") or "").strip() or None
        except Exception:
            logger.debug("forecast: utility service lookup failed", exc_info=True)

        # ── Fetch Native TOU Schedule ─────────────────────────────────────────
        native_mode = "Unknown"
        tou_periods = []
        try:
            from src.routes.api_phase9 import gateway_schedule
            sched_data = await gateway_schedule(gateway_id)
            native_mode = sched_data.get("mode", "Unknown")
            tou_periods = sched_data.get("tou_periods", [])
            if not isinstance(tou_periods, list):
                tou_periods = []
        except Exception as e:
            logger.warning(f"Could not load native TOU schedule for forecast: {e}")

        # Resolve simulated baseline operating mode based on user configuration
        default_pref = cfg.get("default_operating_mode", "gateway_default")
        if default_pref != "gateway_default":
            if default_pref in ("Backup", "Standby", "Emergency Backup"):
                simulated_baseline_mode = "Emergency Backup"
            else:
                simulated_baseline_mode = default_pref
        else:
            simulated_baseline_mode = native_mode

        # ── Predictive SOC & Power Flow Simulation ────────────────────────────
        projected_soc_list: list[float | None] = []
        current_soc = float(soc_pct) if soc_pct is not None else 50.0
        rolling_soc = current_soc
        
        # Max battery power in kW (default ~5kW for 1 aPower)
        max_power_kw = 5.0
        
        try:
            from src.services.db import get_all_forecast_loads
            forecast_loads_list = await get_all_forecast_loads()
        except Exception:
            forecast_loads_list = []

        # Hoist Enphase enablement out of the per-period loop — _build_context
        # would otherwise re-query db.get_solar_forecast_config() N times for
        # the same row. With 48-period forecasts this saves ~5s.
        _enphase_enabled = 0
        try:
            _solar_cfg = await db.get_solar_forecast_config()
            _enphase_enabled = _solar_cfg.get("enphase_enabled", 0)
        except Exception:
            pass

        from datetime import datetime, timezone
        slots = []

        # Defensive cap: evaluate at most 48 periods (24h × 30-min, matches
        # AEMO real-API density). Each period runs _build_context + up to 7
        # evaluators — at ~50-300ms/period this stays under ~15s end-to-end.
        # If a provider supplies denser forecasts (e.g. LocalVolts at 5-min),
        # we downsample by stride so the same time window is covered with a
        # bounded evaluator load. The UI's 24h strip stays informative; the
        # SD decision is only meaningful at ~30-min resolution anyway since
        # dispatch actions can't switch faster than the ramp time.
        forecast_in = list(snap.forecast or [])
        # Raised from 48. A week of TOU blocks is ~48 periods on its own, so
        # the old cap silently decimated anything past tomorrow — the days
        # whose weather actually differs.
        FORECAST_EVAL_CAP = 200
        if len(forecast_in) > FORECAST_EVAL_CAP:
            stride = max(1, len(forecast_in) // FORECAST_EVAL_CAP)
            forecast_in = forecast_in[::stride][:FORECAST_EVAL_CAP]
            logger.info(
                f"forecast endpoint: downsampled {len(snap.forecast)} → "
                f"{len(forecast_in)} periods (stride={stride}) for engine eval"
            )

        for idx, period in enumerate(forecast_in):
            # 1. Get PV temporally (not by simple index, because solar_slots may start at 00:00 today)
            pv_kw = 0.0
            
            p_start = period.start
            if not p_start.tzinfo:
                p_start = p_start.replace(tzinfo=timezone.utc)

            if solar_slots:
                # We find the solar slot whose timestamp is <= period.start and closest
                best_match = None
                best_diff = float('inf')
                for ss in solar_slots:
                    try:
                        ss_time = datetime.fromisoformat(ss.get("timestamp"))
                        if not ss_time.tzinfo:
                            ss_time = ss_time.replace(tzinfo=timezone.utc)
                        diff = abs((p_start - ss_time).total_seconds())
                        if diff < best_diff and diff <= 3600: # Max 1h discrepancy allowed
                            best_diff = diff
                            best_match = ss
                    except Exception:
                        pass
                
                if best_match:
                    pv_kw = float(best_match.get("pv_kw", 0.0))
            
            # Realtime Solar Fallback
            is_current_slot = getattr(period, "is_current", False) or idx == 0
            if is_current_slot:
                if pv_kw == 0.0 and live_solar_kw > 0.1:
                    pv_kw = live_solar_kw
            
            # Start of slot SOC
            slot_soc = rolling_soc
            
            # Calculate Home Load for this interval
            from src.services.smart_dispatch import get_active_forecast_load_kw
            active_load_kw = get_active_forecast_load_kw(
                forecast_loads,
                p_start,
                site_season,
                is_current_slot=is_current_slot,
                ha_states=ha_states,
                gateway_id=gateway_id
            )
            home_load_kw = base_home_load_kw + active_load_kw

            # 2. Build context and evaluate engine
            period_snap = PriceSnapshot(
                provider=snap.provider,
                import_c_kwh=period.import_c_kwh,
                export_c_kwh=period.export_c_kwh,
                tariff_type=period.tariff_type,
                spike_status=period.spike_status,
                demand_window=period.demand_window,
                renewables_pct=period.renewables_pct,
                interval_min=snap.interval_min,
                valid_until=period.end,
                forecast=[period],
            )
            period_snap.is_current = True
            ctx = await _build_context(period_snap, slot_soc, dna=dna, cfg=cfg, loads=forecast_loads_list, enphase_enabled=_enphase_enabled)
            ctx["grid_connected"] = grid_connected
            ctx["generator_enabled"] = generator_enabled

            decision = None
            evaluators = [
                lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules),
                lambda: _eval_demand_charge(ctx, cfg, baseline_ok),
                lambda: _eval_negative_export(ctx, cfg, baseline_ok),
                lambda: _eval_price_spike(ctx, cfg, baseline_ok),
                lambda: _eval_export_bonus(ctx, cfg, baseline_ok),
                lambda: _eval_earnings_target(
                    ctx, cfg, baseline_ok,
                    daily_earnings=daily_earnings,
                    monthly_earnings=monthly_earnings,
                    billing_configured=billing_configured,
                    snap=snap,
                ),
                lambda: _eval_force_charge(ctx, cfg, baseline_ok),
            ]
            
            strategy = cfg.get("strategy_mode", "active")
            if strategy == "passive":
                evaluators = [
                    lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules),
                    lambda: _eval_demand_charge(ctx, cfg, baseline_ok),
                    lambda: _eval_price_spike(ctx, cfg, baseline_ok),
                ]
            elif strategy == "proactive":
                evaluators = [
                    lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules),
                    lambda: _eval_demand_charge(ctx, cfg, baseline_ok),
                    lambda: _eval_negative_export(ctx, cfg, baseline_ok),
                    lambda: _eval_price_spike(ctx, cfg, baseline_ok),
                    lambda: _eval_export_bonus(ctx, cfg, baseline_ok),
                    lambda: _eval_earnings_target(
                        ctx, cfg, baseline_ok,
                        daily_earnings=daily_earnings,
                        monthly_earnings=monthly_earnings,
                        billing_configured=billing_configured,
                        snap=snap,
                    ),
                    lambda: _eval_force_charge(ctx, cfg, baseline_ok),
                ]

            # Detailed Decision Matrix evaluation per slot
            rule_statuses = []
            eval_rules = [
                ("Time Schedule", lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules)),
                ("Demand Charge Protection", lambda: _eval_demand_charge(ctx, cfg, baseline_ok)),
                ("Negative Export Curtailment", lambda: _eval_negative_export(ctx, cfg, baseline_ok)),
                ("Price Spike Arbitrage", lambda: _eval_price_spike(ctx, cfg, baseline_ok)),
                ("Export Bonus Arbitrage", lambda: _eval_export_bonus(ctx, cfg, baseline_ok)),
                ("Earnings Target Arbitrage", lambda: _eval_earnings_target(
                    ctx, cfg, baseline_ok,
                    daily_earnings=daily_earnings,
                    monthly_earnings=monthly_earnings,
                    billing_configured=billing_configured,
                    snap=snap,
                )),
                ("Safety Force Charge", lambda: _eval_force_charge(ctx, cfg, baseline_ok)),
            ]
            
            strategy = cfg.get("strategy_mode", "active")
            if strategy == "passive":
                eval_rules = [
                    ("Time Schedule", lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules)),
                    ("Demand Charge Protection", lambda: _eval_demand_charge(ctx, cfg, baseline_ok)),
                    ("Price Spike Arbitrage", lambda: _eval_price_spike(ctx, cfg, baseline_ok)),
                ]
            elif strategy == "proactive":
                pass
                
            is_dynamic_pricing = snap.provider in ("amber", "localvolts", "aemo", "comed") if snap else False
            if not is_dynamic_pricing:
                eval_rules = [
                    ("Time Schedule", lambda: _eval_time_schedules(ctx, cfg, baseline_ok, schedules)),
                    ("Demand Charge Protection", lambda: _eval_demand_charge(ctx, cfg, baseline_ok)),
                ]

            primary_found = False
            decision = None
            for rule_title, eval_fn in eval_rules:
                res_dec = eval_fn()
                if res_dec is not None:
                    if not primary_found:
                        status = "Passed"
                        decision = res_dec
                        primary_found = True
                    else:
                        status = "Shadowed"
                else:
                    status = "Skipped"
                    
                rule_statuses.append({
                    "rule": rule_title,
                    "status": status,
                    "action": res_dec.action if res_dec else "HOLD",
                    "reason": res_dec.reason if res_dec else "Conditions not met"
                })

            if decision is None:
                trigger_category = "fallback"
                rule_name        = "No Override — Native Mode"
                preset_name      = None
                action           = "HOLD"   # no-op: no SD override issued, gateway native mode continues
            else:
                trigger_category = decision.trigger_category
                rule_name        = decision.rule_name
                preset_name      = decision.preset_name
                action           = decision.action
                
            period_snap.is_current = False

            # 3. Simulate power flow based on action
            tou_dispatch_id = 4  # default to Self-Consumption (4)
            if simulated_baseline_mode in ("Emergency Backup", "Standby"):
                tou_dispatch_id = 5  # Standby / Backup
            elif simulated_baseline_mode == "Time-of-Use" and tou_periods:
                utc_start = period.start
                if not utc_start.tzinfo:
                    utc_start = utc_start.replace(tzinfo=timezone.utc)
                hm = utc_start.astimezone().strftime("%H:%M")
                for tp in tou_periods:
                    t_start = tp.get("start")
                    t_end = tp.get("end")
                    if not t_start or not t_end:
                        continue
                    if t_end == "24:00":
                        t_end = "23:59:59"
                    if t_start <= t_end:
                        if t_start <= hm < t_end:
                            tou_dispatch_id = tp.get("dispatchId", 4)
                            break
                    else:
                        if hm >= t_start or hm < t_end:
                            tou_dispatch_id = tp.get("dispatchId", 4)
                            break

            tou_labels = {
                4: "Self-Consumption",
                5: "Standby",
                6: "Grid Charge",
                7: "Grid Discharge",
                8: "Grid Charge (Solar)"
            }
            tou_dispatch_name = tou_labels.get(tou_dispatch_id, "Self-Consumption")

            charge_kw = 0.0
            discharge_kw = 0.0
            min_soc = cfg.get("min_soc") if cfg and cfg.get("min_soc") is not None else 20.0
            max_soc = cfg.get("max_soc") if cfg and cfg.get("max_soc") is not None else 100.0
            
            if action == "GRID_CHARGE":
                if grid_connected:
                    # Force charge from grid
                    capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                    charge_kw = min(max_power_kw, capacity_needed_kwh / 0.5)
                elif generator_enabled:
                    # Off-grid but generator can charge
                    capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                    charge_kw = min(max_power_kw, capacity_needed_kwh / 0.5)
            elif action == "GRID_EXPORT":
                if grid_connected:
                    # Export to grid
                    capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                    discharge_kw = min(max_power_kw, capacity_available_kwh / 0.5)
            elif action == "HOLD":
                # Strict hold mode: no charge, no discharge
                charge_kw = 0.0
                discharge_kw = 0.0
            elif action in ("STANDBY", "CURTAIL_SOLAR", "ADVISE_CURTAIL_PV"):
                # No SD override active.
                # Check native schedule if in TOU mode.
                dispatch_id = tou_dispatch_id
                
                if dispatch_id in (6, 8): # Grid Charge
                    if grid_connected or generator_enabled:
                        capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                        charge_kw = min(max_power_kw, capacity_needed_kwh / 0.5)
                elif dispatch_id == 7: # Grid Discharge (Force Discharge)
                    if grid_connected:
                        capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                        discharge_kw = min(max_power_kw, capacity_available_kwh / 0.5)
                elif dispatch_id == 5: # Standby (Charge only from excess solar)
                    net_kw = pv_kw - home_load_kw
                    if net_kw > 0:
                        capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                        charge_kw = min(net_kw, max_power_kw, capacity_needed_kwh / 0.5)
                else: # Self-Consumption (4)
                    net_kw = pv_kw - home_load_kw
                    if net_kw > 0:
                        capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                        charge_kw = min(net_kw, max_power_kw, capacity_needed_kwh / 0.5)
                    else:
                        capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                        # We only discharge what we need, not max_power_kw
                        discharge_kw = min(abs(net_kw), max_power_kw, capacity_available_kwh / 0.5)
            else:
                # Any other unrecognised action (fallback Self-Consumption)
                net_kw = pv_kw - home_load_kw
                if net_kw > 0:
                    capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                    charge_kw = min(net_kw, max_power_kw, capacity_needed_kwh / 0.5)
                else:
                    capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                    discharge_kw = min(abs(net_kw), max_power_kw, capacity_available_kwh / 0.5)
            
            # Update SOC for the end of this slot
            net_battery_kw = charge_kw - discharge_kw
            delta_pct = ((net_battery_kw * 0.5) / battery_kwh) * 100.0
            rolling_soc = max(0.0, min(100.0, rolling_soc + delta_pct))
            
            # Calculate active scheduled SOC (default to global min_soc)
            scheduled_soc = cfg.get("min_soc", 20.0)
            for sch in schedules:
                if is_schedule_active(sch, period.start, site_season):
                    scheduled_soc = float(sch.get("min_soc", 0.0))
                    break

            slots.append({
                "start":            period.start.isoformat(),
                "end":              period.end.isoformat(),
                "import_c":         round(period.import_c_kwh, 3),
                "export_c":         round(period.export_c_kwh, 3) if period.export_c_kwh is not None else None,
                "descriptor":       getattr(period, "descriptor", "neutral"),
                "tariff_type":      period.tariff_type,
                "tariff_period":    getattr(period, "tariff_period", None),
                "demand_window":    period.demand_window,
                "spike_status":     period.spike_status,
                "rule_name":        rule_name,
                "trigger_category": trigger_category,
                "preset_name":      preset_name,
                "action":           action,
                "projected_soc":    round(slot_soc, 1),
                "scheduled_soc":    round(scheduled_soc, 1),
                "pv_kw":            round(pv_kw, 2),
                "home_load_kw":     round(home_load_kw, 2),
                "charge_kw":        round(charge_kw, 2),
                "discharge_kw":     round(discharge_kw, 2),
                "tou_dispatch_name": tou_dispatch_name,
                "decision_matrix":  rule_statuses,
            })
            
        soc_trajectory = [
            {"timestamp": s["start"], "soc_pct": s["projected_soc"]}
            for s in slots
        ]
        
        soc_note = "Predictive forecast simulated with real-time Smart Dispatch engine actions"

        # Solar nameplate capacity — sum across all enabled sources for this gateway
        # (Ph-2: replaces flat solar_cfg.get('kwp') — now gateway-scoped)
        try:
            from src.services.db import get_gateway_solar_kwp_total
            site_solar_kwp = await get_gateway_solar_kwp_total(gateway_id)
            # Fallback: if no sources configured yet, use legacy flat kwp field
            if site_solar_kwp == 0.0:
                site_solar_kwp = float(solar_cfg.get("kwp") or 0.0)
        except Exception:
            site_solar_kwp = float(solar_cfg.get("kwp") or 0.0)

        # Determine Site Profile (using counts resolved at start of endpoint)

        # Determine Site Profile
        is_solar_active = bool(solar_slots)
        is_dynamic_pricing = snap.provider in ("amber", "localvolts", "aemo", "comed") if snap else False
        weather_influence_enabled = bool(cfg.get("weather_load_influence", True)) if cfg else True

        if is_solar_active and is_dynamic_pricing:
            site_profile = "hems"
        elif is_solar_active and not is_dynamic_pricing:
            site_profile = "tou_arbitrage"
        elif not is_solar_active and is_dynamic_pricing:
            site_profile = "dynamic_arbitrage"
        else:
            is_tou = (snap.provider == "franklinwh_tou") if snap else False
            if is_tou:
                site_profile = "peak_shaving"
            else:
                site_profile = "standby"

        epoch_segments = cluster_epoch_segments(slots)

        response = {
            "ok":                     True,
            "gateway_id":             gateway_id,
            "soc_pct":                soc_pct,
            "min_soc":                cfg.get("min_soc", 20.0),
            "max_soc":                cfg.get("max_soc", 90.0),
            "backup_reserve_soc":     backup_reserve_soc,
            "native_mode":            native_mode,
            # The utility's actual name. The panel used to derive this from
            # solar_source through a map of *solar forecast* providers, so a
            # site using Solcast for its weather was told its electricity
            # retailer was Amber Electric, and anything unmapped — openmeteo,
            # for instance — fell through to the literal "Utility Service".
            "utility_name":           utility_name,
            "tariff_plan":            tariff_plan,
            "default_operating_mode": cfg.get("default_operating_mode", "gateway_default"),
            "grid_connected":         grid_connected,
            "grid_relay":             grid_relay,
            "generator_relay":        generator_relay,
            "solar_relay_1":          solar_relay_1,
            "solar_relay_2":          solar_relay_2,
            "soc_note":               soc_note,
            "solar_source":           solar_source,
            "site_solar_kwp":         site_solar_kwp,
            "is_solar_active":        is_solar_active,
            "is_dynamic_pricing":     is_dynamic_pricing,
            "weather_influence_enabled": weather_influence_enabled,
            "site_profile":           site_profile,
            "gateway_count":          gateway_count,
            "inverter_capacity_kw":   inverter_capacity_kw,
            "dc_coupled_count":       dc_coupled_count,
            "ac_coupled_count":       ac_coupled_count,
            "remote_pv_count":        remote_pv_count,
            "slot_count":             len(slots),
            "slots":                  slots,
            "daily_outlook":          _daily_outlook(solar_slots, slots),
            "epoch_segments":         epoch_segments,
            "soc_trajectory":         soc_trajectory,
            "forecast_engine_version": FORECAST_ENGINE_VERSION,
        }
        _FORECAST_CACHE[gateway_id] = (_t.time(), response)
        return response

    except Exception as exc:
        logger.exception(f"forecast_map [{gateway_id}]: {exc}")
        return {"ok": False, "error": str(exc), "slots": []}


# ── Forecast Loads Endpoints ────────────────────────────────────────────────

@router.get("/forecast-loads")
@router.get("/forecast_loads/list")
async def api_get_forecast_loads():
    try:
        from src.services.db import get_all_forecast_loads
        from src.routes.api_ha import get_ha_state
        from src.services.smart_dispatch import (
            smart_dispatch_engine,
            resolve_site_season_for_gateway,
        )
        loads = await get_all_forecast_loads()

        # For 'now' loads, fetch every configured HA entity and surface each
        # under its own ha_live_*_state field so the UI can render power,
        # energy, switch and binary telemetry side-by-side. Also surface the
        # HA-reported unit_of_measurement so the client can format and
        # normalize correctly (e.g. "30 W" vs "0.030 kW").
        live_state_map = {
            "ha_entity_id":        ("ha_live_power_state",  "ha_live_power_unit"),
            "ha_energy_entity_id": ("ha_live_energy_state", "ha_live_energy_unit"),
            "ha_switch_entity_id": ("ha_live_switch_state", "ha_live_switch_unit"),
            "ha_binary_entity_id": ("ha_live_binary_state", "ha_live_binary_unit"),
        }
        for load in loads:
            if load.get("measurement_type") != "now":
                continue
            for src_key, (state_key, unit_key) in live_state_map.items():
                ent_id = load.get(src_key)
                if not ent_id:
                    continue
                try:
                    st = await get_ha_state(ent_id)
                    if st and "state" in st:
                        load[state_key] = st["state"]
                        unit = (st.get("attributes") or {}).get("unit_of_measurement")
                        if unit:
                            load[unit_key] = unit
                except Exception as ex:
                    logger.debug(f"Failed to fetch live HA state for {ent_id}: {ex}")

        # Resolve the site season from the first registered gateway's latitude.
        # Multi-gateway sites use the first gateway only; per-load season would
        # require a per-gateway map.
        current_season = ""
        try:
            registry = smart_dispatch_engine._gateway_registry
            if registry:
                gws = registry.get_gateways()
                if gws:
                    current_season = await resolve_site_season_for_gateway(
                        gws[0].short_id, registry
                    )
                    if not current_season:
                        logger.info(
                            f"api_get_forecast_loads: current_season unresolved for "
                            f"gateway {gws[0].short_id!r} — see prior warning"
                        )
                else:
                    logger.info("api_get_forecast_loads: registry has no gateways")
            else:
                logger.info("api_get_forecast_loads: smart_dispatch_engine._gateway_registry is None")
        except Exception as ex:
            logger.warning(f"api_get_forecast_loads: current_season resolution failed: {ex!r}")

        return {"ok": True, "data": loads, "current_season": current_season}
    except Exception as e:
        logger.error(f"Error fetching forecast loads: {e}")
        return {"ok": False, "error": str(e)}

@router.post("/forecast-loads")
async def api_save_forecast_load(data: dict):
    try:
        from src.services.db import upsert_forecast_load
        
        # Reject an entity id that can never resolve, rather than storing it
        # and rediscovering it 2,880 times a day. A Home Load held
        # ha_binary_entity_id = "energipays" and the micro ticker polled it on
        # every 30s tick forever. How it was stored is not known; that it was
        # accepted is the defect, and validating it is this code's job.
        from src.routes.api_ha import entity_id_looks_valid

        bad = [
            f"{field}={data[field]!r}"
            for field in ("ha_entity_id", "ha_switch_entity_id",
                          "ha_binary_entity_id", "ha_energy_entity_id")
            if (data.get(field) or "").strip() and not entity_id_looks_valid(data[field])
        ]
        if bad:
            return {"ok": False, "error":
                    "Not a Home Assistant entity id (expected domain.object_id, "
                    f"e.g. sensor.hot_water_power): {', '.join(bad)}"}

        # Ensure schedule_json is a string if passed as list
        if isinstance(data.get("schedule_json"), list):
            import json
            data["schedule_json"] = json.dumps(data["schedule_json"])
            
        load_id = await upsert_forecast_load(data)
        return {"ok": True, "id": load_id}
    except Exception as e:
        logger.error(f"Error saving forecast load: {e}")
        return {"ok": False, "error": str(e)}

@router.delete("/forecast-loads/{load_id}")
async def api_delete_forecast_load(load_id: str):
    try:
        from src.services.db import delete_forecast_load
        await delete_forecast_load(load_id)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error deleting forecast load: {e}")
        return {"ok": False, "error": str(e)}


# ── Energy Devices Endpoints ───────────────────────────────────────────────

@router.get("/energy-devices")
@router.get("/energy-devices/list")
async def api_get_energy_devices():
    try:
        from src.services.db import get_all_energy_devices
        from src.routes.api_ha import get_ha_state
        devices = await get_all_energy_devices()
        
        # Attach live HA states if entities are present
        for dev in devices:
            if dev.get("ha_power_entity"):
                try:
                    st = await get_ha_state(dev["ha_power_entity"])
                    if st and "state" in st:
                        dev["ha_live_power_state"] = st["state"]
                except Exception as ex:
                    logger.debug(f"Failed to fetch live HA power state for {dev['ha_power_entity']}: {ex}")
            if dev.get("ha_switch_entity"):
                try:
                    st = await get_ha_state(dev["ha_switch_entity"])
                    if st and "state" in st:
                        dev["ha_live_switch_state"] = st["state"]
                except Exception as ex:
                    logger.debug(f"Failed to fetch live HA switch state for {dev['ha_switch_entity']}: {ex}")
                    
        return {"ok": True, "data": devices}
    except Exception as e:
        logger.error(f"Error fetching energy devices: {e}")
        return {"ok": False, "error": str(e)}

@router.post("/energy-devices")
async def api_save_energy_device(data: dict):
    try:
        from src.services.db import upsert_energy_device
        
        # Ensure schedule_json is a string if passed as list
        if isinstance(data.get("schedule_json"), list):
            import json
            data["schedule_json"] = json.dumps(data["schedule_json"])
            
        device_id = await upsert_energy_device(data)
        return {"ok": True, "id": device_id}
    except Exception as e:
        logger.error(f"Error saving energy device: {e}")
        return {"ok": False, "error": str(e)}

@router.delete("/energy-devices/{device_id}")
async def api_delete_energy_device(device_id: str):
    try:
        from src.services.db import delete_energy_device
        await delete_energy_device(device_id)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error deleting energy device: {e}")
        return {"ok": False, "error": str(e)}

