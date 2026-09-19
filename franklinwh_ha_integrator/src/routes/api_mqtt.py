"""MQTT Admin API routes — Phase 7 (rich status, config, reconnect) + service enable/disable."""
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import asyncio
import json

from src.app_state import get_app_state
from src.services import db
from src.services.pin_service import generate_pin, validate_pin, validate_pin_or_session
from src.services.mqtt_publisher import battery_short_id

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


def _pub():
    return get_app_state().get("publisher")


def _config():
    return get_app_state().get("config")


# ── System/config routes (kept from Phase 1) ──────────────────

@router.get("/config/safe")
async def safe_config():
    """Return app config with secrets redacted."""
    config = _config()
    if config is None:
        return {"status": "not_initialised"}
    return config.safe_dict()


@router.get("/diag")
async def diagnostics():
    """Basic connectivity diagnostics."""
    state = get_app_state()
    config = state.get("config")
    registry = state.get("registry")
    publisher = state.get("publisher")
    return {
        "app_version": state.get("version", "unknown"),
        "env": state.get("env", "unknown"),
        "db_path": state.get("db_path", "unknown"),
        "mqtt": {
            "host": config.mqtt_host if config else "unknown",
            "port": config.mqtt_port if config else 0,
            "connected": publisher.connected if publisher else False,
        },
        "gateways_registered": registry.count() if registry else 0,
        "gateways_running": registry.running_count() if registry else 0,
    }


# ── MQTT status & stats ───────────────────────────────────────

@router.get("/mqtt/status")
async def mqtt_status():
    """Rich MQTT broker connection status including runtime stats."""
    publisher = _pub()
    config = _config()
    if not publisher or not config:
        return {
            "connected": False,
            "broker": "not_configured",
            "topic_prefix": "",
            "discovery_prefix": "",
            "messages_published": 0,
            "reconnect_count": 0,
            "queue_depth": 0,
            "last_error": None,
        }
    return {
        "connected": publisher.connected,
        "broker": f"{publisher.host}:{publisher.port}",
        "host": publisher.host,
        "port": publisher.port,
        "topic_prefix": publisher.topic_prefix,
        "discovery_prefix": publisher.discovery_prefix,
        "username_set": bool(publisher.username),
        "messages_published": publisher.messages_published,
        "reconnect_count": publisher.reconnect_count,
        "queue_depth": publisher.queue_depth,
        "last_error": publisher.last_error,
        "publisher_running": publisher.is_running,
        "started_at": publisher.started_at,
        "last_publish_time": publisher.last_publish_time,
    }


async def _retained_context():
    """Publisher, topic prefix and the gateways that legitimately publish."""
    from src.services import db as _db

    publisher = _pub()
    if not publisher:
        return None, "", set()
    prefix = publisher.topic_prefix or "franklinwh"
    try:
        live = {str(g.get("short_id") or "") for g in (await _db.get_all_gateways() or [])}
    except Exception:
        live = set()
    return publisher, prefix, {s for s in live if s}


@router.get("/mqtt/retained/orphans")
async def mqtt_retained_orphans():
    """Retained topics under our prefix that no registered gateway can publish.

    A retained message persists until overwritten or explicitly cleared, so a
    topic we no longer publish to keeps recreating a ghost entity on every Home
    Assistant restart. Read-only.
    """
    from src.services import retained_cleanup

    publisher, prefix, live = await _retained_context()
    if not publisher:
        return {"ok": True, "checked": False, "orphans": [],
                "detail": "MQTT publisher is not running."}

    orphans = await retained_cleanup.find_orphans(publisher, prefix, live)
    return {"ok": True, "checked": True, "prefix": prefix,
            "registered_gateways": sorted(live), "orphans": orphans}


async def _live_device_identities() -> set[str]:
    """The `device.identifiers` every registered gateway publishes under.

    Must match mqtt_publisher exactly: `franklinwh_{full_serial}`. The retired
    `franklinwh_{short_id}` form is deliberately absent — a config still using
    it is precisely the ghost we are looking for.
    """
    from src.services import db as _db

    identities: set[str] = set()
    try:
        for g in (await _db.get_all_gateways() or []):
            full = str(g.get("full_serial") or "").strip()
            if full:
                identities.add(f"franklinwh_{full}")
    except Exception:
        logger.warning("mqtt: could not read gateways for ghost detection", exc_info=True)
    return identities


class GhostAutoRequest(BaseModel):
    enabled: bool


@router.get("/mqtt/discovery/auto")
async def mqtt_ghost_auto_get():
    """Whether old devices of ours are removed automatically at startup."""
    from src.services import db as _db

    raw = await _db.get_config_value("mqtt_auto_remove_ghosts", "1")
    return {"ok": True, "enabled": str(raw) == "1"}


@router.post("/mqtt/discovery/auto")
async def mqtt_ghost_auto_set(req: GhostAutoRequest):
    from src.services import db as _db

    await _db.set_config_value("mqtt_auto_remove_ghosts", "1" if req.enabled else "0")
    return {"ok": True, "enabled": req.enabled}


@router.get("/mqtt/discovery/ghosts")
async def mqtt_discovery_ghosts():
    """Devices Home Assistant keeps rebuilding that no gateway here owns.

    A device is keyed by `device.identifiers`. When ours changed, the old
    identifier stopped being published to — so nothing overwrote its retained
    discovery configs and Home Assistant rebuilt the old device, with every
    entity it ever had, on each restart. Read-only.
    """
    from src.services import retained_cleanup

    publisher = _pub()
    if not publisher:
        return {"ok": True, "checked": False, "devices": [],
                "detail": "MQTT publisher is not running."}

    discovery_prefix = getattr(publisher, "discovery_prefix", "homeassistant") or "homeassistant"
    live = await _live_device_identities()
    if not live:
        # Without a known-good identity every config of ours would look
        # orphaned, and the UI would offer to delete the live device.
        return {"ok": True, "checked": False, "devices": [],
                "detail": "No registered gateway serials — cannot tell live devices from ghosts."}

    ghosts = await retained_cleanup.find_ghost_devices(publisher, discovery_prefix, live)
    return {"ok": True, "checked": True, "discovery_prefix": discovery_prefix,
            "live_identities": sorted(live),
            "devices": retained_cleanup.group_ghosts(ghosts)}


