"""
notification_sender.py — Smart Dispatch HA notification integration.

Tier 2: Standard HA push notifications via POST /api/services/notify/{target}
Tier 3: Actionable notifications with action buttons; user taps fire a callback
        to /api/automation/notifications/override via a HA automation recipe.

All functions are fire-and-forget safe — errors are logged but never raised.
Reuses ha_host + ha_token stored in app_config by the HA integration setup.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

from src.services import db

logger = logging.getLogger(__name__)

# ── Event type definitions ─────────────────────────────────────────────────────

DEFAULT_EVENT_TITLES = {
    "spike":                   "Price Spike Detected",
    "force_charge":            "Cheap Price - Force Charge?",
    "peak_start":              "Peak Tariff Period Starting",
    "signal_change":           "Dispatch Signal Changed",
    "test":                    "Event Test",
    "daily_summary":           "Daily Dispatch Summary",
    "user_approval_request":   "Smart Dispatch - Approval Required",
    "rule_triggered":          "Automation Rule Triggered",
    "actionable_step":         "Automation - Action Required",
    "cert_expiry":             "TLS Certificate Expiring",
}

DEFAULT_EVENT_MESSAGES = {
    # Composed by tls_monitor._compose — it varies by renewal mechanism, which
    # _build_message's fixed context has no way to express.
    "cert_expiry": "{message}",
    "spike": (
        "A price spike is active ({tariff_type}). "
        "Import disabled — battery holding charge."
    ),
    "force_charge": (
        "Price drops to {import_c_kwh}c/kWh. "
        "Force Charge window recommended."
    ),
    "peak_start": (
        "Peak tariff period starting. "
        "Battery set to discharge ({import_c_kwh}c import)."
    ),
    "signal_change": (
        "Dispatch signal changed to: {action}. "
        "Preset: {preset_name}."
    ),
    "test": (
        "Integrator test: {action}"
    ),
    "daily_summary": (
        "Today's Smart Dispatch summary: {summary}."
    ),
    "user_approval_request": (
        # {tap_hint} is substituted per-action in _build_message so the
        # message never contradicts the buttons that actually ship
        # (backlog defect 2026-07-29: HOLD message said "Skip to
        # cancel" but the FWH_APPROVE_V2/FWH_OVERRIDE_V2 button pair
        # for HOLD has no Skip button).
        "Action recommended: {action}. Rule: {rule_name}. {dispatch_summary} "
        "{tap_hint}"
    ),
    "rule_triggered": (
        "{message}"
    ),
    "actionable_step": (
        "{message}"
    ),
}

# Which event types support actionable responses
ACTIONABLE_EVENTS = {"force_charge", "spike", "user_approval_request", "test", "actionable_step"}

# Actions available per event type
ACTIONABLE_ACTIONS = {
    "force_charge": [
        {"action": "FWH_FORCE_CHARGE_V2", "title": "Force Charge Now"},
        {"action": "FWH_SKIP_V2",         "title": "Skip"},
        {"action": "FWH_REMIND_30_V2",    "title": "30 min"},
    ],
    "spike": [
        {"action": "FWH_SPIKE_HOLD_V2",   "title": "Hold (Spike Active)"},
        {"action": "FWH_SKIP_V2",         "title": "Dismiss"},
    ],
    "user_approval_request": [
        {"action": "FWH_APPROVE_V2",      "title": "Approve"},
        {"action": "FWH_SKIP_V2",         "title": "Skip"},
    ],
    "test": [
        {"action": "FWH_TEST_APPROVE_V2", "title": "Approve (Test)"},
        {"action": "FWH_TEST_DENY_V2",    "title": "Deny (Test)"},
    ],
}


# ── Core sender ───────────────────────────────────────────────────────────────

async def _get_ha_credentials() -> tuple[str, str]:
    """Return (ha_host, ha_token), resolved the same way as every other caller.

    This used to resolve its own way and got it half right: it picked up
    SUPERVISOR_TOKEN but never the Supervisor HOST, so as an add-on it held a
    valid token and an empty host. Notify-target discovery then failed with
    "HA returned 401" — a token presented to the wrong place — and the add-on's
    own connection, which works, went unused.

    `api_ha._get_ha_client()` already resolves both, in priority order, and is
    what the rest of the application uses. One resolver.
    """
    try:
        from src.routes.api_ha import _get_ha_client

        base, auth, _env = await _get_ha_client()
        if base and auth:
            # _get_ha_client returns an Authorization header value; callers here
            # want the bare token.
            return base, auth.removeprefix("Bearer ").strip()
    except Exception:
        logger.debug("notification_sender: falling back to direct credential lookup", exc_info=True)

    ha_host  = await db.get_config_value("ha_host",  "") or os.environ.get("HA_HOST",  "")
    ha_token = await db.get_config_value("ha_token", "") or os.environ.get("HA_TOKEN", "")
    if not ha_token:
        ha_token = os.environ.get("SUPERVISOR_TOKEN", "")
    return (ha_host or ""), (ha_token or "")


def _tap_hint_for(event_type: str, action: str) -> str:
    """Build a message tail that describes the ACTUAL buttons shipped
    for this event/action so the push text never contradicts the
    button set. Kept in sync with ACTIONABLE_ACTIONS + the HOLD
    branch in build_notification_payload."""
    if event_type != "user_approval_request":
        return ""
    if str(action).upper() == "HOLD":
        return "Tap Hold State to accept, or Override to release engine control."
    return "Tap Approve to execute now, or Skip to cancel."


def _build_message(event_type: str, context: dict[str, Any], custom_templates: dict = None) -> str:
    custom_templates = custom_templates or {}
    template = custom_templates.get(f"{event_type}_message") or DEFAULT_EVENT_MESSAGES.get(event_type, "FranklinWH Smart Dispatch event: {event_type}")
    ctx = {
        "event_type":  event_type,
        "action":      context.get("action", "-"),
        "preset_name": context.get("preset_name") or "-",
        "import_c_kwh": f"{context.get('import_c_kwh', 0):.2f}" if context.get("import_c_kwh") is not None else "-",
        "export_c_kwh": f"{context.get('export_c_kwh', 0):.2f}" if context.get("export_c_kwh") is not None else "-",
        "tariff_type":     context.get("tariff_type", "-"),
        "summary":         context.get("summary", "-"),
        "rule_name":       context.get("rule_name", "-"),
        "dispatch_summary": context.get("dispatch_summary", ""),
        "message":         context.get("message", ""),
        "tap_hint":        _tap_hint_for(event_type, context.get("action", "")),
    }
    try:
        return template.format(**ctx)
    except Exception:
        return template


async def build_notification_payload(
    event_type: str,
    context: dict[str, Any],
    actionable: bool = False,
    request_id: str = None,
) -> dict:
    """
    Build the HA notify service payload for a given event type.
    When actionable=True and the event supports it, adds data.actions.
    """
    custom_templates = await db.get_config_value("notification_templates", {}) or {}
    
    # Allow context to override title/message for generic rule events
    if event_type in ["rule_triggered", "actionable_step"] and context.get("title"):
        title = context["title"]
    else:
        title = custom_templates.get(f"{event_type}_title") or DEFAULT_EVENT_TITLES.get(event_type, "FranklinWH Notification")
        
    message = _build_message(event_type, context, custom_templates)

    # Do NOT include 'push', 'tag', or 'apns_collapse_id'. Any nested
    # objects inside data (other than actions) breaks the dynamic actionable 
    # category generation on iOS, causing watchOS to collapse the buttons.
    payload: dict[str, Any] = {
        "title":   title,
        "message": message,
        "data": {},
    }

    if actionable and event_type in ACTIONABLE_EVENTS:
        if event_type == "actionable_step":
            # Dynamic actions based on context
            response_type = context.get("response_type", "yes_no")
            if response_type == "yes_no":
                actions = [
                    {"action": context.get("action"), "title": "Yes"},
                    {"action": "FWH_SKIP", "title": "No"}
                ]
            else:
                # Text/number input
                actions = [
                    {"action": context.get("action"), "title": "Reply", "behavior": "textInput"}
                ]
        else:
            if event_type == "user_approval_request" and context.get("action") == "HOLD":
                actions = [
                    {"action": "FWH_APPROVE_V2", "title": "Hold State"},
                    {"action": "FWH_OVERRIDE_V2", "title": "Override"},
                ]
            else:
                actions = ACTIONABLE_ACTIONS.get(event_type, [])
            
        if actions:
            payload["data"]["actions"] = actions
            if request_id:
                # We include these fields in both the top-level 'data' block AND
                # a nested 'action_data' block. 
                # - Top-level is used by Android and some HA internal logic.
                # - 'action_data' is REQUIRED by iOS/watchOS to return data in the callback event.
                payload["data"]["request_id"] = request_id
                payload["data"]["gateway_id"] = context.get("gateway_serial") or context.get("gateway_id") or ""
                payload["data"]["rule_id"] = context.get("rule_id", "")
                
                payload["data"]["action_data"] = {
                    "request_id": request_id,
                    "gateway_id": payload["data"]["gateway_id"],
                    "rule_id":    payload["data"]["rule_id"]
                }

    return payload


async def send_ha_notification(
    event_type: str,
    context: dict[str, Any],
    settings: Optional[dict] = None,
    force_actionable: bool = False,
    request_id: str = None,
) -> dict:
    """
    Send a HA mobile push notification to all configured target devices concurrently.
    Falls back to legacy config if no notification_devices are registered.

    Args:
        event_type:       One of EVENT_TITLES keys.
        context:          Dict with signal context (action, preset_name, import_c_kwh, etc.)
        settings:         Pre-fetched notification settings (will fetch from DB if None).
        force_actionable: If True, override settings.actionable and always add action buttons.
        request_id:       Optional correlation ID (will generate UUID if not provided).

    Returns:
        {"sent": bool, "status_code": int|None, "error": str|None, "request_id": str|None}
    """
    import asyncio
    import json

    if settings is None:
        settings = await db.get_notification_settings()

    is_diag = (event_type == "test" or (request_id and "test" in request_id.lower()))
    log_prefix = "[DIAGNOSTIC] " if is_diag else "[PRODUCTION] "

    if not settings.get("enabled"):
        msg = "Skipped: notifications disabled globally"
        logger.info(f"notification_sender: {msg}")
        await db.add_notification_log("SKIPPED", event_type, f"{log_prefix}{msg}")
        return {"sent": False, "status_code": None, "error": "notifications_disabled"}

    # Retrieve all devices and map them to their parent instances
    devices = await db.get_notification_devices()
    active_devices = [d for d in devices if d.get("enabled")]

    target_user = context.get("username")
    if target_user:
        targeted_devices = [d for d in active_devices if d.get("owner_username") == target_user]
        if targeted_devices:
            active_devices = targeted_devices
            logger.info(f"notification_sender: routing targeted notification to user '{target_user}' devices: {[d['alias'] for d in active_devices]}")
        else:
            logger.info(f"notification_sender: targeted user '{target_user}' has no registered devices, falling back to global broadcast")

    dispatches = []
    skipped: list[dict] = []

    if active_devices:
        # Load all unmasked instances to resolve hosts and tokens
        instances_list = await db.get_ha_instances()
        instances_map = {inst["id"]: inst for inst in instances_list if inst.get("enabled")}

        from src.services.ha_autoconfig import resolve_instance_token

        for dev in active_devices:
            inst = instances_map.get(dev["ha_instance_id"])
            if not inst:
                # Skipping here used to be silent, so five enabled devices could
                # vanish from a dispatch and the UI would report "no targets
                # registered" with no way to find out why. Say what was dropped.
                skipped.append({
                    "ok": False,
                    "device_id": dev.get("id"),
                    "alias": dev.get("alias") or "(unnamed device)",
                    "status_code": None,
                    "error": f"parent HA instance '{dev.get('ha_instance_id')}' is missing or disabled",
                })
                continue

            host = inst.get("host", "").strip()
            token = resolve_instance_token(inst)
            target = dev.get("service_target", "").strip()

            if not host or not token or not target:
                missing = [n for n, v in (("host", host), ("token", token), ("notify target", target)) if not v]
                skipped.append({
                    "ok": False,
                    "device_id": dev.get("id"),
                    "alias": dev.get("alias") or "(unnamed device)",
                    "status_code": None,
                    "error": f"incomplete configuration: no {', '.join(missing)}",
                })
                continue

            dispatches.append({
                "device_id": dev["id"],
                "alias": dev["alias"],
                "instance_alias": inst["alias"],
                "host": host,
                "token": token,
                "target": target
            })

    # Legacy fallback: use single settings targets if no active devices are configured
    if not dispatches:
        legacy_host, legacy_token = await _get_ha_credentials()
        legacy_target = settings.get("ha_target", "").strip()
        if legacy_host and legacy_token and legacy_target:
            dispatches.append({
                "device_id": "legacy_default",
                "alias": "Legacy Default Device",
                "instance_alias": "Legacy Primary HA",
                "host": legacy_host,
                "token": legacy_token,
                "target": legacy_target
            })

    if not dispatches:
        if skipped:
            detail = "; ".join(f"{d['alias']}: {d['error']}" for d in skipped)
            msg = f"Skipped: {len(skipped)} configured device(s) were unusable — {detail}"
        else:
            msg = "Skipped: no notification targets configured"
        # A send that reaches nobody is a failure, not routine information —
        # logging it at INFO is why it never reached the add-on log.
        logger.warning(f"notification_sender: {msg}")
        await db.add_notification_log("SKIPPED", event_type, f"{log_prefix}{msg}")
        return {"sent": False, "status_code": None, "error": "no_targets", "results": skipped}

    if skipped:
        logger.warning(
            "notification_sender: %d configured device(s) skipped — %s",
            len(skipped),
            "; ".join(f"{d['alias']}: {d['error']}" for d in skipped),
        )

    # Generate request_id if needed for actionable notifications
    if not request_id and (force_actionable or bool(settings.get("actionable"))):
        import uuid
        request_id = str(uuid.uuid4())
        # Re-check diagnostic prefix with newly generated request_id
        is_diag = (event_type == "test" or (request_id and "test" in request_id.lower()))
        log_prefix = "[DIAGNOSTIC] " if is_diag else "[PRODUCTION] "

    payload = await build_notification_payload(
        event_type=event_type,
        context=context,
        actionable=force_actionable or bool(settings.get("actionable")),
        request_id=request_id
    )

    async def send_to_target(disp: dict) -> dict:
        url = f"{disp['host'].rstrip('/')}/api/services/notify/{disp['target']}"
        headers = {
            "Authorization": f"Bearer {disp['token']}",
            "Content-Type":  "application/json",
        }

        # Batch N (2026-07-25): demoted from INFO to DEBUG. Rendering the
        # full HTTP request + response as separate ==== banner lines in
        # the app log was legitimate during initial HA integration bring-
        # up but is pure noise in steady-state. Toggle LOG_LEVEL=DEBUG to
        # get the raw dumps back for troubleshooting.
        debug_headers = headers.copy()
        debug_headers["Authorization"] = "Bearer [REDACTED]"
        logger.debug(f"=== RAW HTTP TRANSACTION DUMP TO {disp['alias']} ===")
        logger.debug(f"POST {url}")
        logger.debug(f"Headers: {json.dumps(debug_headers)}")
        logger.debug(f"Body: {json.dumps(payload)}")
        logger.debug("=================================")

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)

            logger.debug(f"=== HA HTTP RESPONSE FROM {disp['alias']} ===")
            logger.debug(f"Status Code: {resp.status_code}")
            logger.debug(f"Response Body: {resp.text}")
            logger.debug("========================")

            if resp.status_code in (200, 201):
                return {
                    "ok": True,
                    "device_id": disp["device_id"],
                    "alias": disp["alias"],
                    "status_code": resp.status_code,
                    "error": None
                }
            else:
                return {
                    "ok": False,
                    "device_id": disp["device_id"],
                    "alias": disp["alias"],
                    "status_code": resp.status_code,
                    "error": resp.text[:200]
                }
        except Exception as exc:
            logger.error(f"notification_sender: failed to send to {disp['alias']} — {exc}")
            return {
                "ok": False,
                "device_id": disp["device_id"],
                "alias": disp["alias"],
                "status_code": None,
                "error": str(exc)
            }

    # Execute all dispatches concurrently
    results = await asyncio.gather(*(send_to_target(disp) for disp in dispatches), return_exceptions=True)

    successful_dispatches = []
    failed_dispatches = []

    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Async dispatch exception: {r}")
            failed_dispatches.append({"alias": "System Failure", "error": str(r)})
        elif r.get("ok"):
            successful_dispatches.append(r)
        else:
            failed_dispatches.append(r)

    has_actions = bool(payload.get("data", {}).get("actions"))
    notif_type_label = "Actionable" if has_actions else "Standard"

    # Batch N: brief single-line summary for the audit ledger `details`
    # column + app log. Full payload + per-device results go into the
    # new `details_json` column for opt-in expand-on-demand UI.
    ok_count = len(successful_dispatches)
    fail_count = len(failed_dispatches)
    total = ok_count + fail_count
    req_short = (request_id[:8] + "…") if (request_id and len(request_id) > 8) else (request_id or "none")
    if ok_count == total:
        outcome = f"{ok_count}/{total} OK"
    elif ok_count == 0:
        outcome = f"0/{total} — all failed"
    else:
        outcome = f"{ok_count}/{total} OK ({fail_count} failed)"
    brief = f"{notif_type_label} '{event_type}' → {outcome} (req={req_short})"

    # Full detail — persisted as JSON string in details_json for opt-in
    # expand-on-demand render. Kept small enough to not blow the row
    # (payload is already the biggest field).
    full_detail_obj = {
        "type":           notif_type_label,
        "event_type":     event_type,
        "request_id":     request_id,
        "successful":     [{"alias": r["alias"], "status_code": r.get("status_code")} for r in successful_dispatches],
        "failed":         [{"alias": r["alias"], "error": r.get("error")} for r in failed_dispatches],
        "payload":        payload,
        "prefix":         log_prefix.strip(),
    }
    details_json = json.dumps(full_detail_obj, separators=(",", ":"))

    if successful_dispatches:
        logger.info(f"notification_sender: {brief}")
        await db.add_notification_log(
            "SENT", event_type,
            f"{log_prefix}{brief}",
            details_json=details_json,
        )
        return {
            "sent": True,
            "status_code": successful_dispatches[0]["status_code"],
            "error": None,
            "request_id": request_id,
            "payload": payload,
            "results": list(results) + skipped
        }
    else:
        logger.warning(f"notification_sender: {brief}")
        await db.add_notification_log(
            "ERROR", event_type,
            f"{log_prefix}{brief}",
            details_json=details_json,
        )
        return {
            "sent": False,
            "status_code": None,
            "error": "all_routes_failed",
            "payload": payload,
            "results": list(results) + skipped
        }


# ── HA notify target discovery ────────────────────────────────────────────────

async def discover_notify_targets() -> dict:
    """
    Query the HA REST API for all available notify services.

    Returns:
        {
          "ok": bool,
          "targets": [{"service": "mobile_app_davids_iphone_15_pro", "label": "David's iPhone 15 Pro"}, ...],
          "error": str | None
        }

    The HA /api/services endpoint returns a list of domain objects.
    We filter to the 'notify' domain and return all service names.
    mobile_app_* entries are rich objects with a 'name' field in their
    fields schema — we try to extract a friendly name where possible.
    """
    ha_host, ha_token = await _get_ha_credentials()
    if not ha_host or not ha_token:
        return {"ok": False, "targets": [], "error": "HA not configured (no host or token)"}

    url = f"{ha_host.rstrip('/')}/api/services"
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type":  "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "targets": [], "error": f"HA returned {resp.status_code}: {resp.text[:200]}"}

        services_list = resp.json()  # list of {"domain": str, "services": {name: {...}}}
        targets = []
        for domain_obj in services_list:
            if domain_obj.get("domain") != "notify":
                continue
            for svc_name, svc_meta in domain_obj.get("services", {}).items():
                # Derive a friendly label
                label = svc_meta.get("name") or svc_name.replace("_", " ").title()
                targets.append({"service": svc_name, "label": label})

        # Sort: mobile_app_* first (most useful), then everything else
        targets.sort(key=lambda t: (0 if t["service"].startswith("mobile_app") else 1, t["service"]))
        logger.info(f"discover_notify_targets: found {len(targets)} notify service(s)")
        return {"ok": True, "targets": targets, "error": None}

    except Exception as exc:
        logger.error(f"discover_notify_targets: failed — {exc}")
        return {"ok": False, "targets": [], "error": str(exc)}


# ── HA YAML recipe generator ──────────────────────────────────────────────────

def generate_ha_automation_yaml(fhai_host: str) -> str:
    """
    Generate the HA automation YAML that the user pastes into configuration.yaml.
    Called dynamically with the resolved FHAI host so the webhook URL is pre-filled.
    """
    fhai_base = fhai_host.rstrip("/") if fhai_host else "http://<FHAI_HOST>:8099"
    is_https = fhai_base.startswith("https://")
    ssl_config = "\n    verify_ssl: false  # Required if using self-signed TLS/HTTPS certs" if is_https else ""
    return f"""# ── FranklinWH Smart Dispatch — Actionable Notification Handler ──────────────
