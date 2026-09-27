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

import asyncio
import logging
import os

from fastapi import APIRouter
from pydantic import BaseModel

from src.services import db

logger = logging.getLogger(__name__)
# No prefix here: main.py mounts this with prefix="/api", and declaring it
# in both places yields /api/api/setup/state.
router = APIRouter(tags=["setup"])

SETUP_COMPLETE_KEY = "setup_complete"


def _app_version() -> str:
    """The running FHAI version, for the wizard header."""
    try:
        from src import __version__
        return str(__version__)
    except Exception:
        return ""


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


PREFIX_CHOICES = [
    {
        "key": "serial",
        "template": "franklinwh_{short_id}_",
        "label": "Serial",
        "detail": "Recommended. Unambiguous, and still correct if a second gateway is added later.",
    },
    {
        "key": "model_serial",
        "template": "franklinwh_{model}_{serial4}_",
        "label": "Model and serial",
        "detail": "Matches the scheme earlier FranklinWH integrations produced. "
                  "Choose this to keep an existing dashboard working.",
    },
]



#: Discovery is ~20 cloud calls; the wizard re-fetches state on every step.
REFRESH_TIMEOUT_S = 45
REFRESH_COOLDOWN_S = 300

_REFRESH_AT_KEY = "gw_{short_id}_setup_refresh_at"
_REFRESH_ERR_KEY = "gw_{short_id}_setup_refresh_error"


async def _refresh_due(short_id: str) -> bool:
    """True if this gateway has not been tried inside the cooldown."""
    import time

    from src.services import db

    try:
        last = float(await db.get_config_value(
            _REFRESH_AT_KEY.format(short_id=short_id), 0) or 0)
    except (TypeError, ValueError):
        return True
    return (time.time() - last) > REFRESH_COOLDOWN_S


async def _mark_refresh_attempt(short_id: str) -> None:
    import time

    from src.services import db

    try:
        await db.set_config_value(
            _REFRESH_AT_KEY.format(short_id=short_id), str(time.time()))
    except Exception:
        logger.debug("setup: could not record the refresh attempt", exc_info=True)


async def _record_refresh_error(short_id: str, message: str) -> None:
    from src.services import db

    try:
        await db.set_config_value(_REFRESH_ERR_KEY.format(short_id=short_id), message)
    except Exception:
        logger.debug("setup: could not record the refresh error", exc_info=True)


async def _clear_refresh_error(short_id: str) -> None:
    await _record_refresh_error(short_id, "")


async def _last_refresh_error(short_id: str) -> str:
    """Why the last attempt failed, so the cooldown does not hide the reason."""
    from src.services import db

    try:
        return str(await db.get_config_value(
            _REFRESH_ERR_KEY.format(short_id=short_id), "") or "")
    except Exception:
        return ""


async def _refresh_stale_profiles(gateways: list[dict]) -> dict[str, str]:
    """Re-run discovery for any gateway whose profile predates the flags.

    Best effort and non-fatal: a gateway that cannot be reached still renders
    from what is stored. Refreshing rewrites the profile in place, so the rows
    read below pick it up on this same request.

    Returns the failures by short_id. The first version of this swallowed them
    into a debug log and showed "Could not reach the FranklinWH cloud" — which
    names no cause, so there is nothing to act on. Whatever actually went
    wrong is what the user needs to read.
    """
    import json as _json

    from src.services.integration_manager import refresh_gateway_profile

    errors: dict[str, str] = {}

    for row in gateways:
        profile = row.get("profile_json")
        if isinstance(profile, str):
            try:
                profile = _json.loads(profile or "{}")
            except ValueError:
                profile = {}
        profile = profile or {}

        # `pcs_enabled` stands in for the whole group — they are written
        # together by one snapshot, so either all are present or none are.
        if "pcs_enabled" in profile:
            continue

        short_id = str(row.get("short_id") or "")
        if not short_id:
            continue

        # Not more than once every few minutes per gateway. `discover(tier=3)`
        # is roughly twenty cloud calls, and this runs inside a GET the wizard
        # re-fetches on every step — without the cooldown a gateway that fails
        # to refresh is retried on every poll, indefinitely.
        if not await _refresh_due(short_id):
            errors[short_id] = await _last_refresh_error(short_id)
            continue

        await _mark_refresh_attempt(short_id)

        try:
            # Bounded: the ingress proxy gives up long before twenty cloud
            # calls can hang, and a step that never returns is worse than one
            # that says it could not finish.
            await asyncio.wait_for(refresh_gateway_profile(short_id),
                                   timeout=REFRESH_TIMEOUT_S)
            await _clear_refresh_error(short_id)
            logger.info("setup: refreshed the stale profile for %s", short_id)
        except asyncio.TimeoutError:
            msg = (f"Discovery did not finish within {REFRESH_TIMEOUT_S}s — "
                   f"the gateway or the cloud is slow to respond")
            logger.warning("setup: refresh of %s timed out", short_id)
            await _record_refresh_error(short_id, msg)
            errors[short_id] = msg
            continue
        except Exception as exc:
            # No credentials, gateway offline, cloud refusing — all survivable,
            # and all different. Logged at warning, not debug: a setup step
            # that silently gave up is why this took four releases to see.
            logger.warning("setup: could not refresh %s — %s: %s",
                           short_id, type(exc).__name__, exc, exc_info=True)
            msg = f"{type(exc).__name__}: {exc}"
            await _record_refresh_error(short_id, msg)
            errors[short_id] = msg
            continue

        fresh = await _reread_profile(short_id)
        if fresh is not None:
            row["profile_json"] = fresh

    return errors