class DiscoveryClearRequest(BaseModel):
    topics: list[str] = []
    confirm: str = ""


@router.post("/mqtt/discovery/clear")
async def mqtt_discovery_clear(req: DiscoveryClearRequest):
    """Tombstone ghost discovery configs. Destructive, so explicitly confirmed."""
    from src.services import retained_cleanup

    if req.confirm.strip().lower() != "clear":
        raise HTTPException(status_code=400,
                            detail="Confirmation required — this permanently removes retained discovery configs.")

    publisher = _pub()
    if not publisher:
        raise HTTPException(status_code=503, detail="MQTT publisher is not running.")

    discovery_prefix = getattr(publisher, "discovery_prefix", "homeassistant") or "homeassistant"

    # Never allow a live gateway's own device to be tombstoned, however the
    # request arrived: that would delete the working device and its history.
    # The topic cannot answer this — entity unique_ids are built from short_id
    # while device identity is the full serial — so re-read the broker and
    # clear only what is *currently* classified as a ghost.
    live = await _live_device_identities()
    if not live:
        raise HTTPException(
            status_code=400,
            detail="No registered gateway serials — refusing to remove anything.")

    ghosts = await retained_cleanup.find_ghost_devices(publisher, discovery_prefix, live)
    ghost_topics = {g["topic"] for g in ghosts}
    requested = [t for t in req.topics if t in ghost_topics]
    refused = [t for t in req.topics if t not in ghost_topics]
    if refused:
        logger.warning("mqtt: refusing %d topic(s) not currently ghosts: %s", len(refused), refused[:5])
    if not requested:
        return {"ok": True, "cleared": 0,
                "detail": "Nothing removed — none of those topics are ghosts now."}

    cleared = await retained_cleanup.clear_discovery(publisher, requested, discovery_prefix)
    return {"ok": True, "cleared": cleared,
            "detail": f"Removed {cleared} discovery config(s). Restart Home Assistant to drop the ghost device."}


class RetainedClearRequest(BaseModel):
    topics: list[str] = []
    confirm: str = ""


@router.post("/mqtt/retained/clear")
async def mqtt_retained_clear(req: RetainedClearRequest):
    """Clear retained topics. Destructive, so explicitly confirmed."""
    from src.services import retained_cleanup

    if req.confirm.strip().lower() != "clear":
        raise HTTPException(status_code=400,
                            detail="Confirmation required — this permanently removes retained messages.")

    publisher, prefix, _live = await _retained_context()
    if not publisher:
        raise HTTPException(status_code=503, detail="MQTT publisher is not running.")

    cleared = await retained_cleanup.clear(publisher, req.topics, prefix)
    return {"ok": True, "cleared": cleared,
            "detail": f"Cleared {cleared} retained topic(s). Restart Home Assistant to drop the ghost entities."}


DEFAULT_DEVICE_NAME = "FranklinWH {model} {serial4}"


class DeviceNameRequest(BaseModel):
    template: str


@router.get("/mqtt/device-name")
async def get_device_name():
    """The Home Assistant device-name template, and what it currently renders."""
    stored = await db.get_config_value("mqtt_device_name", DEFAULT_DEVICE_NAME)
    publisher = _pub()
    return {
        "ok": True,
        "template": stored,
        "default": DEFAULT_DEVICE_NAME,
        "tokens": ["{model}", "{serial4}", "{short_id}", "{serial}", "{name}"],
        "in_sync": (getattr(publisher, "device_name_template", None) == stored)
                   if publisher else False,
    }


@router.put("/mqtt/device-name")
async def set_device_name(req: DeviceNameRequest):
    """Save the device-name template.

    Renaming a device in Home Assistant does not move its entities or their
    history — entity ids follow the unique_id, which this does not touch. It is
    a label, and safe to change.
    """
    tmpl = (req.template or "").strip()
    if not tmpl:
        raise HTTPException(400, "Device name template cannot be empty")
    if len(tmpl) > 96:
        raise HTTPException(400, "Device name template too long (max 96 chars)")
    # A template with no token renders the same name for every gateway, which
    # collapses a multi-gateway install into one indistinguishable list.
    if not any(t in tmpl for t in ("{model}", "{serial4}", "{short_id}", "{serial}", "{name}")):
        raise HTTPException(
            400,
            "Include at least one token so gateways can be told apart: "
            "{model}, {serial4}, {short_id}, {serial} or {name}",
        )

    await db.set_config_value("mqtt_device_name", tmpl)
    publisher = _pub()
    if publisher:
        publisher.device_name_template = tmpl
    logger.info(f"mqtt_device_name set to {tmpl!r} — republish for it to take effect")
    return {"ok": True, "template": tmpl,
            "detail": "Saved. Republish discovery for Home Assistant to pick it up."}


#: Slugs other FranklinWH integrations published under different names. Offered
#: as presets so a dashboard built against one of them can be matched without
#: anybody reverse-engineering the difference from a broken card.
KNOWN_SLUG_ALIASES = {
    "battery_soc": "state_of_charge",
}


class SlugAliasRequest(BaseModel):
    aliases: dict[str, str]


@router.get("/mqtt/slug-aliases")
async def get_slug_aliases():
    """Slug overrides applied before the prefix, and the known presets."""
    stored = await db.get_config_value("mqtt_slug_aliases", None)
    if not isinstance(stored, dict):
        stored = {}
    publisher = _pub()
    return {
        "ok": True,
        "aliases": stored,
        "suggested": KNOWN_SLUG_ALIASES,
        "in_sync": (getattr(publisher, "slug_aliases", None) == stored) if publisher else False,
    }