# Paste into configuration.yaml (or a dedicated automations.yaml)

rest_command:
  franklinwh_action_callback:
    # ⚠️ IMPORTANT: The URL below MUST be reachable from Home Assistant.
    # Use the network IP of this machine, not 'localhost' or 'fwhhai-app'.
    url: "{fhai_base}/api/automation/notifications/override"
    method: POST
    content_type: "application/json"{ssl_config}
    payload: >
      {{
        "action_id": "{{{{ action_id }}}}",
        "gateway_id": "{{{{ gateway_id }}}}",
        "request_id": "{{{{ request_id }}}}",
        "ha_user_id": "{{{{ ha_user_id }}}}",
        "response": "{{{{ response }}}}"
      }}

automation:
  - id: "franklinwh_smart_dispatch_action_callback"
    alias: "FranklinWH Smart Dispatch Action Callback"
    description: "Handles actionable push notifications from the FranklinWH Engine"
    mode: parallel
    max: 10
    trigger:
      - platform: event
        event_type: mobile_app_notification_action
    condition:
      - condition: template
        value_template: >-
          {{{{ trigger.event.data.action.startswith('FWH_') }}}}
    action:
      # Optional: Uncomment to debug event data in HA logs (Settings -> System -> Logs)
      # - action: system_log.write
      #   data:
      #     message: "FHAI Event data: {{{{ trigger.event.data }}}}"
      #     level: info
      - action: rest_command.franklinwh_action_callback
        data:
          action_id: "{{{{ trigger.event.data.action }}}}"
          gateway_id: "{{{{ trigger.event.data.gateway_id | default(trigger.event.data.get('action_data', {{}}).get('gateway_id', '')) }}}}"
          request_id: "{{{{ trigger.event.data.request_id | default(trigger.event.data.get('action_data', {{}}).get('request_id', '')) }}}}"
          ha_user_id: "{{{{ trigger.event.context.user_id | default('unknown') }}}}"
          response: "{{{{ trigger.event.data.response | default(trigger.event.data.reply_text | default('')) }}}}"

# ── Home Assistant REST Sensors (Optional Telemetry Signals) ─────────────────
# If you are using REST sensors to read /api/automation/signal and have enabled
# "Lock Dashboard & REST APIs" (Security Enabled) in FHAI, configure them like this:
#
# sensor:
#   - platform: rest
#     name: "FranklinWH Dispatch Signal"
#     unique_id: franklinwh_dispatch_signal
#     resource: "{fhai_base}/api/automation/signal"
#     # If Security is active, uncomment and configure:
#     # headers:
#     #   Authorization: "Bearer fhai_YOUR_LONG_LIVED_API_TOKEN_HERE"
#     # If HTTPS is active with self-signed certificate:
#     # verify_ssl: false
#     scan_interval: 300
#     value_template: "{{{{ value_json.action }}}}"
#     json_attributes:
#       - preset_name
#       - rule_name
#       - reason
#       - context
"""

