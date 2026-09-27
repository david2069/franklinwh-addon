"""
Automation API routes — C1.

Endpoints:
  GET  /api/automation/signal                    → current rule evaluation result (+ engine_mode)
  GET  /api/automation/rules                     → all rules in active rulebook
  PUT  /api/automation/rules/{rule_id}/toggle    → toggle enabled/disabled
  GET  /api/automation/rulebooks                 → list all rulebooks
  GET  /api/automation/history                   → recent trigger history (filterable by source)
  GET  /api/automation/presets/reserved          → list reserved Amber preset metadata
  PUT  /api/automation/engine/mode               → set engine mode (signal_only|active|paused)
  GET  /api/automation/notifications/settings    → get notification config + HA status
  PUT  /api/automation/notifications/settings    → save notification config
  POST /api/automation/notifications/test        → send a test notification
  POST /api/automation/notifications/override    → HA webhook callback for actionable notifications
"""
import json
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.app_state import get_app_state
from src.services import db
from src.services.smart_dispatch import (
    smart_dispatch_engine,
    build_home_loads_context,
    resolve_site_season_for_gateway,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["automation"])


# ── Pydantic models ────────────────────────────────────────────────────────────

class EngineModePayload(BaseModel):
    mode: str  # 'signal_only' | 'active' | 'paused'


class NotificationSettingsPayload(BaseModel):
    """Every field is optional, and omitted means "leave it alone".

    upsert_notification_settings() replaces the whole singleton row, so a
    payload that defaulted missing fields to ""/False/[] silently reset
    ha_target, triggers and actionable every time any part of the form was
    saved. Four separate screens PUT to this endpoint.
    """
    enabled: Optional[bool] = None
    ha_target: Optional[str] = None
    ha_host: Optional[str] = None       # Home Assistant base URL
    ha_token: Optional[str] = None      # empty = don't overwrite stored value
    fhai_host: Optional[str] = None     # This app's base URL
    triggers: Optional[list[str]] = None
    actionable: Optional[bool] = None
    actionable_ttl: Optional[int] = None


from pydantic import BaseModel, Field

class OverridePayload(BaseModel):
    # Support both 'action' and 'action_id' for maximum compatibility
    action: str = ""
    action_id: str = ""
    
    preset_name: str = ""
    
    # Support both 'gateway_serial' and 'gateway_id'
    # Support both 'gateway_serial' and 'gateway_id'
    gateway_serial: str = ""
    gateway_id: str = ""
    
    decision_hash: str = ""  # Legacy hash support
    request_id: str = ""     # v25+ reconciliation UUID
    ha_user_id: str = ""     # Home Assistant user who pressed the button
    response: str = ""       # Text response if using textInput
    reply_text: str = ""     # Alias for response (legacy/iOS compatibility)
    
    action_data: Optional[dict] = None  # Support nested data if sent by HA

    def get_action(self) -> str:
        return self.action_id or self.action

    def get_gateway(self) -> str:
        return self.gateway_id or self.gateway_serial or (self.action_data or {}).get("gateway_id", "")

    def get_request_id(self) -> str:
        return self.request_id or (self.action_data or {}).get("request_id", "")


class TestWebhookPayload(BaseModel):
    url: str

class TemplatesPayload(BaseModel):
    templates: dict[str, str] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_registry():
    return get_app_state().get("registry")


def _get_presets_mgr():
    return get_app_state().get("schedule_presets")


def _get_pricing_registry():
    return get_app_state().get("pricing_registry")


def _live_soc(short_id: Optional[str] = None) -> Optional[float]:
    """Extract live battery SoC from the first (or specified) gateway in registry.

    Reads from the in-memory poll cache populated by GatewayService — no
    DB hit, no HA round-trip. Called per Smart Dispatch evaluation tick
    via /api/automation/signal.
    """
    registry = _get_registry()
    if not registry:
        return None
    try:
        if short_id:
            status = registry.get_status(short_id)
        else:
            gateways = registry.list_gateways() if hasattr(registry, "list_gateways") else []
            if not gateways:
                return None
            status = registry.get_status(gateways[0])
        # GatewayStatus uses 'last_data'; older code paths used 'last_stats'.
        last_data = (status or {}).get("last_data") or (status or {}).get("last_stats") or {}
        soc = last_data.get("battery_soc")
        return float(soc) if soc is not None else None
    except Exception as exc:
        logger.debug(f"_live_soc: could not read SoC — {exc}")
        return None


async def _diagnose_missing_pending(
    gateway_serial: Optional[str],
    request_id: Optional[str],
) -> tuple[int, str]:
    """Distinguish "expired" from "unknown" pending_approvals for the
    Batch L (2026-07-21) callback diagnostic 404/410 upgrade.

    Returns (http_status_code, kind):
        (410, "expired")  — request_id was a real pending, TTL exceeded
        (404, "unknown")  — request_id never existed or was already cleared
                            by a non-expiry path (SKIP / user response / etc)

    `db.get_pending_approval` auto-clears expired rows when it reads them,
    so by the time the callback handler gets None back, we can't tell
    which case fired. This helper queries pending_approvals directly
    (bypassing the expiry auto-clear) — but that helper doesn't have a
    'include expired' mode, so we consult two side-channels:

      1. notification_cooldown: Batch C's sweep converts expired-and-
         ignored pendings into cooldown rows. If a cooldown exists for
         this gateway within the last hour, it's evidence the request
         did exist and expired.
      2. automation_history: any audit row referencing this request_id
         means it was a real request.

    Neither side-channel is 100%: cooldown-arm can race with cleanup and
    audit rows are best-effort. When in doubt, default to 404 (unknown)
    rather than 410 — 404 is the more actionable error for a caller and
    can't be mistaken for "you were too slow" when it was really "we
    never saw this."
    """
    if not request_id:
        return (404, "unknown")
    try:
        # Direct DB probe — did any audit row ever mention this request_id?
        # If yes → the request existed at some point → expired.
        async with db.get_db() as conn:
            async with conn.execute(
                "SELECT 1 FROM automation_history WHERE request_id = ? LIMIT 1",
                (request_id,),
            ) as cur:
                row = await cur.fetchone()
        if row:
            return (410, "expired")
    except Exception as _exc:
        logger.debug(f"_diagnose_missing_pending audit probe failed — {_exc}")
    return (404, "unknown")


async def _fhai_host() -> str:
    """FHAI base URL for YAML recipe generation.

    Delegates to addon_info.resolve_fhai_host so the add-on and Docker installs
    agree. The old inline fallback returned a docker-compose container name,
    which under the Supervisor produced a Blueprint that could never call back.
    """
    from src.services.addon_info import resolve_fhai_host
    return await resolve_fhai_host()


# ── Signal endpoint ───────────────────────────────────────────────────────────