@router.put("/mqtt/slug-aliases")
async def set_slug_aliases(req: SlugAliasRequest):
    """Save slug overrides.

    A slug is part of the unique_id, so changing one is a migration exactly like
    a prefix change: Home Assistant sees a new entity and the old one is
    orphaned. Republish afterwards, and expect to tombstone the old ids.
    """
    import re as _re

    clean: dict[str, str] = {}
    for src_slug, dst_slug in (req.aliases or {}).items():
        src_slug = str(src_slug).strip()
        dst_slug = str(dst_slug).strip()
        if not src_slug or not dst_slug:
            continue
        if not _re.fullmatch(r"[a-z0-9_]{1,64}", dst_slug):
            raise HTTPException(
                400,
                f"Alias {dst_slug!r} may only contain lowercase letters, digits "
                "and underscores (max 64)",
            )
        if src_slug == dst_slug:
            continue    # a no-op alias is clutter, not configuration
        clean[src_slug] = dst_slug

    await db.set_config_value("mqtt_slug_aliases", clean)
    publisher = _pub()
    if publisher:
        publisher.slug_aliases = dict(clean)

    logger.info(f"mqtt_slug_aliases set: {clean or 'none'} — republish to apply")
    return {
        "ok": True,
        "aliases": clean,
        "detail": (
            f"Saved {len(clean)} alias(es). A slug is part of the unique_id, so "
            "republish and expect the old entity ids to be orphaned."
        ),
    }


@router.get("/mqtt/topics")
async def mqtt_topics():
    """
    Return the canonical MQTT topic patterns used by this integrator.
    Useful for verifying what's being published to the broker.
    """
    publisher = _pub()
    if not publisher:
        return {"topics": []}

    p = publisher.topic_prefix
    d = publisher.discovery_prefix
    topics = [
        {"pattern": f"{d}/{{component}}/franklinwh_{{short_id}}/{{slug}}/config",
         "description": "HA MQTT Discovery config", "retained": True},
        {"pattern": f"{p}/{{short_id}}/availability",
         "description": "Gateway online/offline", "retained": True},
        {"pattern": f"{p}/{{short_id}}/stats/{{slug}}",
         "description": "aGate telemetry state values", "retained": False},
        {"pattern": f"{p}/{{short_id}}/battery/{{bat_id}}/{{slug}}",
         "description": "aPower battery state values", "retained": False},
        {"pattern": f"{p}/{{short_id}}/control/{{slug}}/set",
         "description": "HA command topics (subscribed by CommandListener)", "retained": False},
    ]
    return {
        "topic_prefix": p,
        "discovery_prefix": d,
        "topics": topics,
    }


@router.get("/mqtt/inspector")
async def mqtt_inspector_grid():
    """Builds the comprehensive data grid mapping all HA entities to their runtime topics and live values."""
    from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES, extract_stat, get_entities_for_profile
    publisher = _pub()
    registry = get_app_state().get("registry")
    if not publisher or not registry:
        return {"entities": []}

    rows = []
    
    for gw in registry._services.values():
        profile = gw.context.get("profile", {})
        full_serial = gw.full_serial
        short_id = gw.short_id
        custom_name = gw.context.get("name", "").strip()
        gw_name = custom_name if custom_name and custom_name.lower() != 'agate' else f"{short_id}"
        stats = gw.status.last_data or {}
        
        device_payload = publisher._build_device_payload(full_serial, gw_name, profile)
        avail_topic = f"{publisher.topic_prefix}/{short_id}/availability"
        
        # 1. Agate primary entities
        for ent in get_entities_for_profile(profile):
            state_topic = f"{publisher.topic_prefix}/{short_id}/{ent.state_group}/{ent.slug}"
            val = extract_stat(stats, ent.stat_path) if ent.stat_path else None
            
            # Smart Circuit custom mobile-app alias overlay
            sc_custom = stats.get(f"{ent.slug}_name") if ent.slug.startswith("smart_circuit_") else None
            ent_name = sc_custom or ent.name
            
            disc = publisher._build_entity_payload(ent, short_id, gw_name, device_payload, state_topic, avail_topic, custom_name=ent_name)
            
            rows.append({
                "id": f"{short_id}_{ent.slug}",
                "target": gw_name,
                "slug": ent.slug,
                "name": ent_name,
                "ha_type": ent.ha_type,
                "group": ent.state_group,
                "topic": state_topic,
                "value": val,
                "unit": ent.unit,
                "is_control": ent.is_control,
                "config": disc
            })
            
        # 2. Battery accessory entities
        batteries = stats.get("bms_units", [])
        for bat in batteries:
            bat_full = bat.get("full_serial") or bat.get("short_id", "unknown")
            bat_short = bat_full[-8:] if len(bat_full) >= 8 else bat_full
            b_name = f"aPower {bat_short}"
            
            for ent in BATTERY_ACCESSORY_ENTITIES:
                state_topic = f"{publisher.topic_prefix}/{short_id}/accessories/{bat_short}/{ent.slug}"
                
                # Unwrap the stat path relative to the battery object
                stripped_path = ent.stat_path.replace("bms.", "").replace("battery.", "")
                val = extract_stat(bat, stripped_path) if ent.stat_path else None
                
                # Use agate device payload (flat topology — battery anchors to aGate device)
                disc = publisher._build_entity_payload(ent, bat_short, b_name, device_payload, state_topic, avail_topic)
                
                rows.append({
                    "id": f"{bat_short}_{ent.slug}",
                    "target": b_name,
                    "slug": ent.slug,
                    "name": ent.name,
                    "ha_type": ent.ha_type,
                    "group": ent.state_group,
                    "topic": state_topic,
                    "value": val,
                    "unit": ent.unit,
                    "is_control": ent.is_control,
                    "config": disc
                })

    return {"entities": rows}

