"""Configure Home Assistant for itself when running as an add-on.

Under the Supervisor there is nothing for a user to enter: the add-on already
holds a token and already knows where Home Assistant is. It nonetheless
presented three separate empty forms — an HA connection panel reporting
"Host Address: Not Set / Access Token: None Set", an empty Home Assistant
Instances list, and a Companion Devices table with nothing in it — while the
Home Automation tab, reading the effective connection, simultaneously said
"HA Integration Started".

Both were true of different things. `api_ha._get_ha_client()` prefers
SUPERVISOR_TOKEN and self-configures, so the *connection* works; the panels were
reporting *stored* values, which are legitimately empty because nothing ever
wrote them. Notification routing is the part that genuinely breaks: a device
route is foreign-keyed to an `ha_instances` row, and there was no row, so there
was nothing to route to.

So: seed the local instance from the connection we already have, and discover
the companion apps Home Assistant already exposes. Both are idempotent, both
skip anything the user created themselves, and neither is fatal.
"""
from __future__ import annotations

import logging

from src.services import db

logger = logging.getLogger(__name__)

LOCAL_INSTANCE_ID = "local-supervisor"
LOCAL_ALIAS = "Home Assistant (this instance)"


def resolve_instance_token(inst: dict | None) -> str:
    """The token to authenticate against `inst`, resolved at the moment of use.

    The Supervisor mints a fresh SUPERVISOR_TOKEN for every container start, so
    the copy written into `ha_instances` when the local instance was seeded is
    dead as soon as the add-on restarts. Nothing refreshed it, so discovery and
    every device dispatch through this instance returned 401 while the legacy
    connection — rewritten each boot by populate_stored_connection() — kept
    working, which is why notifications silently fell back to it.

    Read the environment for the local instance; the stored value is only
    meaningful for remote instances the user configured by hand.
    """
    inst = inst or {}
    if inst.get("id") == LOCAL_INSTANCE_ID:
        from src.services.addon_info import supervisor_token
        live = supervisor_token()
        if live:
            return live
    return (inst.get("token") or "").strip()



async def seed_local_instance() -> bool:
    """Register this Home Assistant as an instance. Returns True if created.

    Only under the Supervisor: elsewhere the host and token are genuinely
    unknown and asking is correct.
    """
    from src.routes.api_ha import _get_ha_client
    from src.services.addon_info import supervisor_token

    token = supervisor_token()
    if not token:
        return False

    base, _auth, env = await _get_ha_client()
    if env != "ha_addon" or not base:
        return False

    try:
        existing = await db.get_ha_instances()
    except Exception:
        logger.debug("ha_autoconfig: could not read instances", exc_info=True)
        return False

    # Someone may have added this instance by hand before the seeding existed.
    # Matching on host as well as id avoids handing them a duplicate.
    for inst in existing or []:
        if inst.get("id") == LOCAL_INSTANCE_ID or (inst.get("host") or "").rstrip("/") == base.rstrip("/"):
            return False

    await db.upsert_ha_instance({
        "id": LOCAL_INSTANCE_ID,
        "alias": LOCAL_ALIAS,
        "host": base,
        "token": token,
        "enabled": 1,
        # Default only when nothing else claims it — a user who registered a
        # remote instance first meant that one.
        "is_default": 1 if not existing else 0,
    })
    logger.info(f"Home Assistant registered automatically as '{LOCAL_ALIAS}' ({base})")
    return True


async def discover_notify_targets(limit: int = 25) -> int:
    """Add companion apps Home Assistant already exposes. Returns the count added.

    `notify.mobile_app_*` is what the companion app registers, and it is the
    only notify service this integration can meaningfully target — a generic
    notify service has no device to send an actionable button to.
    """
    try:
        instances = await db.get_ha_instances()
    except Exception:
        return 0
    local = next((i for i in (instances or []) if i.get("id") == LOCAL_INSTANCE_ID), None)
    if not local:
        return 0

    # Reuse the discovery that already exists rather than a second copy of it.
    # The duplicate stored "notify.mobile_app_x" while the sender builds
    # /api/services/notify/{target} and therefore needs the BARE service name —
    # so every auto-added device pointed at
    # /api/services/notify/notify.mobile_app_x and delivered nothing, while the
    # UI rendered "notify.notify.mobile_app_x".
    try:
        from src.services.notification_sender import discover_notify_targets as _discover

        found = await _discover()
    except Exception as exc:
        logger.debug(f"ha_autoconfig: could not list services: {exc!r}")
        return 0

    if not found.get("ok"):
        logger.debug(f"ha_autoconfig: notify discovery failed: {found.get('error')}")
        return 0

    targets = [t["service"] for t in (found.get("targets") or [])
               if str(t.get("service", "")).startswith("mobile_app_")]
    if not targets:
        logger.info(
            "No companion app is registered with Home Assistant yet — add one from "
            "the phone, then re-check."
        )
        return 0

    try:
        known = {d.get("service_target") for d in (await db.get_notification_devices() or [])}
    except Exception:
        known = set()

    added = 0
    for target in sorted(targets)[:limit]:
        # BARE service name: the sender builds /api/services/notify/{target},
        # so a stored "notify." prefix produces a doubled path that delivers
        # nothing.
        service = target
        if service in known:
            continue
        # A stable id, so re-running cannot produce a second row for one phone.
        await db.upsert_notification_device({
            "id": f"auto-{target}",
            "ha_instance_id": LOCAL_INSTANCE_ID,
            "alias": target.replace("mobile_app_", "").replace("_", " ").title(),
            "service_target": service,
            "enabled": 1,
        })
        added += 1

    if added:
        logger.info(f"Discovered {added} companion device(s) for notifications")
    return added