@router.get("/automation/signal")
async def automation_signal(
    short_id: Optional[str] = Query(default=None, description="Gateway short_id for SoC lookup"),
    provider: Optional[str] = Query(default=None, description="Filter by provider (e.g. 'amber')"),
):
    """
    Return the current automation recommendation signal + engine_mode.

    Home Assistant REST sensor:
      resource: http://<FHAI>:8099/api/automation/signal
      value_template: "{{ value_json.action }}"
      json_attributes: [preset_name, rule_name, reason, can_execute, engine_mode]
    """
    engine_mode = await db.get_engine_mode()
    # print() goes to stdout only: it never reaches the log file, so it is
    # absent from the Logs tab and from a support bundle, and no log level
    # turns it off. This fires on every automation signal.
    logger.debug(f"automation_signal: short_id={short_id} provider={provider}")

    pricing_registry = _get_pricing_registry()
    pricing_svc = pricing_registry.get_primary_service() if pricing_registry else None
    snap = pricing_svc.get_snapshot() if pricing_svc else None

    if snap is None:
        return {
            "action": "RESUME_TOU",
            "preset_name": None,
            "rule_id": "__no_pricing__",
            "rule_name": "No Pricing Data",
            "priority": 9999,
            "reason": "Pricing service has no snapshot yet — no active provider or first poll pending.",
            "conditions_met": [],
            "confidence": 0.0,
            "can_execute": False,
            "baseline_missing": True,
            "engine_mode": engine_mode,
            "dispatch_strategy": "info",  # no pricing = effectively info only
            "evaluated_at": None,
            "context": {
                "import_c_kwh": None, "tariff_type": None,
                "spike_status": None, "descriptor": None, "soc_pct": None,
            },
        }

    presets_mgr = _get_presets_mgr()
    smart_dispatch_engine.set_presets_manager(presets_mgr)

    soc_pct = _live_soc(short_id)
    result = await smart_dispatch_engine.evaluate_rules(snap, soc_pct=soc_pct, gateway_id=short_id, provider=provider)

    current_period = next(
        (p for p in snap.forecast if getattr(p, "is_current", False)), None
    )
    context = {
        "import_c_kwh":   snap.import_c_kwh,
        "export_c_kwh":   snap.export_c_kwh,
        "tariff_type":    snap.tariff_type,
        "spike_status":   (snap.spike_status or "").lower(),
        "descriptor":     getattr(current_period, "descriptor", None) if current_period else None,
        "tariff_period":  getattr(current_period, "tariff_period", None) if current_period else None,
        "demand_window":  snap.demand_window,
        "renewables_pct": snap.renewables_pct,
        "soc_pct":        soc_pct,
        "provider":       snap.provider,
    }

    # Resolve per-gateway (or global) dispatch_strategy for the frontend badge
    gw_id = short_id or (result.to_dict().get("gateway_serial") if hasattr(result, "to_dict") else None)
    try:
        # Use gateway-specific config if short_id provided, else fall back to global scope
        gw_cfg = await db.get_smart_dispatch_config(gw_id) if gw_id else await db.get_smart_dispatch_config("global")
    except Exception:
        gw_cfg = {}
    raw_strategy = gw_cfg.get("strategy_mode", "auto")
    notif_mode = gw_cfg.get("notification_mode")
    # Auto-heal historical database inconsistencies
    if raw_strategy == "info" and notif_mode == "ask":
        raw_strategy = "user_approval"
    elif raw_strategy == "info" and notif_mode == "auto":
        raw_strategy = "auto"
        
    _strat_norm = {"passive": "info", "active": "auto", "proactive": "auto"}
    dispatch_strategy = _strat_norm.get(raw_strategy, raw_strategy)
    # Global engine_mode kill switch overrides active strategies to info,
    # but NEVER overrides 'disabled' — that is a deliberate per-gateway user choice.
    if engine_mode != "active" and dispatch_strategy != "disabled":
        dispatch_strategy = "info"  # global kill switch

    shadow_mode = bool(gw_cfg.get("shadow_mode", 0))
    return {**result.to_dict(), "context": context, "engine_mode": engine_mode, "dispatch_strategy": dispatch_strategy, "shadow_mode": shadow_mode}


# ── Rules CRUD ────────────────────────────────────────────────────────────────

@router.get("/automation/rules")
async def list_automation_rules(
    rulebook_id: Optional[str] = Query(default=None),
    include_disabled: bool = Query(default=True),
):
    """Return all rules for the active (or specified) rulebook."""
    if rulebook_id:
        rules = await db.get_all_rules(rulebook_id=rulebook_id)
    else:
        rb = await db.get_active_rulebook()
        if not rb:
            return {"rules": [], "rulebook": None}
        rules = await db.get_all_rules(rulebook_id=rb["rulebook_id"])
        rulebook_id = rb["rulebook_id"]

    if not include_disabled:
        rules = [r for r in rules if r.get("enabled")]

    for rule in rules:
        try:
            rule["condition"] = json.loads(rule.get("condition_json") or "{}")
        except Exception:
            rule["condition"] = {}
        try:
            rule["params"] = json.loads(rule.get("action_params") or "{}")
        except Exception:
            rule["params"] = {}

    return {"rules": rules, "rulebook_id": rulebook_id}


@router.put("/automation/rules/priority")
async def update_automation_rule_priority(payload: dict):
    """
    Update the priority of multiple rules.
    Payload: { "rule_ids": ["rule_id_1", "rule_id_2", ...] }
    """
    rule_ids = payload.get("rule_ids", [])
    if not isinstance(rule_ids, list):
        raise HTTPException(status_code=400, detail="rule_ids must be a list of strings")
    
    ok = await db.update_rule_priorities(rule_ids)
    return {"ok": ok, "updated_count": len(rule_ids)}



@router.put("/automation/rules/{rule_id}/toggle")
async def toggle_automation_rule(rule_id: str, payload: dict):
    """Enable or disable an automation rule. Payload: { "enabled": bool }"""
    enabled = bool(payload.get("enabled", True))
    ok = await db.toggle_rule(rule_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"ok": True, "rule_id": rule_id, "enabled": enabled}


# ── Rulebooks ─────────────────────────────────────────────────────────────────

@router.get("/automation/rulebooks")
async def list_rulebooks():
    """Return all configured rulebooks."""
    books = await db.get_rulebooks()
    return {"rulebooks": books}


# ── History (filterable) ──────────────────────────────────────────────────────

@router.get("/automation/history")
async def automation_history(
    limit: int = Query(default=50, le=500),
    gateway_serial: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None, description="Filter: 'amber' | 'edge'"),
):
    """
    Return recent automation trigger history (newest first).
    Use source=amber to get only Smart Dispatch events.
    Use source=edge to get only Edge Automation events.
    """
    rows = await db.get_automation_history(
        limit=limit,
        gateway_serial=gateway_serial,
        source=source,
    )
    return {"history": rows, "count": len(rows)}


# ── Reserved preset metadata ──────────────────────────────────────────────────

@router.get("/automation/presets/reserved")
async def list_reserved_presets():
    """Return metadata for all reserved Smart Dispatch preset names."""
    presets_mgr = _get_presets_mgr()
    reserved = db.get_smart_dispatch_reserved_presets()

    for preset in reserved:
        name = preset["name"]
        if presets_mgr:
            result = presets_mgr.load_preset(name)
            preset["configured"] = bool(result.get("success"))
            if result.get("success"):
                schedule = result.get("schedule", [])
                preset["block_count"] = len(schedule)
                preset["is_unverified"] = bool(result.get("unverified", False))
            else:
                preset["block_count"] = 0
                preset["is_unverified"] = True
        else:
            preset["configured"] = False
            preset["block_count"] = 0
            preset["is_unverified"] = True

    baseline_configured = any(
        p["name"] == "Smart Default" and p["configured"] for p in reserved
    )

    return {
        "reserved_presets": reserved,
        "baseline_configured": baseline_configured,
        "can_execute": baseline_configured,
        "warning": None if baseline_configured else (
            "Auto-execution is disabled: configure and save the 'Smart Default' "
            "preset via the Schedule tab to enable it."
        ),
    }


# ── Engine mode ───────────────────────────────────────────────────────────────

@router.put("/automation/engine/mode")
async def set_engine_mode(payload: EngineModePayload):
    """
    Set the Smart Dispatch engine mode.

    signal_only — Evaluates rules and reports signal, never pushes (default, safe)
    active       — Evaluates and auto-pushes presets to gateway (requires verified presets)
    paused       — Engine completely dormant, signal returns { action: 'PAUSED' }
    """
    valid = {"signal_only", "active", "paused"}
    if payload.mode not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{payload.mode}'. Must be one of: {sorted(valid)}",
        )
    ok = await db.set_engine_mode(payload.mode)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Rulebook 'amber-au-default' not found. Run seed first.",
        )
    return {"ok": True, "mode": payload.mode}


# ── Notifications ─────────────────────────────────────────────────────────────

@router.get("/automation/notifications/targets")
async def get_notify_targets():
    """
    Discover available HA notify service targets by querying the live HA instance.
    Returns mobile_app_* services first (most relevant for push notifications).
    """
    from src.services.notification_sender import discover_notify_targets
    return await discover_notify_targets()


@router.get("/automation/notifications/settings")
async def get_notification_settings():
    """
    Return notification settings + HA connection status.
    If HA is not configured, ha_connected=False and all controls should be disabled in UI.
    """
    settings = await db.get_notification_settings()

    # Check HA connectivity
    ha_host  = await db.get_config_value("ha_host",  "") or os.environ.get("HA_HOST",  "")
    ha_token = await db.get_config_value("ha_token", "") or os.environ.get("HA_TOKEN", "")
    ha_token_set = bool(ha_token or os.environ.get("SUPERVISOR_TOKEN", ""))

    # Generate HA YAML recipe for actionable notifications
    from src.services.notification_sender import generate_ha_automation_yaml
    host = await _fhai_host()
    ha_yaml = generate_ha_automation_yaml(host) if ha_token_set else ""

    return {
        **settings,
        "ha_connected":  bool(ha_host and ha_token_set),
        "ha_host":       ha_host or "",
        "fhai_host":     host,
        "ha_token_set":  ha_token_set,
        "ha_automation_yaml": ha_yaml,
        "available_triggers": [
            {"id": "spike",          "label": "Price Spike detected"},
            {"id": "force_charge",   "label": "Force Charge window opening"},
            {"id": "peak_start",     "label": "Peak tariff period starting"},
            {"id": "signal_change",  "label": "Signal changed (any)"},
            {"id": "daily_summary",  "label": "Daily summary (07:00)"},
            {"id": "cert_expiry",    "label": "TLS certificate expiring"},
        ],
    }