# ── MQTT config (persistent overrides) ───────────────────────

class MQTTConfigPatch(BaseModel):
    mqtt_enabled: bool | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    qos: int | None = None
    retain_discovery: bool | None = None
    topic_prefix: str | None = None
    discovery_prefix: str | None = None


@router.get("/mqtt/config")
async def get_mqtt_config():
    """Return current MQTT configuration (from config file + saved overrides)."""
    config = _config()
    if not config:
        raise HTTPException(status_code=503, detail="App not initialised")
    # Check for saved overrides in DB
    custom_prefix = await db.get_config_value("mqtt_topic_prefix")
    custom_discovery = await db.get_config_value("mqtt_discovery_prefix")
    return {
        "mqtt_enabled": config.mqtt_enabled,
        "host": config.mqtt_host,
        "port": config.mqtt_port,
        "username": config.mqtt_username,
        "password": "***" if config.mqtt_password else "",
        "client_id": config.mqtt_client_id,
        "qos": config.mqtt_qos,
        "retain_discovery": config.mqtt_retain_discovery,
        "topic_prefix": custom_prefix or config.topic_prefix,
        "discovery_prefix": custom_discovery or config.ha_discovery_prefix,
        "overrides_active": bool(custom_prefix or custom_discovery),
    }


@router.patch("/mqtt/config")
async def patch_mqtt_config(req: MQTTConfigPatch):
    """
    Save MQTT topic prefix overrides to the DB config store.
    The new values take effect on the next application restart.
    Does NOT hot-swap the running publisher (would invalidate all Discovery payloads).
    """
    changed = []
    
    # Simple JSON config values applied to SQLite
    if req.topic_prefix is not None:
        await db.set_config_value("mqtt_topic_prefix", req.topic_prefix)
        changed.append("topic_prefix")
    if req.discovery_prefix is not None:
        await db.set_config_value("mqtt_discovery_prefix", req.discovery_prefix)
        changed.append("discovery_prefix")

    # In a real environment, changing port/host/password requires options.json injection
    # For now we document that these are immutable via UI or we'd write them out softly
    
    # Toggle Engine Control
    if req.mqtt_enabled is not None:
        # If toggling off, disable background task instantly
        publisher = _pub()
        if publisher and not req.mqtt_enabled:
            await publisher.stop()
        elif publisher and req.mqtt_enabled and not publisher.is_running:
            publisher.start()
        changed.append("mqtt_enabled")

    if not changed:
        return {"ok": True, "changed": [], "note": "Nothing to update"}

    return {
        "ok": True,
        "changed": changed,
        "note": "Configuration patched. Some settings may require a hard service reboot to persist.",
    }


# ── Reconnect ─────────────────────────────────────────────────

@router.post("/mqtt/reconnect")
async def mqtt_reconnect():
    """Force a publisher reconnect + discovery re-publish by cycling the background task."""
    publisher = _pub()
    if not publisher:
        raise HTTPException(status_code=503, detail="Publisher not initialised")
    await publisher.stop()
    # Reset discovery flag so retained payloads are re-published on reconnect
    registry = get_app_state().get("registry")
    if registry:
        for svc in registry._services.values():
            svc.status.mqtt_published = False
    publisher.start()
    return {"ok": True, "note": "Publisher restarted — discovery will re-publish on next poll"}


# ── Publish / Unpublish ───────────────────────────────────────

@router.post("/mqtt/publish")
async def mqtt_force_publish():
    """
    Force-republish all HA MQTT Discovery payloads for every registered gateway.
    Resets the mqtt_published flag so the NEXT poll cycle immediately re-fires
    publish_discovery() — no restart required.
    """
    publisher = _pub()
    registry = get_app_state().get("registry")
    if not publisher:
        raise HTTPException(status_code=503, detail="Publisher not initialised")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")

    count = 0
    for svc in registry._services.values():
        svc.status.mqtt_published = False
        count += 1

    logger.info(f"Force-publish requested: reset mqtt_published for {count} gateway(s)")
    return {
        "ok": True,
        "gateways_reset": count,
        "note": f"Discovery will re-publish for {count} gateway(s) on the next poll cycle (≤30s).",
    }


@router.post("/mqtt/unpublish")
async def mqtt_unpublish():
    """
    Clear all HA MQTT Discovery retained payloads for every registered gateway.
    Sends an empty retained message to every discovery topic, which tells HA to remove
    the entities from its registry. Mirrors FEM's 'Unpublish' button behaviour.
    """
    from src.models.entities import get_entities_for_profile, BATTERY_ACCESSORY_ENTITIES

    publisher = _pub()
    registry = get_app_state().get("registry")
    if not publisher:
        raise HTTPException(status_code=503, detail="Publisher not initialised")
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    if not publisher.connected:
        raise HTTPException(status_code=503, detail="MQTT broker not connected — cannot unpublish")

    cleared = 0
    for svc in registry._services.values():
        full_serial = svc.full_serial
        short_id = svc.short_id
        profile = svc.context.get("profile", {})
        stats = svc.status.last_data or {}

        # Clear aGate entity discovery topics
        for ent in get_entities_for_profile(profile):
            disc_topic = (
                f"{publisher.discovery_prefix}/{ent.ha_type}/"
                f"{publisher._make_uid(short_id, ent.slug)}/config"
            )
            publisher.enqueue(disc_topic, "", retain=True, qos=publisher.default_qos)
            cleared += 1

        # Clear battery accessory entity discovery topics
        batteries = stats.get("bms_units", [])
        for bat in batteries:
            bat_short = battery_short_id(bat)
            if not bat_short:
                continue
            for ent in BATTERY_ACCESSORY_ENTITIES:
                disc_topic = (
                    f"{publisher.discovery_prefix}/{ent.ha_type}/"
                    f"{publisher._make_uid(bat_short, ent.slug)}/config"
                )
                publisher.enqueue(disc_topic, "", retain=True, qos=publisher.default_qos)
                cleared += 1

        # Also mark as unpublished so a future publish will redo discovery properly
        svc.status.mqtt_published = False

    logger.info(f"Unpublish: cleared {cleared} HA discovery topic(s) across {len(registry._services)} gateway(s)")
    return {
        "ok": True,
        "topics_cleared": cleared,
        "note": f"Sent empty retained payload to {cleared} discovery topic(s). HA will remove these entities.",
    }