async def _reread_profile(short_id: str):
    """The profile as it stands after a refresh, or None if it cannot be read."""
    from src.services import db

    try:
        gw = await db.get_gateway_full(short_id)
    except Exception:
        return None
    return (gw or {}).get("profile_json")


def _features(profile: dict, gw: dict, registry) -> dict:
    """Feature facets from the stored profile, plus any live telemetry.

    The profile is the whole snapshot discovery wrote; `gw` contributes the
    handful of flags the hydrator re-derives. Profile first, so a hydrator
    default of False cannot overwrite a flag the cloud actually set.
    """
    from src.services.accessories import features

    last_data = {}
    if registry is not None:
        try:
            last_data = (registry.get_status(gw.get("short_id")) or {}).get("last_data") or {}
        except Exception:
            logger.debug("setup: no live status for features", exc_info=True)

    merged = {**{k: v for k, v in gw.items() if v is not None}, **profile}
    facets = features(merged, last_data)
    return {"hardware_features": facets["hardware"],
            "setup_features": facets["setup"],
            "features_stale": facets["stale"]}


async def _hardware_state() -> dict:
    """What is installed, hydrated exactly as the Gateways tab hydrates it.

    This step used to build its own summary from `profile.has_*`, and showed
    "Solar ? Smart circuits ? Generator ? aPBox ?" on a site that was
    generating at the time — while the Gateways tab, one click away, listed the
    accessories and the relay states correctly.

    So it calls the Gateways tab's own hydrator rather than keeping a second,
    worse answer to the same question. Everything the dashboard row renders —
    `accessories`, `grid_relay1`, `generator_relay`, `solar_relay1`, `solar_kw`
    — is on the object here too, under the same names, so the wizard can render
    it the same way.
    """
    from src.routes.api_gateways import _hydrate_gateway_fields
    from src.app_state import get_app_state
    from src.services import db

    try:
        gateways = await db.get_all_gateways() or []
    except Exception:
        logger.debug("setup: no gateways for the hardware summary", exc_info=True)
        return {"done": False, "gateways": []}

    registry = get_app_state().get("registry")

    import json as _json

    # A profile written before FHAI stored the discover flags reports every one
    # of them as absent, which reads as "nothing is installed" on a site that
    # has solar, two smart circuits and a configured tariff. The fix for that
    # is one `discover()` call, which this integration already knows how to
    # make — so make it, rather than printing a notice asking the user to go to
    # another tab and do it by hand. This is the setup wizard; fetching what
    # setup needs is its job.
    refresh_errors = await _refresh_stale_profiles(gateways)

    summary = []
    for row in gateways:
        # Parsed before hydrating: `_hydrate_gateway_fields` copies a dozen
        # named keys onto the row and then pops `profile_json`, so the flags it
        # does not know about — `pcs_enabled`, `tariff_configured`, `sc_names`
        # — are gone by the time it returns.
        profile = row.get("profile_json")
        if isinstance(profile, str):
            try:
                profile = _json.loads(profile or "{}")
            except ValueError:
                profile = {}
        profile = profile or {}

        try:
            gw = await _hydrate_gateway_fields(dict(row), registry)
        except Exception:
            logger.debug("setup: could not hydrate %s", row.get("short_id"), exc_info=True)
            gw = dict(row)

        short_id = str(gw.get("short_id") or "")

        # The batteries table is the inventory; the hydrator does not read it.
        try:
            batteries = await db.get_batteries_for_gateway(short_id) or []
        except Exception:
            batteries = []

        # Capacity from the units themselves, summed — not a per-unit constant
        # times a count, which over-states a mixed fleet.
        total_kwh = round(sum(float(b.get("rated_kwh") or 0) for b in batteries), 1)
        total_kw = round(sum(float(b.get("rated_kw") or 0) for b in batteries), 1)

        summary.append({
            "short_id": short_id,
            "name": gw.get("name") or "",
            "site": gw.get("site_name") or "",
            "model": (gw.get("model") or "").strip(),
            "sku": gw.get("sku", ""),
            "firmware": gw.get("firmware", ""),
            "apower_count": len(batteries) or gw.get("battery_count") or 0,
            "apower_kwh": total_kwh,
            "apower_kw": total_kw,
            # The Gateways tab's own answer, not a second opinion.
            "accessories": gw.get("accessories") or [],
            "grid_relay1": gw.get("grid_relay1"),
            "generator_relay": gw.get("generator_relay"),
            "solar_relay1": gw.get("solar_relay1"),
            "solar_kw": gw.get("solar_kw"),
            "poll_status": gw.get("poll_status"),
            # The two facets `franklinwh-cli discover` prints: what is fitted,
            # and how it is set up. `gw` carries the profile flags, hydrated
            # at the top of _hydrate_gateway_fields.
            **_features(profile, gw, registry),
            # Why the refresh failed, verbatim. "Could not reach the cloud"
            # names no cause and leaves nothing to act on.
            "refresh_error": refresh_errors.get(short_id, ""),
        })

    return {"done": bool(summary), "gateways": summary}


