"""First-run setup wizard — state and completion.

GH #1, queued 2026-08-07 and never built. Until now the app *talked about* a
wizard it did not have: main.py logged "SUSPENDED: Awaiting Setup Wizard
completion", gateway discovery answered "Complete the setup wizard first", and
`setup_required` was returned by the API and read by nothing. A first-time user
landed on the admin shell and had to know to visit Gateways, then HA Entities,
then MQTT Admin, in that order.

This endpoint does not re-implement any of those checks. Each step already has
a source of truth — registered gateways, /ha/status, /mqtt/status — and the
wizard reports them rather than keeping its own idea of what is configured.
That matters because a second opinion about whether MQTT works is a second
thing to be wrong.

`setup_complete` is stored so a returning user is not asked again, but it is
deliberately NOT the only gate: a install with no gateways is incomplete
whatever the flag says, which is what makes the flag safe to set early.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter

from src.services import db

logger = logging.getLogger(__name__)
# No prefix here: main.py mounts this with prefix="/api", and declaring it
# in both places yields /api/api/setup/state.
router = APIRouter(tags=["setup"])

SETUP_COMPLETE_KEY = "setup_complete"


def _install_context() -> str:
    """Where this is running, which changes what the wizard needs to ask.

    Under the Supervisor the HA connection and the MQTT broker configure
    themselves, so asking for them would be two questions with one answer.
    """
    if os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN"):
        return "ha_addon"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return "native"


async def _gateway_state() -> dict:
    try:
        gateways = await db.get_all_gateways()
    except Exception:
        logger.debug("setup: gateway list unavailable", exc_info=True)
        gateways = []
    try:
        creds = await db.get_all_credentials()
    except Exception:
        creds = []
    return {
        "done": len(gateways) > 0,
        "gateway_count": len(gateways),
        "has_credentials": bool(creds),
        "account_email": (creds[0].get("email") if creds else "") or "",
    }


async def _ha_state() -> dict:
    """Read the same status the HA tab shows, rather than forming a view."""
    from src.routes.api_ha import ha_status

    try:
        status = await ha_status()
    except Exception:
        logger.debug("setup: HA status unavailable", exc_info=True)
        return {"done": False, "detail": "Could not reach Home Assistant."}

    connected = bool(status.get("connected") or status.get("ok"))
    return {
        "done": connected,
        "source": status.get("source") or status.get("mode") or "",
        # /ha/status already counts entities matching FHAI's published slugs.
        # Surfacing it here tells a re-install that its old entities are still
        # in Home Assistant, which is the difference between "nothing happened"
        # and "these will be reclaimed".
        "fhai_entity_count": int(status.get("fhai_entity_count") or 0),
        "detail": status.get("message") or status.get("error") or "",
    }


async def _mqtt_state() -> dict:
    from src.routes.api_mqtt import mqtt_status

    try:
        status = await mqtt_status()
    except Exception:
        logger.debug("setup: MQTT status unavailable", exc_info=True)
        return {"done": False, "detail": "Could not read MQTT status."}

    connected = bool(status.get("connected"))

    # Whether a broker EXISTS and whether we have connected to it are separate
    # facts. Reporting only the second told a user running Mosquitto (with
    # Zigbee2MQTT against it) to "install the Mosquitto broker add-on".
    from src.services.addon_info import mqtt_service, supervisor_token

    broker = await mqtt_service()
    host = status.get("host") or status.get("broker") or ""

    if connected:
        action = "none"
        detail = ""
    elif broker:
        action = "configure"
        detail = (
            status.get("last_error")
            or f"Broker found at {broker.get('host')}:{broker.get('port')}, "
            "but this add-on is not publishing to it yet."
        )
        host = host or f"{broker.get('host')}:{broker.get('port')}"
    elif supervisor_token():
        action = "install"
        detail = "No add-on on this instance provides an MQTT broker."
    else:
        # Not under the Supervisor: we cannot see the instance, so we must not
        # claim a broker is missing.
        action = "configure"
        detail = "Set mqtt_host to point at your broker."

    return {
        "done": connected,
        "action": action,
        "broker_present": bool(broker),
        "host": host,
        "detail": detail,
    }


@router.get("/setup/state")
async def setup_state():
    """What still needs doing, and what already configured itself."""
    gateways = await _gateway_state()
    ha = await _ha_state()
    mqtt = await _mqtt_state()

    try:
        flag = str(await db.get_config_value(SETUP_COMPLETE_KEY, "") or "").lower()
    except Exception:
        flag = ""
    dismissed = flag in ("true", "1", "yes")

    # A gateway is the one thing nothing else can supply, so it decides whether
    # the install is usable regardless of the stored flag.
    return {
        "ok": True,
        "install_context": _install_context(),
        "complete": gateways["done"] and dismissed,
        "required_done": gateways["done"],
        "dismissed": dismissed,
        "steps": {"gateway": gateways, "ha": ha, "mqtt": mqtt},
    }


@router.post("/setup/complete")
async def setup_complete(payload: dict | None = None):
    """Record that the wizard has been seen, so it does not reappear."""
    await db.set_config_value(SETUP_COMPLETE_KEY, "true")
    await db.log_admin_audit(
        event="Setup:Wizard_Completed", source="ui",
        details="First-run setup wizard completed",
    )
    return {"ok": True}


@router.post("/setup/skip")
async def setup_skip():
    """Dismiss without finishing.

    Offered because a user who already configured everything by hand should not
    be held in a wizard telling them so. Audited separately from completion, so
    a support conversation can tell the two apart.
    """
    await db.set_config_value(SETUP_COMPLETE_KEY, "true")
    await db.log_admin_audit(
        event="Setup:Wizard_Skipped", source="ui",
        details="First-run setup wizard dismissed without completing every step",
    )
    return {"ok": True}