@router.put("/automation/notifications/settings")
async def save_notification_settings(payload: NotificationSettingsPayload):
    """Save notification settings."""
    # Merge over what is stored: the upsert replaces the row wholesale, so
    # anything not sent must be carried forward rather than defaulted away.
    current = await db.get_notification_settings() or {}
    merged = {
        "enabled":        current.get("enabled", False),
        "ha_target":      current.get("ha_target", ""),
        "triggers":       current.get("triggers", []),
        "actionable":     current.get("actionable", False),
        "actionable_ttl": current.get("actionable_ttl", 1800),
    }
    for field in merged:
        sent = getattr(payload, field, None)
        if sent is not None:
            merged[field] = sent

    # Record what changed. Muting notifications is exactly the change someone
    # needs to find later — "why did nothing alert me last night?" — and it
    # left no trace anywhere: the switch wrote the row and returned ok.
    BOOLEAN_FIELDS = ("enabled", "actionable")
    changes = []
    for field in ("enabled", "ha_target", "actionable", "actionable_ttl", "triggers"):
        before, after = current.get(field), merged[field]
        if field in BOOLEAN_FIELDS:
            # SQLite stores these as 0/1 and the payload sends true/false,
            # so compare meaning rather than representation.
            if bool(before) != bool(after):
                changes.append(f"{field} {'ON' if before else 'OFF'}→{'ON' if after else 'OFF'}")
        elif before != after:
            changes.append(f"{field} {before!r}→{after!r}")

    await db.upsert_notification_settings(merged)

    if changes:
        summary = "; ".join(changes)[:150]
        logger.info("notification settings changed: %s", summary)
        try:
            await db.add_notification_log("CONFIG", "settings_changed", summary)
            await db.log_admin_audit(
                event="notification_settings_changed",
                source="ui",
                details=summary,
            )
        except Exception:
            logger.debug("could not record the settings change", exc_info=True)
    # NOTE: HA credentials (ha_host, ha_token) are now managed solely in the Home Automation settings tab.
    # We do NOT write ha_host or ha_token to db here to ensure single source of truth.
    if payload.fhai_host:
        await db.set_config_value("fhai_host", payload.fhai_host.rstrip("/"))
    return {"ok": True}

import time
import datetime

TEST_RESULTS = {}
async def _add_debug_log(direction: str, event_type: str, details: str):
    """Add a bounded entry to the notification debug log in DB."""
    await db.add_notification_log(direction, event_type, details)

@router.get("/automation/notifications/templates")
async def get_notification_templates():
    """Get the current notification templates, both defaults and customizations."""
    from src.services.notification_sender import DEFAULT_EVENT_TITLES, DEFAULT_EVENT_MESSAGES
    custom_templates = await db.get_config_value("notification_templates", {}) or {}
    return {
        "ok": True,
        "defaults": {
            "titles": DEFAULT_EVENT_TITLES,
            "messages": DEFAULT_EVENT_MESSAGES
        },
        "custom": custom_templates
    }

@router.put("/automation/notifications/templates")
async def save_notification_templates(payload: TemplatesPayload):
    """Save custom notification templates and log the change."""
    await db.set_config_value("notification_templates", payload.templates)
    
    # Audit trail
    registry = _get_registry()
    gw_serial = ""
    if registry and hasattr(registry, "list_gateways"):
        gws = registry.list_gateways()
        gw_serial = gws[0] if gws else ""
        
    await db.log_automation_trigger(
        rule_id="__templates_updated__",
        rule_name="Notification Templates Updated",
        gateway_serial=gw_serial,
        action_type="CONFIG_CHANGE",
        status="executed",
        detail="User updated notification templates.",
        source="amber"
    )
    return {"ok": True}

class TestNotificationPayload(BaseModel):
    event_type: str = "test"

@router.post("/automation/notifications/test")
async def send_test_notification(payload: TestNotificationPayload = None):
    """Send a test push notification to the configured HA target."""
    from src.services.notification_sender import send_ha_notification

    event_type = payload.event_type if payload else "test"
    actual_event_type = "user_approval_request" if event_type == "hold_approval" else event_type

    settings = await db.get_notification_settings()
    if not settings.get("enabled"):
        raise HTTPException(status_code=400, detail="Global Notifications are toggled OFF. Please enable them to test.")
    
    gateways = await db.get_all_gateways()
    sd_config = {}
    if gateways:
        sd_config = await db.get_smart_dispatch_config(gateways[0].get("id"))

    if sd_config.get("dispatch_mode", "ask").lower() == "silent":
        raise HTTPException(status_code=400, detail="Event Preferences are set to SILENT Mode. No notifications can be sent.")

    pricing_registry = _get_pricing_registry()
    pricing_svc = pricing_registry.get_primary_service() if pricing_registry else None
    snap = pricing_svc.get_snapshot() if pricing_svc else None
    request_id = str(uuid.uuid4())
    ctx = {
        "action":       "HOLD" if event_type == "hold_approval" else "TEST",
        "preset_name":  "Smart Default",
        "import_c_kwh": snap.import_c_kwh if snap else None,
        "tariff_type":  snap.tariff_type if snap else "—",
        "rule_name":    "Test Rule",
        "dispatch_summary": "Test Summary",
        "summary":      "Test Daily Summary",
        "decision_hash": f"test_{str(uuid.uuid4())[:8]}",
        "gateway_serial": gateways[0].get("full_serial") if gateways else "99900001",
        "rule_id":      "test_rule",
    }

    # If it's a reconciliation-type test, register a dummy pending record
    # so the callback actually finds something in the DB.
    #
    # rule_id="__test__" is a sentinel checked by the callback handler
    # (POST /automation/notifications/override) to short-circuit BEFORE any
    # hardware dispatch (RESUME_NATIVE / execute_sd_signal_list). Without
    # this, a user tapping Override on a Test notification triggers a real
    # state change on the physical gateway — see backlog P1 #1 dated
    # 2026-07-09 for the incident that motivated this sentinel.
    if event_type in ("user_approval_request", "hold_approval", "force_charge", "spike"):
        await db.set_pending_approval(
            gateway_serial=ctx["gateway_serial"],
            request_id=request_id,
            rule_id="__test__",
            rule_name="Test Approval Flow",
            action="HOLD" if event_type == "hold_approval" else "TEST_ACTION",
            dispatch_summary="This is a test notification generated by the UI."
        )

    result = await send_ha_notification(actual_event_type, ctx, force_actionable=True, request_id=request_id)

    # Log the outcome the UI is about to receive, at WARNING so no level
    # setting can hide it. Three rounds of diagnosis stalled because the
    # dispatch console showed "no per-device results" while the add-on log
    # said nothing at all about the test having run.
    _rows = result.get("results") or []
    logger.warning(
        "notification test [%s]: sent=%s results=%d — %s",
        event_type, result.get("sent"), len(_rows),
        json.dumps([{"alias": r.get("alias"), "ok": r.get("ok"),
                     "status": r.get("status_code"), "error": r.get("error")}
                    for r in _rows], separators=(",", ":")) if _rows
        else "NO PER-DEVICE RESULTS — nothing was dispatched to any device",
    )

    if not result.get("sent"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Notification failed"),
        )
    return {"ok": True, "result": result}