# ── Entity Prefix Management ─────────────────────────────────

DEFAULT_ENTITY_PREFIX = "franklinwh_{short_id}_"
_PREFIX_RE = __import__("re").compile(r"^[a-z0-9_{}]+$")


@router.get("/mqtt/prefix")
async def get_entity_prefix():
    """
    Return the current MQTT entity unique_id prefix template and a rendered
    example for each registered gateway (all tokens resolved).
    """
    import re as _re

    publisher = _pub()
    registry  = get_app_state().get("registry")
    stored    = await db.get_config_value("mqtt_entity_prefix", DEFAULT_ENTITY_PREFIX)
    template  = stored or DEFAULT_ENTITY_PREFIX

    def _sanitise(s: str) -> str:
        s = s.lower().strip()
        s = _re.sub(r"[\s\-\.]+", "_", s)
        s = _re.sub(r"_+", "_", s)
        return s.strip("_")

    examples = []
    if registry:
        for svc in registry._services.values():
            sid        = svc.short_id
            full       = svc.full_serial or ""
            gw_name    = (svc.context.get("name") or "").strip()
            # site_id — prefer the gateways column, fall back to profile_json.site_id
            gw_row     = await db.get_gateway(sid)
            site_id    = ""
            if gw_row:
                site_id = (gw_row.get("site_id") or "").strip()
                if not site_id:
                    # Fallback: may have been written to profile_json by the setup wizard
                    import json as _json
                    pjson = gw_row.get("profile_json") or "{}"
                    try:
                        prof = _json.loads(pjson)
                        site_id = (prof.get("site_id") or "").strip()
                    except Exception:
                        pass

            # Resolve prefix with all tokens
            prefix = template
            prefix = prefix.replace("{short_id}",     sid)
            prefix = prefix.replace("{full_serial}",  _sanitise(full)     if full     else sid)
            prefix = prefix.replace("{gateway_name}", _sanitise(gw_name)  if gw_name  else sid)
            prefix = prefix.replace("{site_id}",      _sanitise(site_id)  if site_id  else sid)

            examples.append({
                "short_id":      sid,
                "full_serial":   full,
                "gateway_name":  gw_name,
                "site_id":       site_id,
                "prefix":        prefix,
                "example_entity": f"sensor.{prefix}grid_status",
            })

    return {
        "template":           template,
        "is_default":         template == DEFAULT_ENTITY_PREFIX,
        "examples":           examples,
        "publisher_template": publisher.entity_prefix_template if publisher else None,
        "in_sync":            (publisher.entity_prefix_template == template) if publisher else False,
        "supported_tokens": [
            {"token": "{short_id}",     "description": "Last 8 chars of gateway serial",          "example": examples[0]["short_id"]     if examples else "24170091"},
            {"token": "{full_serial}",  "description": "Full gateway serial (sanitised)",          "example": _sanitise(examples[0]["full_serial"])  if examples else "10060006a02f24170091"},
            {"token": "{gateway_name}", "description": "Gateway name from official app (sanitised)","example": _sanitise(examples[0]["gateway_name"]) if examples and examples[0]["gateway_name"] else "fhp"},
            {"token": "{site_id}",      "description": "Cloud site ID",                            "example": _sanitise(examples[0]["site_id"])      if examples and examples[0]["site_id"]      else "12345"},
        ],
    }


class PrefixUpdateRequest(BaseModel):
    template: str


@router.put("/mqtt/prefix")
async def set_entity_prefix(req: PrefixUpdateRequest):
    """
    Save a new entity prefix template to the DB.
    Does NOT apply immediately — a restart or /mqtt/migrate-prefix is required
    to tombstone old entities and republish under the new prefix.
    Template rules: lowercase alphanumeric + underscore + {short_id} token, max 48 chars.
    """
    tmpl = req.template.strip()
    if not tmpl:
        raise HTTPException(400, "Prefix template must not be empty")
    if len(tmpl) > 48:
        raise HTTPException(400, "Prefix template too long (max 48 chars)")
    allowed = __import__("re").compile(r"^[a-z0-9_{} ]+$")
    if not allowed.match(tmpl.replace("{", "").replace("}", "")):
        raise HTTPException(400, "Prefix may only contain lowercase letters, digits, and underscores")
    await db.set_config_value("mqtt_entity_prefix", tmpl)
    logger.info(f"Entity prefix template updated in DB: {tmpl!r}")
    return {
        "ok": True,
        "template": tmpl,
        "note": "Saved to DB. Run POST /mqtt/migrate-prefix to apply (tombstone old + republish new).",
    }