async def run() -> dict:
    """Seed the instance, then discover devices. Best-effort throughout."""
    result = {"instance_seeded": False, "devices_added": 0}
    try:
        result["connection_populated"] = await populate_stored_connection()
    except Exception:
        logger.warning("ha_autoconfig: could not populate the stored connection", exc_info=True)
    try:
        result["instance_seeded"] = await seed_local_instance()
    except Exception:
        logger.warning("ha_autoconfig: could not register this instance", exc_info=True)
    try:
        result["targets_repaired"] = await repair_notify_targets()
    except Exception:
        logger.warning("ha_autoconfig: could not repair notify targets", exc_info=True)
    try:
        result["devices_added"] = await discover_notify_targets()
    except Exception:
        logger.warning("ha_autoconfig: could not discover companion devices", exc_info=True)
    return result


async def repair_notify_targets() -> int:
    """Strip a leading "notify." from stored device targets. Returns rows fixed.

    Auto-discovery stored `notify.mobile_app_x` while the sender builds
    `/api/services/notify/{target}` — producing
    `/api/services/notify/notify.mobile_app_x`, which Home Assistant does not
    route. Every auto-added device was enabled, looked correct in the table as
    `notify.notify.mobile_app_x`, and delivered nothing.

    Rows the user typed by hand are repaired too: the same doubled path is wrong
    however it got there, and the bare form is what every consumer expects.
    """
    try:
        devices = await db.get_notification_devices()
    except Exception:
        logger.debug("notify repair: could not read devices", exc_info=True)
        return 0

    fixed = 0
    for dev in devices or []:
        target = (dev.get("service_target") or "").strip()
        if not target.startswith("notify."):
            continue
        bare = target[len("notify."):]
        if not bare:
            continue
        try:
            await db.upsert_notification_device({**dev, "service_target": bare})
            fixed += 1
        except Exception:
            logger.debug(f"notify repair: could not fix {dev.get('id')}", exc_info=True)

    if fixed:
        logger.info(
            f"Repaired {fixed} notification device target(s): removed a duplicated "
            "'notify.' prefix that made every push resolve to a path Home "
            "Assistant does not route."
        )
    return fixed


#: Marks ha_host/ha_token as ours to maintain. Without it we cannot tell a value
#: we populated from one the user typed, and would overwrite a deliberate
#: remote-Home-Assistant configuration on every boot.
CONNECTION_SOURCE_KEY = "ha_connection_source"
SOURCE_SUPERVISOR = "supervisor"


async def populate_stored_connection() -> bool:
    """Write the Supervisor connection into ha_host / ha_token. Returns True if set.

    A stop-gap, and deliberately so — see [BKL-HA-CONSOLIDATE]. Several panels
    read the STORED ha_host/ha_token rather than the effective connection, and
    report "DISCONNECTED / Host Address: Not Set" on an add-on whose Home
    Assistant connection is working. Fixing each panel means finding each panel;
    filling the store fixes all of them at once, including any not yet found.

    Two things make this safe:

    * **Rewritten every boot, not seeded once.** SUPERVISOR_TOKEN is issued per
      container start, so a value stored once is stale after the next restart —
      which would be worse than empty, because it looks configured.
    * **Only touches values we own.** A user pointing this at a remote Home
      Assistant has set ha_host deliberately, and having it silently replaced on
      every boot would be a genuine fault rather than a cosmetic one.
    """
    from src.services.addon_info import supervisor_token

    token = supervisor_token()
    if not token:
        return False

    try:
        from src.routes.api_ha import _get_ha_client

        base, _auth, env = await _get_ha_client()
    except Exception:
        logger.debug("connection populate: could not resolve the client", exc_info=True)
        return False

    if env != "ha_addon" or not base:
        return False

    try:
        existing_host = (await db.get_config_value("ha_host", "") or "").strip()
        source = (await db.get_config_value(CONNECTION_SOURCE_KEY, "") or "").strip()
    except Exception:
        return False

    if existing_host and source != SOURCE_SUPERVISOR:
        logger.debug("connection populate: ha_host was set by hand — leaving it alone")
        return False

    try:
        await db.set_config_value("ha_host", base)
        await db.set_config_value("ha_token", token)
        await db.set_config_value(CONNECTION_SOURCE_KEY, SOURCE_SUPERVISOR)
    except Exception:
        logger.debug("connection populate: write failed", exc_info=True)
        return False

    logger.info(
        f"Home Assistant connection populated from the Supervisor ({base}). "
        "Panels that read the stored host and token now show it as configured."
    )
    return True