@router.get("/automation/notifications/verify-yaml")
async def verify_ha_yaml_installation():
    """
    Silent audit: probe HA directly for the required services and automations.
    This replaces the 'Test Notification' round-trip for YAML verification.
    """
    from src.routes.api_ha import _ha_get
    results = {
        "rest_command": False,
        "automation": False,
        "details": []
    }
    
    # 1. Check services for rest_command.franklinwh_action_callback
    services = await _ha_get("/services")
    if services:
        found_rest = False
        for domain_obj in services:
            if domain_obj.get("domain") == "rest_command":
                svcs = domain_obj.get("services", {})
                if "franklinwh_action_callback" in svcs:
                    found_rest = True
                    break
        results["rest_command"] = found_rest
        results["details"].append(f"rest_command.franklinwh_action_callback: {'✓ Found' if found_rest else '✗ Missing'}")
    else:
        results["details"].append("✗ Failed to reach HA services API")

    # 2. Check state of automation.franklinwh_smart_dispatch_action_callback
    auto_state = await _ha_get("/states/automation.franklinwh_smart_dispatch_action_callback")
    is_on = False
    entity_checked = "automation.franklinwh_smart_dispatch_action_callback"
    if auto_state:
        is_on = auto_state.get("state") == "on"
        if not is_on:
            # Check for name collisions suffix (e.g. _2)
            auto_state_2 = await _ha_get("/states/automation.franklinwh_smart_dispatch_action_callback_2")
            if auto_state_2 and auto_state_2.get("state") == "on":
                auto_state = auto_state_2
                is_on = True
                entity_checked = "automation.franklinwh_smart_dispatch_action_callback_2"
    else:
        # If the direct one is missing, check the _2 version
        auto_state_2 = await _ha_get("/states/automation.franklinwh_smart_dispatch_action_callback_2")
        if auto_state_2:
            auto_state = auto_state_2
            is_on = auto_state_2.get("state") == "on"
            entity_checked = "automation.franklinwh_smart_dispatch_action_callback_2"

    if auto_state:
        results["automation"] = is_on
        results["details"].append(f"{entity_checked}: {'✓ Loaded' if is_on else '⚠ Disabled'}")
    else:
        results["details"].append("✗ automation.franklinwh_smart_dispatch_action_callback: Not found in HA")

    ok = results["rest_command"] and results["automation"]
    return {
        "ok": ok,
        "results": results,
        "msg": "✓ Setup Verified: YAML blueprint detected in HA!" if ok else "✗ Verification failed: Check HA configuration.yaml"
    }


# ── Batch O.3 (2026-07-29) — Diagnostic audit endpoints ─────────────────
# Thin FastAPI wrappers over src.services.audit_queries.* — same query
# primitives the CLI (scripts/audit_notifications.py) uses so the UI +
# CLI never disagree on anomaly rules.

@router.get("/automation/notifications/audit")
async def audit_notifications_endpoint(
    last: str = Query("72h", description="Time window: 72h / 24h / 7d / 30m"),
    event: Optional[str] = Query(None, description="Filter by event type"),
    direction: Optional[str] = Query(None, description="SENT / RECEIVED / SUPPRESSED / ERROR"),
    anomalies_only: bool = Query(False, description="Only rows flagged as anomalies"),
):
    """Correlated notification audit — joins automation_notification_log
    against gateway_metrics + pricing_eval_log at each notification's
    timestamp so consumers can see WHY it fired.

    Anomaly classification uses the same rules as Batch O.2's
    SmartDispatchEngine._is_event_stale gate — if a row is flagged
    'charging-vs-export' etc. that's the gate that SHOULD have
    suppressed it (post-fix these should stop appearing)."""
    from src.services import audit_queries
    async with db.get_db() as conn:
        rows = await audit_queries.audit_notifications_async(
            conn, last=last, event=event, direction=direction,
            anomalies_only=anomalies_only,
        )
    anomaly_count = sum(1 for r in rows if r.get("anomaly"))
    return {
        "ok": True,
        "count": len(rows),
        "anomaly_count": anomaly_count,
        "window": last,
        "rows": rows,
    }


@router.get("/automation/notifications/audit/stale-window")
async def audit_stale_window_endpoint(
    last: str = Query("24h", description="Time window"),
):
    """gateway_metrics rows matching Batch I's stale-window signature
    (soc=0 + batt=0 + grid=0 + Standby). These are already dropped
    inline by _normalise_stats — this endpoint reports HISTORICAL
    rows that slipped through before the fix landed OR before the
    threshold widened."""
    from src.services import audit_queries
    async with db.get_db() as conn:
        rows = await audit_queries.stale_window_async(conn, last=last)
    return {"ok": True, "count": len(rows), "window": last, "rows": rows}


@router.get("/automation/notifications/audit/cooldowns")
async def audit_cooldowns_endpoint(
    rule: Optional[str] = Query(None, description="Filter by rule_id"),
    last: Optional[str] = Query(None, description="Time window (default all-time)"),
):
    """notification_cooldown rows — evidence of expired-and-ignored
    pendings converted by Batch C's sweep."""
    from src.services import audit_queries
    async with db.get_db() as conn:
        rows = await audit_queries.cooldowns_async(conn, rule=rule, last=last)
    return {"ok": True, "count": len(rows), "rows": rows}


@router.get("/automation/notifications/audit/shadow-decisions")
async def audit_shadow_decisions_endpoint(
    last: str = Query("24h", description="Time window"),
    reason: Optional[str] = Query(None, description="Filter by shadow_reason (e.g. vpp_active)"),
):
    """Batch P (2026-07-30): SD decisions where the engine deferred to
    an external controller. Populated from pricing_eval_log.shadow_reason
    whenever VPP / Modbus / Manual owned the gateway during a tick.
    Provides the audit trail complementary to the SUPPRESSED notifi-
    cation entries: what SD wanted to DECIDE but didn't act on."""
    from src.services import audit_queries
    async with db.get_db() as conn:
        rows = await audit_queries.shadow_decisions_async(conn, last=last, reason=reason)
    return {"ok": True, "count": len(rows), "window": last, "rows": rows}


@router.get("/automation/notifications/audit/multi-emit")
async def audit_multi_emit_endpoint(
    last: str = Query("7d", description="Time window"),
    min_count: int = Query(2, ge=2, le=10, description="Minimum events per cluster"),
):
    """Timestamps where >=N SENT notifications share the same second.
    Post-Batch M-3 (tick collapse) these should mostly be legitimate
    actionable + info emits for the same rule, not spam."""
    from src.services import audit_queries
    async with db.get_db() as conn:
        rows = await audit_queries.multi_emit_async(conn, last=last, min_count=min_count)
    return {"ok": True, "count": len(rows), "window": last, "rows": rows}


@router.get("/automation/notifications/debug-log")
async def get_notification_debug_log():
    """Retrieve persistent notification logs from DB."""
    logs = await db.get_notification_logs(limit=50)
    return {"ok": True, "log": logs}


@router.get("/automation/notifications/cooldown-rules")
async def get_notification_cooldown_rules():
    """Return the per-rule notification cooldown configuration used by the
    'ignored → cooldown' dedup mechanism (schema v47). Each row is one of
    the 7 canonical ev_key rule categories with its cooldown window in
    seconds and enabled flag. When enabled=0, that rule never cools down
    (matrix re-fires as before)."""
    rules = await db.get_notification_cooldown_rules()
    return {"ok": True, "rules": rules}


@router.put("/automation/notifications/cooldown-rules")
async def update_notification_cooldown_rules(payload: dict):
    """Update one or more cooldown rules. Body:
        {"rules": [{"rule_id": "spike", "cooldown_seconds": 7200, "enabled": 1}, ...]}
    Only the provided rows are touched; omitted rows keep their current
    values. Server-side clamp: cooldown_seconds ∈ [60, 86400]."""
    rules = payload.get("rules") or []
    if not isinstance(rules, list):
        return {"ok": False, "error": "rules must be a list"}
    updated: list[str] = []
    for r in rules:
        try:
            rid = str(r.get("rule_id") or "").strip()
            if not rid:
                continue
            cd = int(r.get("cooldown_seconds") or 0)
            en = int(r.get("enabled") if r.get("enabled") is not None else 1)
            cd = max(60, min(86400, cd))
            en = 1 if en else 0
            await db.update_notification_cooldown_rule(rid, cooldown_seconds=cd, enabled=en)
            updated.append(rid)
        except Exception as _:
            continue
    latest = await db.get_notification_cooldown_rules()
    return {"ok": True, "updated": updated, "rules": latest}


@router.get("/automation/notifications/test-result")
async def get_test_notification_result():
    """Poll for the result of the last test notification."""
    now = time.time()
    # Clean up old results but don't clear the latest one immediately on read
    # to avoid race conditions with UI polling.
    for k, v in list(TEST_RESULTS.items()):
        if now - v["timestamp"] > 60:
            TEST_RESULTS.pop(k, None)
            
    if TEST_RESULTS:
        # return the latest one
        latest = max(TEST_RESULTS.values(), key=lambda x: x["timestamp"])
        # We only clear it if it's older than 5 seconds, allowing the UI 
        # a window to catch it if multiple rapid polls happen.
        if now - latest["timestamp"] > 5:
            TEST_RESULTS.clear()
        return {"ok": True, "action": latest["action"]}
    return {"ok": False, "action": None}