async def _prefix_state() -> dict:
    """The entity prefix, the choices, and what each would actually produce.

    Asked during setup because it cannot be changed afterwards without
    renaming every entity: Home Assistant assigns an entity id once, at
    creation, and keeps it. A dashboard written against the wrong scheme has to
    be rewritten or the entities renamed — both avoidable by asking here, once,
    before the first discovery publish.
    """
    from src.routes.api_mqtt import DEFAULT_ENTITY_PREFIX
    from src.services import db
    from src.services.mqtt_publisher import MQTTPublisher

    stored = await db.get_config_value("mqtt_entity_prefix", DEFAULT_ENTITY_PREFIX)

    serial, model = "", ""
    try:
        gateways = await db.get_all_gateways() or []
        if gateways:
            serial = str(gateways[0].get("full_serial") or "")
            model = str(gateways[0].get("model") or "")
    except Exception:
        logger.debug("setup: no gateway yet for the prefix preview", exc_info=True)

    # Before a gateway is registered there is nothing real to render, so show
    # the documented fixture rather than an invented serial.
    if not serial:
        from src.models.gateway import FIXTURE_SERIAL
        serial, model = FIXTURE_SERIAL, "aGate X-01-AU"

    pub = MQTTPublisher.__new__(MQTTPublisher)
    pub.slug_aliases = {}

    choices = []
    for choice in PREFIX_CHOICES:
        pub.entity_prefix_template = choice["template"]
        rendered = pub._make_uid(serial[-8:], "grid_power", full_serial=serial, model=model)
        choices.append({**choice, "example": f"sensor.{rendered}"})

    return {
        "template": stored,
        "choices": choices,
        "locked": False,
        "note": "Entity ids are assigned once. Changing this later means renaming every entity.",
    }


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
    prefix = await _prefix_state()
    hardware = await _hardware_state()

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
        # Which build is answering. Asked directly — "no idea what version is
        # running" — after an add-on update that may or may not have landed.
        "version": _app_version(),
        "complete": gateways["done"] and dismissed,
        "required_done": gateways["done"],
        "dismissed": dismissed,
        "steps": {"gateway": gateways, "ha": ha, "mqtt": mqtt, "prefix": prefix,
                  "hardware": hardware},
    }


class PrefixChoice(BaseModel):
    template: str


@router.post("/setup/prefix")
async def setup_choose_prefix(req: PrefixChoice):
    """Set the entity prefix during setup, before anything is published.

    Delegates to the MQTT route rather than writing the config value here —
    that one validates the template, and a second writer would be a second set
    of rules to keep in step.
    """
    from src.routes.api_mqtt import PrefixUpdateRequest, set_entity_prefix

    result = await set_entity_prefix(PrefixUpdateRequest(template=req.template))
    logger.info("setup: entity prefix set to %r", req.template)
    return result


@router.post("/setup/complete")
async def setup_complete(payload: dict | None = None):
    """Record that the wizard has been seen, so it does not reappear."""
    await db.set_config_value(SETUP_COMPLETE_KEY, "true")
    await db.log_admin_audit(
        event="Setup:Wizard_Completed", source="ui",
        details="First-run setup wizard completed",
    )
    return {"ok": True}


@router.post("/setup/reopen")
async def setup_reopen():
    """Let the wizard run again.

    It hides itself once `setup_complete` is set, and nothing cleared that — so
    the only way back was to edit the flag by hand or reinstall. That is a poor
    answer for the two steps most worth revisiting: the hardware summary, and
    the entity naming scheme, which is the one setting that cannot be changed
    casually afterwards.

    Nothing is reset. Every step reads live state, so reopening shows what is
    configured now rather than an empty form.
    """
    await db.set_config_value(SETUP_COMPLETE_KEY, "false")
    await db.log_admin_audit(
        event="Setup:Wizard_Reopened", source="ui",
        details="Setup wizard reopened from settings; no configuration was reset",
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
