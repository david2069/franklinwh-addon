"""Clear retained MQTT messages this integration no longer publishes to.

A retained message is how entities survive a Home Assistant restart: the broker
keeps the last message on each topic and hands it to every new subscriber, so
discovery configs and last values are there immediately rather than after the
next poll. That is why `Retain Discovery Messages` is on, and it is correct.

The same property is why deleted things come back. A retained message persists
until something overwrites it or explicitly clears it. Change the topic prefix,
change the entity-id template, or publish once with an empty `short_id`, and the
old topic is never written again — so nothing overwrites it, and Home Assistant
recreates a ghost entity from it on every restart.

A real instance of this: `franklinwh//status/battery_status` — note the empty
segment where the gateway short id belongs — still retained six months after it
was written, from a build that no longer exists.

Clearing one means publishing a zero-length payload to it with retain set. That
is the only way; there is no delete.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: How long to listen for retained messages before deciding the broker has sent
#: everything it has. Retained delivery is immediate on subscribe, so this only
#: has to cover the round trip.
COLLECT_SECONDS = 3.0


def _is_orphan(topic: str, prefix: str, live_ids: set[str]) -> str | None:
    """Why `topic` is an orphan, or None if it belongs to a live gateway.

    Only topics under our own prefix are ever considered — clearing anything
    else would be destroying another integration's data.
    """
    if not topic.startswith(f"{prefix}/"):
        return None

    rest = topic[len(prefix) + 1:]
    parts = rest.split("/")
    short_id = parts[0] if parts else ""

    if short_id == "":
        # "franklinwh//status/..." — published when short_id was empty. No
        # gateway can ever own this, so nothing will overwrite it.
        return "empty gateway id — no gateway can ever publish here"
    if live_ids and short_id not in live_ids:
        return f"gateway {short_id} is not registered here"
    return None


async def find_orphans(publisher, prefix: str, live_ids: set[str]) -> list[dict]:
    """Subscribe, collect retained topics, and report which are orphaned.

    Read-only.
    """
    import aiomqtt

    kwargs = dict(hostname=publisher.host, port=publisher.port)
    if publisher.username:
        kwargs["username"] = publisher.username
    if publisher.password:
        kwargs["password"] = publisher.password

    found: list[dict] = []
    seen: set[str] = set()
    try:
        async with aiomqtt.Client(**kwargs) as client:
            await client.subscribe(f"{prefix}/#")

            async def collect():
                async for msg in client.messages:
                    topic = msg.topic.value
                    if topic in seen:
                        continue
                    seen.add(topic)
                    # Only retained messages are our problem: a live message is
                    # about to be republished anyway.
                    if not getattr(msg, "retain", False):
                        continue
                    reason = _is_orphan(topic, prefix, live_ids)
                    if reason:
                        found.append({"topic": topic, "reason": reason})

            try:
                await asyncio.wait_for(collect(), timeout=COLLECT_SECONDS)
            except asyncio.TimeoutError:
                pass    # expected — this is how we stop listening
    except Exception as exc:
        logger.warning(f"retained cleanup: could not scan the broker: {exc!r}")
        return []

    if found:
        logger.info(f"retained cleanup: {len(found)} orphaned retained topic(s) found")
    return found


async def clear(publisher, topics: list[str], prefix: str) -> int:
    """Clear the given retained topics. Returns how many were cleared.

    Refuses anything outside our own prefix, whatever the caller asked for:
    this publishes destructively, and a caller bug must not be able to wipe
    another integration's retained state.
    """
    import aiomqtt

    safe = [t for t in topics if t.startswith(f"{prefix}/")]
    refused = [t for t in topics if t not in safe]
    if refused:
        logger.warning(f"retained cleanup: refusing topics outside {prefix}/: {refused}")
    if not safe:
        return 0

    kwargs = dict(hostname=publisher.host, port=publisher.port)
    if publisher.username:
        kwargs["username"] = publisher.username
    if publisher.password:
        kwargs["password"] = publisher.password

    cleared = 0
    try:
        async with aiomqtt.Client(**kwargs) as client:
            for topic in safe:
                # Zero-length payload with retain set is the only way to remove
                # a retained message. There is no delete in MQTT.
                await client.publish(topic, payload=b"", retain=True, qos=1)
                cleared += 1
    except Exception as exc:
        logger.warning(f"retained cleanup: clearing failed after {cleared}: {exc!r}")

    if cleared:
        logger.info(f"retained cleanup: cleared {cleared} retained topic(s)")
    return cleared


# ── Ghost devices ────────────────────────────────────────────────────────────
#
# Everything above deals with *state* topics under our own prefix. A ghost
# *device* is a different animal: it is created by a retained **discovery**
# config under `homeassistant/`, which the scan above never subscribes to and
# `clear()` explicitly refuses. So a device could not be removed by any of it.
#
# Devices are keyed in Home Assistant by `device.identifiers`, not by name.
# Commit 03cc5d8 changed ours from `franklinwh_{short_id}` to
# `franklinwh_{full_serial}`, which does not rename the device — it declares a
# new one. The old identifier was never published to again, so nothing
# overwrote its retained configs and Home Assistant kept rebuilding the old
# device, with every entity it had ever been given, on each restart. That is
# the "aGate X" sitting alongside "aGate 99900001".

#: Identity root for everything this integration has ever published. A
#: discovery config that does not carry it belongs to somebody else and is
#: never a candidate for removal, whatever the caller asks for.
OUR_IDENTITY_ROOT = "franklinwh"


def discovery_object_id(topic: str, discovery_prefix: str) -> str | None:
    """The object_id from a discovery config topic, or None if it isn't one.

    Both documented shapes are accepted:
        <prefix>/<component>/<object_id>/config
        <prefix>/<component>/<node_id>/<object_id>/config
    """
    if not topic.startswith(f"{discovery_prefix}/") or not topic.endswith("/config"):
        return None
    parts = topic.split("/")
    if len(parts) not in (4, 5):
        return None
    return parts[-2]


def is_ours(topic: str, discovery_prefix: str) -> bool:
    """Whether a discovery topic is one this integration could have written.

    Structural, not payload-based: the object_id is in the topic itself, so
    this holds even for a config whose payload is unreadable — which is
    exactly when a caller must not be trusted to have checked.
    """
    object_id = discovery_object_id(topic, discovery_prefix)
    return bool(object_id) and object_id.startswith(OUR_IDENTITY_ROOT)


def _identity_of(config: dict) -> str:
    """The device identifier a discovery payload declares, or ""."""
    device = config.get("device") or {}
    for ident in device.get("identifiers") or []:
        if isinstance(ident, str) and ident.startswith(f"{OUR_IDENTITY_ROOT}_"):
            return ident
    return ""


def classify_discovery(topic: str, config: dict, discovery_prefix: str,
                       live_identities: set[str]) -> dict | None:
    """Describe `topic` if it is a ghost of ours, else None.

    A config is a ghost when it is ours and declares a device identifier that
    no registered gateway publishes under. Anything we cannot positively
    identify as ours is left alone.
    """
    if not is_ours(topic, discovery_prefix):
        return None

    identity = _identity_of(config)
    if not identity:
        # Ours by topic but carrying no identifier we recognise. Retired
        # builds did this; it can never be adopted by a live gateway.
        identity = ""
        reason = "declares no FranklinWH device identifier"
    elif identity in live_identities:
        return None
    else:
        reason = f"device '{identity}' is not a registered gateway"

    device = config.get("device") or {}
    return {
        "topic": topic,
        "identity": identity,
        "device_name": (device.get("name") or "").strip() or "(unnamed device)",
        "entity_name": (config.get("name") or "").strip(),
        "reason": reason,
    }


async def find_ghost_devices(publisher, discovery_prefix: str,
                             live_identities: set[str]) -> list[dict]:
    """Retained discovery configs of ours that rebuild devices nothing owns.

    Returns one entry per ghost device, each carrying the topics that recreate
    it. Read-only.
    """
    import json

    import aiomqtt

    kwargs = dict(hostname=publisher.host, port=publisher.port)
    if publisher.username:
        kwargs["username"] = publisher.username
    if publisher.password:
        kwargs["password"] = publisher.password

    ghosts: list[dict] = []
    seen: set[str] = set()
    try:
        async with aiomqtt.Client(**kwargs) as client:
            await client.subscribe(f"{discovery_prefix}/#")

            async def collect():
                async for msg in client.messages:
                    topic = msg.topic.value
                    if topic in seen:
                        continue
                    seen.add(topic)
                    if not getattr(msg, "retain", False):
                        continue
                    payload = msg.payload or b""
                    if not payload:
                        continue    # already tombstoned
                    try:
                        config = json.loads(payload)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(config, dict):
                        continue
                    found = classify_discovery(topic, config, discovery_prefix, live_identities)
                    if found:
                        ghosts.append(found)

            try:
                await asyncio.wait_for(collect(), timeout=COLLECT_SECONDS)
            except asyncio.TimeoutError:
                pass    # expected — this is how we stop listening
    except Exception as exc:
        logger.warning(f"retained cleanup: could not scan discovery: {exc!r}")
        return []

    if ghosts:
        logger.info(f"retained cleanup: {len(ghosts)} ghost discovery config(s) found")
    return ghosts


def group_ghosts(ghosts: list[dict]) -> list[dict]:
    """Collapse ghost configs into the devices a user actually sees."""
    devices: dict[str, dict] = {}
    for g in ghosts:
        key = g["identity"] or g["device_name"]
        entry = devices.setdefault(key, {
            "identity": g["identity"],
            "device_name": g["device_name"],
            "reason": g["reason"],
            "topics": [],
            "entities": [],
        })
        entry["topics"].append(g["topic"])
        if g["entity_name"]:
            entry["entities"].append(g["entity_name"])
    for entry in devices.values():
        entry["entity_count"] = len(entry["topics"])
        entry["entities"].sort()
    return sorted(devices.values(), key=lambda d: -d["entity_count"])


async def clear_discovery(publisher, topics: list[str], discovery_prefix: str) -> int:
    """Tombstone the given discovery configs. Returns how many were cleared.

    Refuses any topic that is not structurally one of ours, whatever the
    caller asked for — the discovery prefix is shared with every other MQTT
    integration in the house, so a caller bug here would delete their devices.
    """
    import aiomqtt

    safe = [t for t in topics if is_ours(t, discovery_prefix)]
    refused = [t for t in topics if t not in safe]
    if refused:
        logger.warning(f"retained cleanup: refusing discovery topics that are not ours: {refused}")
    if not safe:
        return 0

    kwargs = dict(hostname=publisher.host, port=publisher.port)
    if publisher.username:
        kwargs["username"] = publisher.username
    if publisher.password:
        kwargs["password"] = publisher.password

    cleared = 0
    try:
        async with aiomqtt.Client(**kwargs) as client:
            for topic in safe:
                await client.publish(topic, payload=b"", retain=True, qos=1)
                cleared += 1
    except Exception as exc:
        logger.warning(f"retained cleanup: discovery clear failed after {cleared}: {exc!r}")

    if cleared:
        logger.info(f"retained cleanup: tombstoned {cleared} discovery config(s)")
    return cleared


def retired_identities(short_ids: set[str], live_identities: set[str]) -> set[str]:
    """Identifiers our own gateways used to publish under, and no longer do.

    Only the shapes this integration has actually shipped are listed, and each
    is tied to hardware that is registered here — the same gateway under an
    identifier it used to carry. That is narrow enough to act on without asking.

    Two shapes qualify:

    * `franklinwh_<short_id>` — the identity before 03cc5d8 changed it to the
      full serial.
    * `franklinwh_<full_serial>` in a different case. `gateway_service` upper-
      cases the serial before publishing, so a lowercase one on the broker was
      written by an older build. Home Assistant keys devices on the identifier
      string, so the two are separate devices and the old one keeps rebuilding
      itself from its retained configs — this is the 51-entity 'aGate 99900001'
      sitting beside the live gateway of the same name.

    Anything else that looks orphaned is left alone: it may belong to a gateway
    the user removed on purpose, or to a build we know nothing about, and
    deleting someone's entity history on a guess is not a repair.
    """
    retired = {f"{OUR_IDENTITY_ROOT}_{sid}" for sid in short_ids if sid}

    # Case variants of a live identity: same serial, so provably the same
    # hardware, and provably not what is being published now. Only the serial
    # varies — the prefix has always been written lowercase, so upper-casing
    # the whole string invents a shape that was never published.
    for identity in live_identities:
        if not identity.startswith(f"{OUR_IDENTITY_ROOT}_"):
            continue
        serial = identity[len(OUR_IDENTITY_ROOT) + 1:]
        retired.add(f"{OUR_IDENTITY_ROOT}_{serial.lower()}")
        retired.add(f"{OUR_IDENTITY_ROOT}_{serial.upper()}")

    # Whatever the live devices answer to is never retired, whatever case it
    # happens to be in.
    return retired - live_identities


async def auto_sweep(publisher, discovery_prefix: str, live_identities: set[str],
                     retired: set[str]) -> dict:
    """Remove our own retired devices, unattended. Returns a summary.

    Deliberately narrower than the manual scan: it clears only identities in
    `retired`, and reports the rest for a human to look at.
    """
    if not live_identities or not retired:
        return {"swept": 0, "removed": [], "left": []}

    ghosts = await find_ghost_devices(publisher, discovery_prefix, live_identities)
    if not ghosts:
        return {"swept": 0, "removed": [], "left": []}

    doomed = [g for g in ghosts if g["identity"] in retired]
    left = group_ghosts([g for g in ghosts if g["identity"] not in retired])

    cleared = 0
    if doomed:
        cleared = await clear_discovery(publisher, [g["topic"] for g in doomed], discovery_prefix)

    return {
        "swept": cleared,
        "removed": group_ghosts(doomed),
        "left": left,
    }