@router.post("/automation/notifications/test-webhook")
async def test_webhook(payload: TestWebhookPayload):
    """
    Test if the integrator can reach itself via the provided Callback URL.
    This helps verify network/Docker routing without CORS issues.
    """
    target_url = payload.url.rstrip("/") + "/api/automation/notifications/override"
    logger.info(f"test_webhook: attempting self-ping to {target_url}")
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Send a dummy test approve action
            resp = await client.post(target_url, json={
                "action_id": "FWH_TEST_APPROVE_V2",
                "request_id": "test-webhook-ping"
            })
            
        if resp.status_code == 200:
            return {"ok": True, "status": resp.status_code, "note": "Integrator successfully reached itself."}
        else:
            return {"ok": False, "status": resp.status_code, "note": f"Server responded with {resp.status_code}"}
    except Exception as e:
        logger.error(f"test_webhook failed: {e}")
        return {"ok": False, "error": str(e), "note": "Connection failed. Check IP/Port and Firewall."}


@router.post("/automation/notifications/test-callback-path")
async def test_callback_path():
    """Ask Home Assistant to invoke its own rest_command, and see if it lands.

    The outbound leg is easy to verify — the push either reaches the phone or
    it does not. The return leg is not: a tap produces nothing observable here
    when the rest_command URL is wrong, so "no response recorded" looks
    identical to "the user never tapped", and the only signal is the console
    eventually saying it timed out.

    This drives the return leg directly. HA is told to run
    rest_command.franklinwh_action_callback with a sentinel request_id; if that
    POST arrives at /override, the URL in the user's configuration.yaml is
    reachable and correct, and any remaining failure is the automation or the
    phone. If it does not arrive, the rest_command URL is the fault — which is
    the half nobody could see.
    """
    import asyncio
    import uuid as _uuid

    import httpx

    from src.routes.api_ha import _get_ha_client

    base, auth, _env = await _get_ha_client()
    if not base or not auth:
        raise HTTPException(status_code=400, detail="No Home Assistant connection is configured.")

    sentinel = f"loopback-{_uuid.uuid4()}"
    gateways = await db.get_all_gateways()
    gateway_serial = gateways[0].get("full_serial") if gateways else "unknown"

    await db.set_pending_approval(
        gateway_serial=gateway_serial,
        request_id=sentinel,
        rule_id="__test__",
        rule_name="Callback path test",
        action="TEST_ACTION",
        dispatch_summary="Loopback test of the REST callback path.",
    )

    url = f"{base.rstrip('/')}/api/services/rest_command/franklinwh_action_callback"
    body = {
        "action_id":  "FWH_TEST_APPROVE_V2",
        "gateway_id": gateway_serial,
        "request_id": sentinel,
        "ha_user_id": "loopback-test",
        "response":   "",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers={"Authorization": auth,
                                                   "Content-Type": "application/json"},
                                     json=body)
    except Exception as exc:
        logger.warning("callback path test: could not reach Home Assistant — %r", exc)
        return {"ok": False, "stage": "calling_ha",
                "detail": f"Could not reach Home Assistant at {base}: {exc}"}

    if resp.status_code == 400 and "rest_command" in resp.text:
        return {"ok": False, "stage": "rest_command_missing",
                "detail": "Home Assistant has no rest_command.franklinwh_action_callback. "
                          "Paste the blueprint YAML into configuration.yaml and restart HA."}
    if resp.status_code >= 400:
        return {"ok": False, "stage": "calling_ha",
                "detail": f"Home Assistant returned {resp.status_code}: {resp.text[:200]}"}

    # HA returns as soon as it has dispatched the service; the POST back to us
    # is a separate connection, so give it a moment to arrive.
    # The override handler clears a "__test__" pending record as soon as it
    # runs, so the record disappearing is the proof that our POST arrived.
    for _ in range(20):
        await asyncio.sleep(0.25)
        pending = await db.get_pending_approval(request_id=sentinel)
        if not pending:
            logger.info("callback path test: the return leg works (req=%s)", sentinel)
            return {"ok": True, "stage": "complete",
                    "detail": "Home Assistant reached this add-on. The callback path works — "
                              "any remaining failure is the automation trigger or the phone."}

    logger.warning(
        "callback path test: HA ran the rest_command but nothing arrived at /override — "
        "the URL in rest_command.franklinwh_action_callback is probably not reachable from HA"
    )
    try:
        await db.clear_pending_approval(request_id=sentinel)
    except Exception:
        logger.debug("callback path test: could not clear the sentinel", exc_info=True)

    return {"ok": False, "stage": "no_callback_received",
            "detail": "Home Assistant ran the rest_command, but nothing arrived here. "
                      "The URL in rest_command.franklinwh_action_callback is wrong or "
                      "unreachable from Home Assistant — check it ends in /override."}


@router.post("/automation/notifications/callback")
async def notification_callback_compat(payload: OverridePayload):
    """Accept the URL the UI used to advertise, and say so.

    The Home Automation tab displayed `/api/automation/notifications/callback`
    as the "HA Callback Target" while the only route was `/override`. Anyone
    who copied it into their rest_command had every response 404 in silence —
    the tap worked, the automation fired, and nothing was ever recorded.

    Handling it is not enough on its own: a silent success would leave the
    wrong URL in place forever. Log it at WARNING so the fix is findable.
    """
    logger.warning(
        "notification callback arrived on the retired /callback path — "
        "update the rest_command URL to end in /override (req=%r)",
        payload.get_request_id(),
    )
    return await notification_override(payload)