@router.get("/mqtt/prefix/scan")
async def scan_live_entity_prefix():
    """
    Subscribe to the MQTT broker for 2 seconds and count how many retained
    HA discovery messages exist under the current entity prefix.
    Returns: { found, prefix_in_use, samples[...] }
    Used as a pre-flight check before allowing a prefix change in the UI.
    """
    import aiomqtt
    publisher = _pub()
    registry = get_app_state().get("registry")
    if not publisher:
        raise HTTPException(503, "Publisher not initialised")

    stored = await db.get_config_value("mqtt_entity_prefix", DEFAULT_ENTITY_PREFIX)
    template = stored or DEFAULT_ENTITY_PREFIX

    # Build resolved prefixes for client-side filtering
    resolved_prefixes = []
    if registry:
        for svc in registry._services.values():
            prefix = template.replace("{short_id}", svc.short_id)
            resolved_prefixes.append(prefix)
    # Subscribe broadly — filter client-side to avoid invalid wildcard patterns
    scan_pattern = f"{publisher.discovery_prefix}/+/#"

    found = []
    client_kwargs = dict(hostname=publisher.host, port=publisher.port, identifier="fhai_prefix_scan")
    if publisher.username:
        client_kwargs["username"] = publisher.username
    if publisher.password:
        client_kwargs["password"] = publisher.password

    try:
        async with aiomqtt.Client(**client_kwargs) as client:
            await client.subscribe(scan_pattern)
            # Collect retained messages for 2 seconds, filter by prefix client-side
            try:
                async with asyncio.timeout(2.0):
                    async for msg in client.messages:
                        topic_str = str(msg.topic)
                        # Only count topics whose node matches our prefix(es)
                        topic_node = topic_str.split("/")[2] if topic_str.count("/") >= 2 else ""
                        if not resolved_prefixes or any(topic_node.startswith(p) for p in resolved_prefixes):
                            found.append(topic_str)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        raise HTTPException(503, f"MQTT scan failed: {exc}")

    return {
        "found": len(found),
        "prefix_in_use": template,
        "live_entities_detected": len(found) > 0,
        "samples": found[:10],
        "note": (
            f"{len(found)} live entity discovery topic(s) detected under prefix {template!r}. "
            "Migration required before changing prefix."
            if found else
            "No live entities detected under current prefix — safe to change freely."
        ),
    }


class MigratePrefixRequest(BaseModel):
    dry_run: bool = True
    new_template: str


@router.post("/mqtt/migrate-prefix")
async def migrate_entity_prefix(req: MigratePrefixRequest):
    """
    Migrate all MQTT entity discovery topics from the current (old) prefix to a new prefix.

    Sequence:
      1. Read old prefix from DB (or publisher default).
      2. Tombstone all discovery topics under old prefix (null retained payload).
      3. Save new prefix to DB + update publisher.entity_prefix_template.
      4. Reset mqtt_published flags so discovery re-publishes on next poll.

    dry_run=True returns a preview only — no MQTT messages sent, no DB writes.
    """
    from src.models.entities import get_entities_for_profile, BATTERY_ACCESSORY_ENTITIES

    publisher = _pub()
    registry = get_app_state().get("registry")
    if not publisher:
        raise HTTPException(503, "Publisher not initialised")
    if not publisher.connected:
        raise HTTPException(503, "MQTT broker not connected — cannot migrate")
    if not registry:
        raise HTTPException(503, "Registry not initialised")

    new_tmpl = req.new_template.strip()
    if not new_tmpl:
        raise HTTPException(400, "new_template must not be empty")
    if len(new_tmpl) > 48:
        raise HTTPException(400, "Prefix template too long (max 48 chars)")

    old_tmpl = await db.get_config_value("mqtt_entity_prefix", DEFAULT_ENTITY_PREFIX)
    old_tmpl = old_tmpl or DEFAULT_ENTITY_PREFIX

    if old_tmpl == new_tmpl:
        return {"ok": True, "dry_run": req.dry_run, "note": "Old and new prefix are identical — nothing to do.",
                "tombstoned": 0, "republish_queued": 0}

    # Build preview / action list
    preview_rows = []
    tombstone_count = 0

    for svc in registry._services.values():
        short_id = svc.short_id
        profile = svc.context.get("profile", {})
        stats = svc.status.last_data or {}
        batteries = stats.get("bms_units", [])

        for ent in get_entities_for_profile(profile):
            old_prefix = old_tmpl.replace("{short_id}", short_id)
            new_prefix = new_tmpl.replace("{short_id}", short_id)
            preview_rows.append({
                "old_entity_id": f"{ent.ha_type}.{old_prefix}{ent.slug}",
                "new_entity_id": f"{ent.ha_type}.{new_prefix}{ent.slug}",
            })
        for bat in batteries:
            bat_short = battery_short_id(bat)
            if not bat_short:
                continue
            for ent in BATTERY_ACCESSORY_ENTITIES:
                old_prefix = old_tmpl.replace("{short_id}", bat_short)
                new_prefix = new_tmpl.replace("{short_id}", bat_short)
                preview_rows.append({
                    "old_entity_id": f"{ent.ha_type}.{old_prefix}{ent.slug}",
                    "new_entity_id": f"{ent.ha_type}.{new_prefix}{ent.slug}",
                })

    if req.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "old_prefix": old_tmpl,
            "new_prefix": new_tmpl,
            "affected_entities": len(preview_rows),
            "preview": preview_rows,
        }

    # ── Live run ──────────────────────────────────────────────────
    await db.set_config_value("admin_audit_log",
        f"migrate-prefix: {old_tmpl!r} → {new_tmpl!r} at {__import__('datetime').datetime.utcnow().isoformat()}Z")

    for svc in registry._services.values():
        short_id = svc.full_serial[-8:]
        profile = svc.context.get("profile", {})
        stats = svc.status.last_data or {}
        batteries = stats.get("bms_units", [])
        tombstone_count += publisher.tombstone_all_discovery(
            short_id, batteries, profile, override_prefix_template=old_tmpl
        )

    # Save new prefix to DB + hot-swap publisher template
    await db.set_config_value("mqtt_entity_prefix", new_tmpl)
    publisher.entity_prefix_template = new_tmpl

    # Reset discovery flags — next poll cycle will republish under new prefix
    for svc in registry._services.values():
        svc.status.mqtt_published = False

    logger.info(f"Entity prefix migrated: {old_tmpl!r} → {new_tmpl!r} ({tombstone_count} tombstoned)")
    return {
        "ok": True,
        "dry_run": False,
        "old_prefix": old_tmpl,
        "new_prefix": new_tmpl,
        "tombstoned": tombstone_count,
        "republish_queued": len(preview_rows),
        "note": f"Tombstoned {tombstone_count} old discovery topics. New prefix active. Discovery will republish on next poll (≤30s).",
    }