@router.post("/automation/notifications/override")
async def notification_override(payload: OverridePayload):
    """
    HA webhook callback for actionable notifications.

    Called by HA automations when the user taps an action on their mobile device.
    FWH_APPROVE: look up pending_approval for gateway, execute the action, clear record.
    FWH_SKIP:    clear pending_approval (no execution), log to audit trail.
    FWH_FORCE_CHARGE / FWH_SPIKE_HOLD: legacy pre-User-Approval actions (still supported).
    """
    from src.services.smart_dispatch import smart_dispatch_engine

    # Resolve gateway serial — use payload field or fall back to first active gw
    action_id = payload.get_action()
    gw_serial = payload.get_gateway()
    
    if not gw_serial:
        registry = _get_registry()
        if registry:
            gws = registry.list_gateways() if hasattr(registry, "list_gateways") else []
            gw_serial = gws[0] if gws else ""

    logger.info(
        f"automation/override: action={action_id!r} gw={gw_serial!r} "
        f"req={payload.request_id!r} user={payload.ha_user_id!r}"
    )
    
    details = (
        f"Action: {action_id}\n"
        f"Gateway: {gw_serial or 'not specified'}\n"
        f"Request ID: {payload.get_request_id() or 'none'}\n"
        f"User ID: {payload.ha_user_id or 'unknown'}"
    )
    if payload.response:
        details += f"\nResponse: {payload.response}"
        
    await _add_debug_log("RECEIVED", action_id, details)

    # ── TEST ACTIONS ─────────────────────────────────────────────────────────────
    # iOS actionable notifications only return the 'action' string and do not echo
    # back arbitrary payload fields like decision_hash. To ensure the UI Test Modal 
    # detects the callback regardless of which dropdown test the user selected, we 
    # always populate TEST_RESULTS with the latest received action.
    TEST_RESULTS["test"] = {
        "action": action_id,
        "timestamp": time.time()
    }

    if action_id in ("FWH_TEST_APPROVE_V2", "FWH_TEST_DENY_V2", "FWH_TEST_APPROVE", "FWH_TEST_DENY"):
        return {
            "ok": True,
            "action": action_id,
            "note": "Test notification callback received successfully.",
        }

    # ── TEST-ORIGINATED SHORT-CIRCUIT ──────────────────────────────────────────
    # Test notifications fired via POST /automation/notifications/test carry a
    # pending_approval record with rule_id="__test__" (new sentinel) or
    # rule_id="test_rule" (legacy — pre-2026-07-10 test firings before the
    # sentinel was introduced). Test-originated FWH_APPROVE / FWH_OVERRIDE /
    # FWH_SKIP callbacks must be treated as no-op acknowledgements — they
    # must NOT bridge RESUME_NATIVE, execute_sd_signal_list, or any real
    # hardware command. This block enforces that guarantee for all three
    # actionable branches in a single place.
    #
    # Root cause: backlog P1 #1 dated 2026-07-09 — user tapped Override on
    # a Test notification and inadvertently pushed the gateway to TOU Standby.
    if action_id in (
        "FWH_APPROVE_V2", "FWH_APPROVE",
        "FWH_OVERRIDE_V2", "FWH_OVERRIDE",
        "FWH_SKIP_V2", "FWH_SKIP",
    ):
        _test_req_id = payload.get_request_id()
        if _test_req_id:
            _test_pending = await db.get_pending_approval(
                gateway_serial=gw_serial, request_id=_test_req_id
            )
            if _test_pending and _test_pending.get("rule_id") in ("__test__", "test_rule"):
                await db.clear_pending_approval(request_id=_test_pending["request_id"])
                await db.log_automation_trigger(
                    rule_id="__test__",
                    rule_name="Test Callback Short-Circuited",
                    gateway_serial=gw_serial,
                    action_type=action_id,
                    status="skipped",
                    detail=(
                        f"Test-originated pending record (req={_test_req_id}) — "
                        f"no hardware dispatch. Action button: {action_id}."
                    ),
                    source="amber",
                    request_id=_test_req_id,
                    ha_user_id=payload.ha_user_id,
                )
                logger.info(
                    f"automation/override [TEST]: {action_id} received for "
                    f"test-originated req={_test_req_id!r} — short-circuited, "
                    f"no hardware action"
                )
                return {
                    "ok": True,
                    "action": action_id,
                    "note": "Test callback received — no hardware dispatch executed.",
                    "test_mode": True,
                }

    # ── FWH_APPROVE: execute the pending action ─────────────────────────────────
    if action_id in ("FWH_APPROVE_V2", "FWH_APPROVE"):
        req_id = payload.get_request_id()

        # request_id is MANDATORY on this path — it is the only credential.
        #
        # This route is in AUTH_EXEMPT_PREFIXES because Home Assistant's
        # notification callback cannot easily carry a session, so anyone who can
        # reach the port can POST here. The legacy fallback below used to look up
        # "latest pending for this gateway" when no request_id was supplied,
        # which meant an unauthenticated LAN host could POST
        # {"action_id":"FWH_APPROVE","gateway_id":"..."} and approve whatever
        # dispatch happened to be queued — a real hardware command, with no
        # secret required.
        #
        # request_id is already a uuid4 (notification_sender.py:321,
        # smart_dispatch/__init__.py:2238) minted per approval request and
        # echoed back verbatim by the HA blueprint, so 122 bits of entropy are
        # ALREADY on the wire. Requiring it turns it into what it always
        # effectively was — a capability token — and needs no blueprint change,
        # unlike signing the payload.
        if not req_id:
            logger.warning(
                f"automation/override: rejecting FWH_APPROVE with no request_id "
                f"(gw={gw_serial!r}) — this path executes hardware and is unauthenticated"
            )
            await db.log_automation_trigger(
                rule_id="__approval_unidentified__",
                rule_name="User Approval — Missing request_id",
                gateway_serial=gw_serial,
                action_type="FWH_APPROVE",
                status="skipped",
                detail="Rejected: approval callback carried no request_id.",
                source="amber",
            )
            return JSONResponse(status_code=400, content={
                "ok": False,
                "action": "FWH_APPROVE",
                "kind": "unidentified",
                "note": (
                    "request_id is required. Re-import the notification blueprint "
                    "from Notifications → Setup if your automation predates it."
                ),
                "gateway_serial": gw_serial,
            })

        pending = await db.get_pending_approval(gateway_serial=gw_serial, request_id=req_id)

        if not pending:
            # Batch L (2026-07-21) — differentiate "expired" (410) from
            # "unknown" (404). Consumers can distinguish "user was too slow"
            # from "handler couldn't find anything to route to".
            status_code, kind = await _diagnose_missing_pending(gw_serial, req_id)
            await db.log_automation_trigger(
                rule_id="__approval_expired__" if kind == "expired" else "__approval_unknown__",
                rule_name=f"User Approval — {kind.title()}",
                gateway_serial=gw_serial,
                action_type="FWH_APPROVE",
                status="skipped",
                detail=f"{kind.title()} pending approval for req={payload.request_id or 'legacy'}.",
                source="amber",
            )
            return JSONResponse(status_code=status_code, content={
                "ok": False,
                "action": "FWH_APPROVE",
                "kind": kind,
                "note": (
                    "Pending approval expired (TTL exceeded)." if kind == "expired"
                    else "No matching pending approval — request_id unknown."
                ),
                "request_id": req_id,
                "gateway_serial": gw_serial,
            })

        approved_action = pending.get("action", "")
        approved_rule   = pending.get("rule_name", "")

        # Clear the pending record before executing (prevents double-execution)
        await db.clear_pending_approval(request_id=pending["request_id"])

        # Log approval to audit trail with user identity
        user_info = f" (by {payload.ha_user_id})" if payload.ha_user_id else ""
        await db.log_automation_trigger(
            rule_id="__user_approved__",
            rule_name=f"User Approved: {approved_rule}",
            gateway_serial=gw_serial,
            action_type="FWH_APPROVE",
            status="executed",
            detail=f"User approved '{approved_action}'{user_info}. Dispatching now.",
            source="amber",
            request_id=pending["request_id"],
            ha_user_id=payload.ha_user_id
        )

        # Execute the action via AutomationEngine bridge
        exec_note = "Executing via approved User Approval dispatch."
        try:
            automation_engine = get_app_state().get("scheduler")
            if automation_engine:
                await automation_engine.execute_sd_signal_list(
                    gateway_serial=gw_serial,
                    signals=[{"order": 1, "signal": approved_action}],
                    dispatch_guid=f"UA-APPROVE-{int(time.time())}"
                )
                exec_note = f"Executed: {approved_action} across all active gateways."
                logger.info(f"automation/override [APPROVED]: {approved_action} bridged to AutomationEngine for gw={gw_serial!r}")
            else:
                exec_note = "Execution failed: AutomationEngine not found."
                logger.error(f"automation/override [APPROVED]: could not find scheduler in app_state")
        except Exception as exc:
            exec_note = f"Execution attempt failed: {exc}"
            logger.error(f"automation/override [APPROVED]: execution failed — {exc}")

        return {
            "ok": True,
            "action": "FWH_APPROVE",
            "approved_action": approved_action,
            "gateway_serial": gw_serial,
            "note": exec_note,
        }

    # ── FWH_OVERRIDE: clear pending, lock NONE, resume native ──────────────────
    if action_id in ("FWH_OVERRIDE_V2", "FWH_OVERRIDE"):
        req_id = payload.get_request_id()
        pending = await db.get_pending_approval(gateway_serial=gw_serial, request_id=req_id)
        
        # Clear any pending approval
        if pending:
            await db.clear_pending_approval(request_id=pending["request_id"])
            
        user_info = f" (by {payload.ha_user_id})" if payload.ha_user_id else ""
        
        # Log to audit trail
        await db.log_automation_trigger(
            rule_id="__user_overrode__",
            rule_name="User Overrode Dispatch",
            gateway_serial=gw_serial,
            action_type="FWH_OVERRIDE",
            status="executed",
            detail=f"User selected Override{user_info}. Registering intent lock and resuming native control.",
            source="amber",
            request_id=req_id,
            ha_user_id=payload.ha_user_id
        )

        # Register a high-priority NONE intent lock (priority=0, duration=120 mins)
        from src.services.intent_lock import manager as intent_manager
        intent_manager.request_intent(
            gateway_serial=gw_serial,
            action="NONE",
            rule_id="user_ui_override",
            priority=0,
            duration_mins=120
        )

        # Dispatch RESUME_NATIVE command list
        exec_note = "Dispatched RESUME_NATIVE and registered priority 0 NONE intent lock."
        try:
            automation_engine = get_app_state().get("scheduler")
            if automation_engine:
                await automation_engine.execute_sd_signal_list(
                    gateway_serial=gw_serial,
                    signals=[{"order": 1, "signal": "RESUME_NATIVE"}],
                    dispatch_guid=f"UA-OVERRIDE-{int(time.time())}"
                )
                exec_note = "Dispatched RESUME_NATIVE command list and registered priority 0 NONE intent lock."
                logger.info(f"automation/override [OVERRIDE]: bridged RESUME_NATIVE to AutomationEngine for gw={gw_serial!r}")
            else:
                exec_note = "Registered lock, but RESUME_NATIVE dispatch failed: AutomationEngine not found."
                logger.error(f"automation/override [OVERRIDE]: could not find scheduler in app_state")
        except Exception as exc:
            exec_note = f"Registered lock, but RESUME_NATIVE dispatch failed: {exc}"
            logger.error(f"automation/override [OVERRIDE]: RESUME_NATIVE dispatch failed — {exc}")

        return {
            "ok": True,
            "action": "FWH_OVERRIDE",
            "gateway_serial": gw_serial,
            "note": exec_note,
        }

    # ── FWH_SKIP: clear pending without executing ─────────────────────────────
    if action_id in ("FWH_SKIP_V2", "FWH_SKIP"):
        req_id = payload.get_request_id()
        # Peek before clear so we can tell the caller whether their tap
        # actually matched a live pending (idempotent — SKIP still 200s
        # even when nothing to clear, but the response body is honest).
        _existed = await db.get_pending_approval(gateway_serial=gw_serial, request_id=req_id)
        await db.clear_pending_approval(gateway_serial=gw_serial, request_id=req_id)
        await db.log_automation_trigger(
            rule_id="__user_skipped__",
            rule_name="User Skipped Dispatch",
            gateway_serial=gw_serial,
            action_type="FWH_SKIP",
            status="skipped",
            detail=(
                "User dismissed via HA notification. No action taken."
                if _existed else
                "User tapped Skip but no active pending existed (already expired/unknown)."
            ),
            source="amber",
        )
        return {
            "ok": True,
            "action": "FWH_SKIP",
            "gateway_serial": gw_serial,
            "matched_pending": bool(_existed),
            "note": (
                "Pending approval cleared. No dispatch action taken."
                if _existed else
                "No matching pending — nothing to clear. Skip acknowledged."
            ),
        }

    # ── FWH_ACTIONABLE_*: Pipeline variable reply ─────────────────────────────
    if payload.action.startswith("FWH_ACTIONABLE_"):
        req_id = payload.get_request_id()
        # Extract reply (could be text input or button value)
        response_val = payload.response or payload.reply_text or payload.action
        
        # We store the response into the pending record so the awaiting thread in scheduler_core picks it up
        ok = await db.update_pending_approval_response(req_id, response_val)
        if not ok and gw_serial:
            # Fallback to lookup latest for gateway if request_id mismatch (legacy/edge cases)
            pending = await db.get_pending_approval(gateway_serial=gw_serial)
            if pending and pending.get("action") == payload.action:
                ok = await db.update_pending_approval_response(pending["request_id"], response_val)
        
        if ok:
            return {"ok": True, "note": f"Actionable response recorded: {response_val}"}
        else:
            return {"ok": False, "note": "No matching pending actionable notification found or expired."}

    # ── Legacy actions (pre-User-Approval flow) — also handle lookahead ─────
    # Batch L (2026-07-21): the lookahead notifications (Force Charge Now /
    # Skip / 30-min remind) use these button names because renaming would
    # break existing HA blueprints. Bridge them into execute_sd_signal_list
    # when a matching pending_approval exists (Batch L Part 1 ensures
    # lookahead pre-notifications now register pending rows).
    #
    # Behaviour matrix:
    #   FWH_FORCE_CHARGE_V2 with matching pending  → bridge action → 200
    #   FWH_FORCE_CHARGE_V2 without matching pending → 404 diagnostic
    #   FWH_SPIKE_HOLD_V2  with/without pending    → audit + 200 (legacy)
    #   FWH_REMIND_30_V2                           → audit + 200 (no dispatch)
    action_map = {
        "FWH_FORCE_CHARGE_V2": "Amber Force Charge",
        "FWH_SPIKE_HOLD_V2":   "Amber Spike Hold",
        "FWH_REMIND_30_V2":    None,
        "FWH_FORCE_CHARGE": "Amber Force Charge",
        "FWH_SPIKE_HOLD":   "Amber Spike Hold",
        "FWH_REMIND_30":    None,
    }
    preset = payload.preset_name or action_map.get(action_id, "")

    # FORCE_CHARGE: bridge to dispatch via matching pending
    if action_id in ("FWH_FORCE_CHARGE_V2", "FWH_FORCE_CHARGE"):
        req_id = payload.get_request_id()
        pending = await db.get_pending_approval(gateway_serial=gw_serial, request_id=req_id)
        if not pending:
            status_code, kind = await _diagnose_missing_pending(gw_serial, req_id)
            await db.log_automation_trigger(
                rule_id=f"__lookahead_{kind}__",
                rule_name=f"Lookahead Force Charge — {kind.title()}",
                gateway_serial=gw_serial,
                action_type=action_id,
                status="skipped",
                detail=f"{kind.title()} pending for req={req_id or 'none'} — no dispatch bridged.",
                source="amber",
                request_id=req_id,
            )
            logger.warning(
                f"automation/override [{action_id}]: no matching pending "
                f"(kind={kind}, req={req_id!r}, gw={gw_serial!r}) — returning {status_code}"
            )
            return JSONResponse(status_code=status_code, content={
                "ok": False,
                "action": action_id,
                "kind": kind,
                "note": (
                    "Pending approval expired (TTL exceeded)." if kind == "expired"
                    else "No matching pending — request_id unknown or never registered."
                ),
                "request_id": req_id,
                "gateway_serial": gw_serial,
            })
        # Found — bridge the pending's stored action
        approved_action = pending.get("action", "GRID_CHARGE")
        approved_rule   = pending.get("rule_name", "Lookahead Force Charge")
        await db.clear_pending_approval(request_id=pending["request_id"])
        user_info = f" (by {payload.ha_user_id})" if payload.ha_user_id else ""
        await db.log_automation_trigger(
            rule_id="__user_approved__",
            rule_name=f"User Approved (Lookahead): {approved_rule}",
            gateway_serial=gw_serial,
            action_type=action_id,
            status="executed",
            detail=f"User tapped {action_id}{user_info}. Bridging '{approved_action}' via execute_sd_signal_list.",
            source="amber",
            request_id=pending["request_id"],
            ha_user_id=payload.ha_user_id,
        )
        exec_note = f"Bridged {approved_action} to AutomationEngine."
        try:
            automation_engine = get_app_state().get("scheduler")
            if automation_engine:
                await automation_engine.execute_sd_signal_list(
                    gateway_serial=gw_serial,
                    signals=[{"order": 1, "signal": approved_action}],
                    dispatch_guid=f"UA-LOOKAHEAD-{int(time.time())}"
                )
                logger.info(
                    f"automation/override [{action_id}]: bridged {approved_action} "
                    f"to AutomationEngine for gw={gw_serial!r}"
                )
            else:
                exec_note = "AutomationEngine not found — pending cleared but no dispatch."
                logger.error(f"automation/override [{action_id}]: scheduler missing from app_state")
        except Exception as exc:
            exec_note = f"Dispatch attempt failed: {exc}"
            logger.error(f"automation/override [{action_id}]: dispatch failed — {exc}")
        return {
            "ok": True,
            "action": action_id,
            "approved_action": approved_action,
            "gateway_serial": gw_serial,
            "preset_name": preset or None,
            "request_id": pending["request_id"],
            "note": exec_note,
        }

    # SPIKE_HOLD / REMIND_30 — audit-only (legacy pattern unchanged)
    await db.log_automation_trigger(
        rule_id="__override__",
        rule_name="User Override (HA Actionable)",
        gateway_serial=gw_serial,
        action_type=action_id,
        status="override",
        detail=f"User tapped '{action_id}' on mobile. Preset: {preset or '—'}",
        action_payload=json.dumps({"preset_name": preset}),
        source="amber",
    )

    return {
        "ok": True,
        "action": action_id,
        "preset_name": preset or None,
        "note": (
            "Override logged. For User Approval strategy, use FWH_APPROVE/FWH_SKIP. "
            "For legacy active-mode push, enable Active engine mode and verify presets."
        ),
    }


# ── Home Loads picker catalog (AB exposure §4) ──────────────────────────────
# See docs/automation_builder_home_loads_exposure_plan.md for the contract.

# Per-load fields available in home_load.<gateway_key>.<slug>.*
_PER_LOAD_FIELDS = [
    {"key": "name",                "label": "Name",                       "group": "Identity",       "type": "string"},
    {"key": "slug",                "label": "Slug",                       "group": "Identity",       "type": "string"},
    {"key": "id",                  "label": "ID",                         "group": "Identity",       "type": "string"},
    {"key": "enabled",             "label": "Enabled",                    "group": "Identity",       "type": "bool"},
    {"key": "gateway_id",          "label": "Gateway",                    "group": "Identity",       "type": "string"},
    {"key": "measurement_type",    "label": "Measurement Type",           "group": "Configuration",  "type": "enum",
     "values": ["forecast", "now"]},
    {"key": "category",            "label": "Equipment Type",             "group": "Configuration",  "type": "enum",
     "values": ["ev", "hvac", "water_heater", "pool", "general"]},
    {"key": "dispatch_category",   "label": "Dispatch Priority",          "group": "Configuration",  "type": "enum",
     "values": ["1-Critical Load", "2-Essential Load", "3-Shed Load", "4-Off Grid Load"]},
    {"key": "avg_kw",              "label": "Average kW",                 "group": "Configuration",  "type": "float"},
    {"key": "peak_kw",             "label": "Peak kW",                    "group": "Configuration",  "type": "float"},
    {"key": "schedule_active_now", "label": "Schedule active now",        "group": "Schedule",       "type": "bool"},
    {"key": "is_controllable",     "label": "Controllable (has switch)",  "group": "Controllability","type": "bool"},
    {"key": "is_observable",       "label": "Observable (binary sensor)", "group": "Controllability","type": "bool"},
    {"key": "is_metered_power",    "label": "Has power sensor",           "group": "Controllability","type": "bool"},
    {"key": "is_metered_energy",   "label": "Has energy sensor",          "group": "Controllability","type": "bool"},
    {"key": "controllability",     "label": "Controllability label",      "group": "Controllability","type": "enum",
     "values": ["control+observe", "control_only", "observe_only", "none"]},
]