# ── WebSockets ────────────────────────────────────────────────

async def _websocket_authorised(websocket: WebSocket) -> bool:
    """Authenticate a WebSocket handshake the way AdminAuthMiddleware would.

    WebSockets never reach BaseHTTPMiddleware, so the global auth gate does not
    apply to them — and /api/ws/ is additionally in AUTH_EXEMPT_PREFIXES. This
    endpoint proxies the MQTT broker (using the app's own broker credentials)
    to whoever connects, so without this check an unauthenticated client on the
    LAN gets a live feed of every retained and live topic.

    Mirrors the HTTP tiers deliberately: open when security is disabled, else a
    session cookie or a token. Browsers send the same-origin `fhai_session`
    cookie on the handshake automatically, so the frontend needs no change.
    """
    import os
    from src.middleware.auth import verify_jwt

    try:
        raw = await db.get_config_value("security_enabled")
        security_enabled = str(raw).lower() in ("true", "1", "yes")
    except Exception:
        # Fail closed: if the posture cannot be determined, require credentials.
        security_enabled = True

    if os.environ.get("FWH_DISABLE_SECURITY", "").lower() in ("true", "1", "yes"):
        security_enabled = False
    if not security_enabled:
        return True

    # Same rule as the HTTP path: a stale cookie must not veto a valid one, so
    # every offered session cookie is verified rather than just the first.
    from src.middleware.auth import session_cookie_candidates
    for candidate in session_cookie_candidates(websocket.cookies):
        if await verify_jwt(candidate):
            return True

    token = websocket.query_params.get("token") or websocket.query_params.get("api_key")
    if token:
        if await verify_jwt(token):
            return True
        import hashlib
        if await db.verify_api_token(hashlib.sha256(token.encode("utf-8")).hexdigest()):
            return True

    return False


@router.websocket("/ws/mqtt")
async def websocket_mqtt_explorer(websocket: WebSocket):
    """
    Live WebSocket bridge into the local MQTT broker for the frontend MQTT Explorer modal.
    Parses 'subscribe' JSON actions from the frontend and blindly proxies all retained/live payloads.
    """
    if not await _websocket_authorised(websocket):
        logger.warning("ws/mqtt: rejecting unauthenticated handshake")
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()

    publisher = _pub()
    config = _config()
    if not publisher or not config:
        # Closed silently with code 1000 before this, which the browser shows as
        # a plain "Disconnected" — indistinguishable from a normal shutdown, and
        # with nothing in the log either. Say which half is missing.
        missing = "MQTT publisher" if not publisher else "MQTT configuration"
        logger.warning(f"ws/mqtt: refusing — {missing} is not available")
        await websocket.close(code=1011, reason=f"{missing} not available")
        return

    import aiomqtt
    client_kwargs = dict(hostname=publisher.host, port=publisher.port)
    if publisher.username:
        client_kwargs["username"] = publisher.username
    if publisher.password:
        client_kwargs["password"] = publisher.password

    try:
        async with aiomqtt.Client(**client_kwargs) as client:
            
            async def mqtt_receiver():
                try:
                    async for message in client.messages:
                        payload_str = ""
                        try:
                            payload_str = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
                        except Exception:
                            pass
                        
                        await websocket.send_text(json.dumps({
                            "type": "message",
                            "topic": message.topic.value,
                            "payload": payload_str,
                            "retain": message.retain,
                            "timestamp": asyncio.get_event_loop().time()
                        }))
                except Exception as e:
                    logger.warning(f"MQTT proxy receiver stopped: {e}")
            
            receive_task = asyncio.create_task(mqtt_receiver())
            
            try:
                while True:
                    text_data = await websocket.receive_text()
                    try:
                        cmd = json.loads(text_data)
                        if cmd.get("action") == "subscribe" and cmd.get("topic"):
                            await client.subscribe(cmd["topic"])
                    except json.JSONDecodeError:
                        pass
            except WebSocketDisconnect:
                receive_task.cancel()
    except Exception as e:
        # Falling off the end here closes with 1000, which reads as a clean
        # shutdown in the browser while the real cause sits only in the log.
        logger.error(f"ws/mqtt: broker bridge failed: {e!r}")
        try:
            await websocket.close(code=1011, reason=f"broker bridge failed: {type(e).__name__}")
        except Exception:
            pass   # already gone


# ── Service Management (enable / disable / status) ────────────────────────

def _uptime_seconds(ts_float: float | None) -> int | None:
    if ts_float is None:
        return None
    return max(0, int(time.time() - ts_float))


async def _get_mqtt_service_state() -> tuple[str, int | None]:
    """
    Returns (service_state, uptime_seconds).
    service_state: 'started' | 'stopped' | 'not_configured'

    Priority:
      1. If publisher is actually running → always 'started' (trust live state)
      2. DB mqtt_enabled flag → authoritative once set
      3. First install / no DB key:
           - if config has any broker host → 'stopped' (configured but not yet DB-enabled)
           - otherwise → 'not_configured'
    """
    publisher = _pub()
    config = _config()

    # 1. Live publisher always wins — handles pre-existing installs where DB key is absent
    if publisher and publisher.is_running:
        uptime = _uptime_seconds(publisher.started_at)
        return "started", uptime

    # 2. DB-driven state (set after first UI enable/disable action)
    enabled_db = await db.get_config_value("mqtt_enabled", None)

    if enabled_db is not None:
        return ("started", None) if enabled_db else ("stopped", None)

    # 3. First install / no DB key yet
    has_host = bool(config and config.mqtt_host)
    if has_host:
        # Broker configured but not yet explicitly enabled — show as stopped, not unconfigured
        return "stopped", None

    return "not_configured", None