# Top-level (site-wide) aggregates under home_loads.*
_TOP_AGG_FIELDS = [
    {"key": "total_count",           "label": "Total loads",                                          "type": "int"},
    {"key": "enabled_count",         "label": "Enabled loads",                                        "type": "int"},
    {"key": "forecast_active_count", "label": "Active forecast loads",                                "type": "int"},
    {"key": "forecast_total_kw",     "label": "Forecast total (kW)",                                  "type": "float"},
    {"key": "controllable_count",    "label": "Controllable loads",                                   "type": "int"},
    {"key": "controllable_total_kw", "label": "Controllable total (kW)",                              "type": "float"},
    {"key": "observable_count",      "label": "Observable loads",                                     "type": "int"},
    {"key": "sheddable_count",       "label": "Sheddable loads (controllable + Shed-priority)",       "type": "int"},
    {"key": "sheddable_total_kw",    "label": "Sheddable total (kW)",                                 "type": "float"},
    {"key": "absorbable_count",      "label": "Absorbable loads (controllable + non-Critical)",       "type": "int"},
    {"key": "absorbable_total_kw",   "label": "Absorbable total (kW)",                                "type": "float"},
    {"key": "dispatch_categories",       "label": "Dispatch priorities in use (comma-separated)", "type": "string"},
    {"key": "dispatch_categories_count", "label": "Distinct dispatch priorities in use",           "type": "int"},
    {"key": "categories",                "label": "Equipment types in use (comma-separated)",      "type": "string"},
    {"key": "categories_count",          "label": "Distinct equipment types in use",               "type": "int"},
]

# Per-gateway aggregates under home_loads.by_gateway.<gateway_key>.*
_PER_GW_AGG_FIELDS = [
    {"key": "forecast_total_kw",     "label": "Forecast total (kW)",      "type": "float"},
    {"key": "forecast_active_count", "label": "Active forecast loads",    "type": "int"},
    {"key": "season",                "label": "Site season",              "type": "enum",
     "values": ["summer", "winter", "spring", "autumn", ""]},
    {"key": "controllable_total_kw", "label": "Controllable total (kW)",  "type": "float"},
    {"key": "sheddable_total_kw",    "label": "Sheddable total (kW)",     "type": "float"},
    {"key": "absorbable_total_kw",   "label": "Absorbable total (kW)",    "type": "float"},
]

_DC_KEYS_LABELS = [
    ("critical",  "Critical"),
    ("essential", "Essential"),
    ("shed",      "Shed"),
    ("off_grid",  "Off-Grid"),
]

_CAT_KEYS_LABELS = [
    ("ev",           "EV"),
    ("hvac",         "HVAC"),
    ("water_heater", "Water Heater"),
    ("pool",         "Pool Pump"),
    ("general",      "General"),
]

# Cross-cut metric shape applied inside by_dispatch_category.* and by_category.*
_CROSS_CUT_METRICS = [
    ("scheduled_kw",          "scheduled (kW)",   "float"),
    ("active_count",          "active count",     "int"),
    ("controllable_count",    "controllable count", "int"),
    ("controllable_total_kw", "controllable (kW)","float"),
]


def _field(path: str, label: str, group: str, ftype: str, values=None) -> dict:
    f = {"path": path, "label": label, "group": group, "type": ftype}
    if values is not None:
        f["values"] = values
    return f


@router.get("/automation/home-load-fields")
async def get_home_load_fields(scope: str = Query("all", description="Gateway short_id or 'all' for All-Gateways")):
    """
    AB picker catalog for the Home Loads namespace.

    Returns the gateways visible at the given scope, each with the loads in
    that bucket and the per-gateway aggregate fields. Top-level (site-wide)
    aggregates are returned separately.

    See docs/automation_builder_home_loads_exposure_plan.md §4-§5.
    """
    rule_scope = None if scope in ("all", "") else scope

    # Pull source data
    try:
        loads = await db.get_all_forecast_loads()
    except Exception as exc:
        logger.warning(f"home-load-fields: get_all_forecast_loads failed: {exc!r}")
        loads = []

    try:
        gateway_rows = await db.get_all_gateways()
    except Exception:
        gateway_rows = []
    gw_display = {g["short_id"]: g.get("name") or g["short_id"] for g in gateway_rows}

    # Resolve seasons per visible gateway (the season values themselves don't
    # affect catalog structure but the builder requires the dict)
    registry = smart_dispatch_engine._gateway_registry
    seasons: dict[str, str] = {"global": ""}
    for g in gateway_rows:
        sid = g["short_id"]
        if rule_scope is None or rule_scope == sid:
            try:
                season = await resolve_site_season_for_gateway(sid, registry)
            except Exception:
                season = ""
            seasons[sid] = season
            if not seasons["global"]:
                seasons["global"] = season

    # Use the same builder the rule evaluator uses, so the catalog matches
    # exactly what AB rules will see at evaluation time.
    from datetime import datetime
    ctx = build_home_loads_context(
        loads=loads,
        target_time=datetime.now(),
        seasons_by_gateway=seasons,
        rule_gateway_scope=rule_scope,
    )

    # ── Per-gateway catalog entries ────────────────────────────────────────
    gateways_out = []
    for gw_key, gw_loads in ctx["home_load"].items():
        # Per-load field entries
        loads_out = []
        for slug, entry in gw_loads.items():
            fields = [
                _field(
                    f"home_load.{gw_key}.{slug}.{tmpl['key']}",
                    tmpl["label"], tmpl["group"], tmpl["type"],
                    tmpl.get("values"),
                )
                for tmpl in _PER_LOAD_FIELDS
            ]
            loads_out.append({"slug": slug, "name": entry["name"], "fields": fields})

        # Per-gateway aggregate field entries
        agg_fields = [
            _field(
                f"home_loads.by_gateway.{gw_key}.{tmpl['key']}",
                tmpl["label"], "Aggregates", tmpl["type"], tmpl.get("values"),
            )
            for tmpl in _PER_GW_AGG_FIELDS
        ]
        for dc_key, dc_label in _DC_KEYS_LABELS:
            for m_key, m_lbl, m_type in _CROSS_CUT_METRICS:
                agg_fields.append(_field(
                    f"home_loads.by_gateway.{gw_key}.by_dispatch_category.{dc_key}.{m_key}",
                    f"{dc_label}: {m_lbl}", "By Dispatch Priority", m_type,
                ))
        for cat_key, cat_label in _CAT_KEYS_LABELS:
            for m_key, m_lbl, m_type in _CROSS_CUT_METRICS:
                agg_fields.append(_field(
                    f"home_loads.by_gateway.{gw_key}.by_category.{cat_key}.{m_key}",
                    f"{cat_label}: {m_lbl}", "By Equipment Type", m_type,
                ))

        if gw_key == "global":
            gw_label = "Global (All Gateways)"
        else:
            display_name = gw_display.get(gw_key, gw_key)
            gw_label = f"{display_name} ({gw_key})" if display_name != gw_key else gw_key
        gateways_out.append({
            "key":               gw_key,
            "label":             gw_label,
            "loads":             loads_out,
            "aggregate_fields":  agg_fields,
        })

    # ── Site-level (top-level) aggregates ──────────────────────────────────
    site_fields = [
        _field(
            f"home_loads.{tmpl['key']}",
            tmpl["label"], "Aggregates", tmpl["type"],
            tmpl.get("values"),
        )
        for tmpl in _TOP_AGG_FIELDS
    ]

    return {
        "ok":                     True,
        "scope":                  "all" if rule_scope is None else rule_scope,
        "field_groups": [
            "Identity", "Configuration", "Schedule", "Controllability",
            "Aggregates", "By Dispatch Priority", "By Equipment Type",
        ],
        "gateways":               gateways_out,
        "site_aggregate_fields":  site_fields,
    }