@router.get("/mqtt/service-status")
async def mqtt_service_status():
    """
    Enhanced MQTT service status with lifecycle state and uptime.
    Used by the MQTT Admin tab status badges and data flow diagram.
    """
    publisher = _pub()
    config = _config()
    service_state, uptime_secs = await _get_mqtt_service_state()

    base_status = {
        "service_state": service_state,
        "uptime_seconds": uptime_secs,
        "connected": publisher.connected if publisher else False,
        "broker": f"{publisher.host}:{publisher.port}" if publisher else "not_configured",
        "host": publisher.host if publisher else (config.mqtt_host if config else ""),
        "port": publisher.port if publisher else (config.mqtt_port if config else 1883),
        "messages_published": publisher.messages_published if publisher else 0,
        "reconnect_count": publisher.reconnect_count if publisher else 0,
        "queue_depth": publisher.queue_depth if publisher else 0,
        "last_error": publisher.last_error if publisher else None,
        "publisher_running": publisher.is_running if publisher else False,
        "started_at": publisher.started_at if publisher else None,
        "last_publish_time": publisher.last_publish_time if publisher else None,
    }
    return base_status


@router.get("/mqtt/detect-broker")
async def mqtt_detect_broker():
    """
    Auto-detect MQTT broker availability.
    HA Addon: attempts to resolve 'core-mosquitto' (requires Mosquitto add-on).
    Docker/dev: returns manual mode — user must supply broker details.
    """
    import socket
    state = get_app_state()
    env = state.get("env", "dev")

    if env == "ha_addon":
        try:
            socket.getaddrinfo("core-mosquitto", 1883, timeout=2)
            return {
                "detected": True,
                "host": "core-mosquitto",
                "port": 1883,
                "source": "ha_addon_auto",
                "note": "Mosquitto Add-on detected — using core-mosquitto",
            }
        except (socket.gaierror, OSError):
            return {
                "detected": False,
                "host": "",
                "port": 1883,
                "source": "ha_addon_auto",
                "error": "Mosquitto Add-on not found — install it from the HA Add-on Store first",
            }
    else:
        # Docker/dev — user must provide external broker
        config = _config()
        return {
            "detected": False,
            "host": config.mqtt_host if config else "",
            "port": config.mqtt_port if config else 1883,
            "source": "manual",
            "note": f"{'Docker' if env == 'docker' else 'Dev'} mode — configure your external MQTT broker below",
        }


@router.post("/mqtt/generate-pin")
async def mqtt_generate_pin():
    """
    Generate a fresh UI-only one-time PIN for enabling/disabling MQTT.
    UI pin only — NOT for scripting (headless uses MQTT_ENABLED env var).
    Returns the raw 6-digit PIN — display to user ONCE.
    """
    raw_pin = await generate_pin("mqtt")
    return {"pin": raw_pin, "ttl_minutes": 10}


class MQTTPinRequest(BaseModel):
    pin: str = ""
    session_token: str | None = None


@router.post("/mqtt/enable")
async def mqtt_enable(req: MQTTPinRequest):
    """
    Enable MQTT publisher. Requires valid UI PIN or active 24h session token.
    On success: sets mqtt_enabled=true in DB, records mqtt_started_at, starts publisher.
    Returns session_token for 24h re-use.
    """
    valid, result = await validate_pin_or_session("mqtt", req.pin, req.session_token)
    if not valid:
        raise HTTPException(status_code=403, detail=result)

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.set_config_value("mqtt_enabled", True)
    await db.set_config_value("mqtt_started_at", now_iso)

    publisher = _pub()
    if publisher and not publisher.is_running:
        publisher.start()
        logger.info("MQTT publisher enabled via UI")

    return {"ok": True, "message": "MQTT integration enabled", "started_at": now_iso, "session_token": result}


@router.post("/mqtt/disable")
async def mqtt_disable(req: MQTTPinRequest):
    """
    Disable MQTT publisher. Requires valid UI PIN or active 24h session token.
    On success: stops publisher, sets mqtt_enabled=false in DB, resets discovery flags.
    Returns session_token for 24h re-use.
    """
    valid, result = await validate_pin_or_session("mqtt", req.pin, req.session_token)
    if not valid:
        raise HTTPException(status_code=403, detail=result)

    publisher = _pub()
    if publisher and publisher.is_running:
        await publisher.stop()

    # Reset discovery flag so retained payloads re-publish when re-enabled
    registry = get_app_state().get("registry")
    if registry:
        for svc in registry._services.values():
            svc.status.mqtt_published = False

    await db.set_config_value("mqtt_enabled", False)
    await db.set_config_value("mqtt_started_at", None)
    logger.info("MQTT publisher disabled via UI")
    return {"ok": True, "message": "MQTT integration disabled", "session_token": result}


@router.post("/mqtt/test")
async def mqtt_test():
    """
    Live connectivity test to the configured MQTT broker.
    Attempts a brief connection and disconnects. Returns {ok, latency_ms, error}.
    """
    config = _config()
    publisher = _pub()

    host = (publisher.host if publisher else None) or (config.mqtt_host if config else "localhost")
    port = (publisher.port if publisher else None) or (config.mqtt_port if config else 1883)
    username = (publisher.username if publisher else None) or (config.mqtt_username if config else "")
    password = (publisher.password if publisher else None) or (config.mqtt_password if config else "")

    t0 = time.monotonic()
    try:
        import aiomqtt
        client_kwargs = dict(hostname=host, port=port, identifier="fhai_test_probe")
        if username:
            client_kwargs["username"] = username
        if password:
            client_kwargs["password"] = password

        async with aiomqtt.Client(**client_kwargs) as _:
            pass  # connected + disconnected cleanly

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "host": host, "port": port, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "host": host, "port": port, "latency_ms": latency_ms, "error": str(exc)}

