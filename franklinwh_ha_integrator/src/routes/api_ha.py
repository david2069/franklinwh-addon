"""
api_ha.py — Home Assistant Supervisor / REST API proxy endpoints.

Token and host are resolved at REQUEST TIME (not import time), reading from:
  1. SUPERVISOR_TOKEN env var  → ha_addon runtime (always wins, no config needed)
  2. DB app_config (ha_host, ha_token) → set via UI configuration sub-tab
  3. HA_HOST + HA_TOKEN env vars  → fallback bootstrap (migrated to DB on startup)

Enable/disable gate:
  - First install: ha_enabled = False (DB key absent)
  - After UI enable (with PIN): ha_enabled = True written to DB; auto-restarts on
    subsequent boots without PIN

New endpoints:
  GET  /api/ha/status         → enhanced with service_state + uptime_seconds
  GET  /api/ha/config         → current config (token masked)
  POST /api/ha/config         → save ha_host + ha_token to DB (no PIN required)
  POST /api/ha/test           → live connectivity test
  POST /api/ha/generate-pin   → generate UI-only enable/disable PIN
  POST /api/ha/enable         → enable HA integration (requires UI PIN)
  POST /api/ha/disable        → disable HA integration (requires UI PIN)
  GET  /api/ha/entities       → paginated HA entity browser (unchanged)
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES
from src.services import db
from src.services.pin_service import generate_pin, validate_pin, validate_pin_or_session

logger = logging.getLogger(__name__)
router = APIRouter()

# FHAI-published entity slugs for cross-reference annotation
_FHAI_SLUGS: set[str] = {e.slug for e in AGATE_ENTITIES} | {
    e.slug for e in BATTERY_ACCESSORY_ENTITIES
}

# ── Supervisor token (auto-injected in ha_addon, never changes at runtime) ──
_SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


# ── Runtime HA client resolution ────────────────────────────────────────────

async def _get_ha_client() -> tuple[str, str, str]:
    """
    Resolve (base_url, Authorization_header, env_label) at request time.
    Priority:
      1. SUPERVISOR_TOKEN (ha_addon — self-configuring, no DB needed)
      2. DB ha_host + ha_token
      3. HA_HOST + HA_TOKEN env vars (bootstrap fallback)
    Returns ("", "", "unavailable") when nothing is configured.
    """
    if _SUPERVISOR_TOKEN:
        return "http://supervisor/core", f"Bearer {_SUPERVISOR_TOKEN}", "ha_addon"

    # DB values (set via UI config tab)
    db_host = await db.get_config_value("ha_host", "")
    db_token = await db.get_config_value("ha_token", "")

    if db_host and db_token:
        return db_host.rstrip("/"), f"Bearer {db_token}", "docker"

    # Env var fallback (bootstrap / dev)
    env_host = os.environ.get("HA_HOST", "")
    env_token = os.environ.get("HA_TOKEN", "")
    if env_host and env_token:
        return env_host.rstrip("/"), f"Bearer {env_token}", "docker"

    return "", "", "unavailable"


async def _ha_get(path: str, timeout: float = 8.0, quiet_404: bool = False) -> Optional[dict | list]:
    """Proxy a GET to the HA REST API. Returns parsed JSON or None on error.

    `quiet_404` drops the repeat of an already-reported miss to DEBUG. A
    polled entity that does not exist is one fault, not one per poll.
    """
    base, auth, _ = await _get_ha_client()
    if not base or not auth:
        return None
    url = f"{base}/api{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers={"Authorization": auth})
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        if quiet_404:
            logger.debug(f"HA API error {path}: {exc}")
        else:
            logger.warning(f"HA API error {path}: {exc}")
        return None


def _uptime_seconds(started_at_iso: str | None) -> int | None:
    """Calculate uptime in seconds from a stored ISO timestamp."""
    if not started_at_iso:
        return None
    try:
        started = datetime.fromisoformat(started_at_iso)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    except Exception:
        return None


async def _get_service_state() -> tuple[str, int | None]:
    """
    Returns (service_state, uptime_seconds).
    service_state: 'started' | 'stopped' | 'not_configured'

    Priority:
      1. Supervisor token (ha_addon) — always 'started' unless DB explicitly disables
      2. DB ha_enabled flag — authoritative once set
      3. First install / no DB key:
           - any token configured (DB, env var, or supervisor) → 'stopped'
           - otherwise → 'not_configured'
    """
    enabled_db = await db.get_config_value("ha_enabled", None)

    # Supervisor token — self-configuring, treat as started unless explicitly disabled
    if _SUPERVISOR_TOKEN:
        if enabled_db is False:
            return "stopped", None
        started_at = await db.get_config_value("ha_started_at")
        return "started", _uptime_seconds(started_at)

    if enabled_db is not None:
        if not enabled_db:
            return "stopped", None
        started_at = await db.get_config_value("ha_started_at")
        return "started", _uptime_seconds(started_at)

    # First install — no DB key yet. Determine configured state from env/DB.
    db_host  = await db.get_config_value("ha_host", "")
    db_token = await db.get_config_value("ha_token", "")
    env_host  = os.environ.get("HA_HOST", "")
    env_token = os.environ.get("HA_TOKEN", "")

    has_token = bool(db_token or env_token)
    has_host  = bool(db_host  or env_host)

    if has_token and has_host:
        # Configured via .env / options.json but not yet explicitly enabled — show as stopped
        return "stopped", None
    if has_token or has_host:
        # Partially configured
        return "stopped", None

    return "not_configured", None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/ha/status")
async def ha_status():
    """
    Returns HA connection state, summary stats, and service lifecycle info.
    Used by the Home Automation tab status header and data flow diagram.
    """
    service_state, uptime_secs = await _get_service_state()

    base, auth, env_label = await _get_ha_client()

    if not base or not auth or service_state != "started":
        db_token = await db.get_config_value("ha_token", "")
        token_set = bool(db_token or os.environ.get("HA_TOKEN", "") or _SUPERVISOR_TOKEN)
        return {
            "connected": False,
            "env": env_label,
            "service_state": service_state,
            "ha_token_set": token_set,
            "uptime_seconds": uptime_secs,
            "ha_version": None,
            "ha_name": None,
            "entity_count": 0,
            "fhai_entity_count": 0,
            "message": (
                "HA integration not configured — add host + token in Configuration tab"
                if service_state == "not_configured"
                else "HA integration disabled — enable via Configuration tab"
            ),
        }


    config = await _ha_get("/config")
    states = await _ha_get("/states")

    if config is None:
        return {
            "connected": False,
            "env": env_label,
            "service_state": "started",
            "uptime_seconds": uptime_secs,
            "ha_version": None,
            "ha_name": None,
            "entity_count": 0,
            "fhai_entity_count": 0,
            "message": "HA API unreachable — check host / token",
        }

    entity_count = len(states) if states else 0
    fhai_count = 0
    if states:
        for e in states:
            if any(slug in e.get("entity_id", "") for slug in _FHAI_SLUGS):
                fhai_count += 1

    # Check if a notification target is configured
    from src.services import db as _db
    notif_settings = await _db.get_notification_settings()
    ha_target_set = bool(notif_settings.get("ha_target"))

    return {
        "connected": True,
        "env": env_label,
        "service_state": "started",
        "uptime_seconds": uptime_secs,
        # Where the connection came from, so a panel can say "configured by the
        # Supervisor" rather than reporting the empty host/token fields that
        # nothing writes under the add-on and calling it DISCONNECTED.
        "self_configured": env_label == "ha_addon",
        "connection_source": env_label,
        "ha_version": config.get("version"),
        "ha_name": config.get("location_name", "Home Assistant"),
        "entity_count": entity_count,
        "fhai_entity_count": fhai_count,
        "ha_target_set": ha_target_set,
        "message": "Connected",
    }


@router.get("/ha/config")
async def ha_config_get():
    """Return current HA configuration (token masked, host shown)."""
    enabled = await db.get_config_value("ha_enabled", None)
    ha_host = await db.get_config_value("ha_host", "")
    ha_token = await db.get_config_value("ha_token", "")
    from src.services.addon_info import resolve_fhai_host
    fhai_host = await resolve_fhai_host()
    started_at = await db.get_config_value("ha_started_at")
    _, _, env_label = await _get_ha_client()
    service_state, uptime_secs = await _get_service_state()

    return {
        "enabled": bool(enabled),
        "ha_host": ha_host or os.environ.get("HA_HOST", ""),
        "ha_token_set": bool(ha_token or os.environ.get("HA_TOKEN", "") or _SUPERVISOR_TOKEN),
        "fhai_host": fhai_host,
        "env": env_label,
        "service_state": service_state,
        "uptime_seconds": uptime_secs,
        "started_at": started_at,
        "supervisor_mode": bool(_SUPERVISOR_TOKEN),
    }


class HAConfigSave(BaseModel):
    ha_host: str = ""
    ha_token: str = ""
    fhai_host: str = ""


@router.post("/ha/config")
async def ha_config_save(req: HAConfigSave):
    """
    Save HA host and/or token to DB. No PIN required — this is just config storage.
    Token is written only if provided (non-empty). Existing token preserved if blank.
    """
    changed = []
    if req.ha_host:
        await db.set_config_value("ha_host", req.ha_host.rstrip("/"))
        changed.append("ha_host")
    if req.ha_token:
        await db.set_config_value("ha_token", req.ha_token)
        changed.append("ha_token")
    if req.fhai_host:
        await db.set_config_value("fhai_host", req.fhai_host.rstrip("/"))
        changed.append("fhai_host")
    return {"ok": True, "changed": changed}


@router.post("/ha/test")
async def ha_test():
    """
    Live connectivity test against the configured HA instance.
    Returns {ok, version, latency_ms, error}.
    """
    base, auth, env_label = await _get_ha_client()
    if not base or not auth:
        return {"ok": False, "version": None, "latency_ms": None,
                "error": "HA not configured — set host + token first"}

    url = f"{base}/api/"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers={"Authorization": auth})
            r.raise_for_status()
            data = r.json()
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "version": data.get("version"),
            "ha_name": data.get("location_name"),
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "version": None, "latency_ms": latency_ms, "error": str(exc)}


@router.post("/ha/generate-pin")
async def ha_generate_pin():
    """
    Generate a fresh UI-only one-time PIN for enabling/disabling HA integration.
    UI pin — NOT for scripting (headless uses env vars directly).
    Returns the raw 6-digit PIN — display to user ONCE.
    """
    raw_pin = await generate_pin("ha")
    return {"pin": raw_pin, "ttl_minutes": 10}


class PinRequest(BaseModel):
    pin: str = ""
    session_token: str | None = None


@router.post("/ha/enable")
async def ha_enable(req: PinRequest):
    """
    Enable HA integration. Requires valid UI PIN or active 24h session token.
    On success: sets ha_enabled=true in DB, records ha_started_at.
    Returns session_token for 24h re-use.
    """
    valid, result = await validate_pin_or_session("ha", req.pin, req.session_token)
    if not valid:
        raise HTTPException(status_code=403, detail=result)

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.set_config_value("ha_enabled", True)
    await db.set_config_value("ha_started_at", now_iso)
    logger.info("HA integration enabled via UI")
    return {"ok": True, "message": "Home Assistant integration enabled", "started_at": now_iso, "session_token": result}


@router.post("/ha/disable")
async def ha_disable(req: PinRequest):
    """
    Disable HA integration. Requires valid UI PIN or active 24h session token.
    On success: sets ha_enabled=false in DB, clears ha_started_at.
    Returns session_token for 24h re-use.
    """
    valid, result = await validate_pin_or_session("ha", req.pin, req.session_token)
    if not valid:
        raise HTTPException(status_code=403, detail=result)

    await db.set_config_value("ha_enabled", False)
    await db.set_config_value("ha_started_at", None)
    logger.info("HA integration disabled via UI")
    return {"ok": True, "message": "Home Assistant integration disabled", "session_token": result}


@router.get("/ha/entities")
async def ha_entities(
    domain: str = Query("", description="Filter by domain (sensor, switch, select…)"),
    search: str = Query("", description="Search entity_id or friendly_name"),
    fhai_only: bool = Query(False, description="Show only FHAI-published entities"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Paginated HA entity browser with FHAI annotation.
    Only available when HA integration is enabled and connected.
    """
    service_state, _ = await _get_service_state()
    if service_state == "not_configured":
        return {"total": 0, "page": page, "limit": limit, "entities": [],
                "error": "HA not configured — add host + token in Configuration tab"}

    states = await _ha_get("/states")
    if states is None:
        return {"total": 0, "page": page, "limit": limit, "entities": [],
                "error": "HA API unreachable — check host / token"}

    results = []
    for e in states:
        entity_id: str = e.get("entity_id", "")
        attrs: dict = e.get("attributes", {})
        friendly_name: str = attrs.get("friendly_name", entity_id)
        state_val: str = e.get("state", "")
        last_updated: str = e.get("last_updated", "")
        e_domain = entity_id.split(".")[0] if "." in entity_id else ""
        unit = attrs.get("unit_of_measurement", "")
        device_class = attrs.get("device_class", "")
        is_fhai = any(slug in entity_id for slug in _FHAI_SLUGS)

        if domain and e_domain != domain:
            continue
        if fhai_only and not is_fhai:
            continue
        if search:
            sq = search.lower()
            if sq not in entity_id.lower() and sq not in friendly_name.lower():
                continue

        results.append({
            "entity_id": entity_id,
            "name": friendly_name,
            "domain": e_domain,
            "state": state_val,
            "unit": unit,
            "device_class": device_class,
            "last_updated": last_updated,
            "is_fhai_published": is_fhai,
        })

    results.sort(key=lambda x: (not x["is_fhai_published"], x["entity_id"]))
    total = len(results)
    offset = (page - 1) * limit
    page_data = results[offset: offset + limit]

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "entities": page_data,
        "domains": sorted(list({e["domain"] for e in results if e.get("domain")})),
    }


# ── Entity History: Solar Actuals ─────────────────────────────────────────────

@router.get("/ha/history")
async def ha_entity_history(
    entity_id: str = Query(..., description="HA entity ID to fetch history for"),
    hours: float = Query(24, ge=1, le=72, description="Hours of history to retrieve"),
    bucket_mins: int = Query(30, ge=5, le=60, description="Resampling bucket size in minutes"),
):
    """
    Fetch and resample HA entity history into fixed-size time buckets.

    Returns a list of {timestamp, value, unit} dicts — one per bucket — suitable
    for overlaying on the solar forecast chart as 'actuals'.

    Uses the HA REST /api/history/period endpoint with minimal_response=true.
    """
    from datetime import datetime, timezone, timedelta
    import math

    base, auth, _ = await _get_ha_client()
    if not base or not auth:
        raise HTTPException(status_code=503, detail="HA not configured")

    now_utc = datetime.now(timezone.utc)
    start   = now_utc - timedelta(hours=hours)
    start_s = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    url = (
        f"{base}/api/history/period/{start_s}"
        f"?filter_entity_id={entity_id}&minimal_response=true"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers={"Authorization": auth})
            r.raise_for_status()
            raw = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"HA history error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HA history fetch failed: {e}")

    if not raw or not raw[0]:
        return {"entity_id": entity_id, "buckets": [], "unit": "", "record_count": 0}

    records = raw[0]
    unit = ""

    # Parse (timestamp, float_value) pairs
    points: list[tuple[datetime, float]] = []
    for rec in records:
        state_str = rec.get("s") or rec.get("state", "")
        ts_str    = rec.get("lu") or rec.get("last_updated", "")
        # Minimal response uses 's' and 'lu' (last_updated as epoch float)
        try:
            if isinstance(ts_str, (int, float)):
                ts = datetime.fromtimestamp(ts_str, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            val = float(state_str)
            points.append((ts, val))
        except (ValueError, TypeError):
            continue
        if not unit and "a" in rec:
            unit = rec["a"].get("unit_of_measurement", "")

    if not points:
        return {"entity_id": entity_id, "buckets": [], "unit": unit, "record_count": 0}

    # Resample into fixed-size buckets
    bucket_s = bucket_mins * 60
    bucket_data: dict[int, list[float]] = {}
    for ts, val in points:
        epoch   = int(ts.timestamp())
        bucket  = (epoch // bucket_s) * bucket_s
        bucket_data.setdefault(bucket, []).append(val)

    buckets = []
    for bucket_epoch in sorted(bucket_data.keys()):
        vals   = bucket_data[bucket_epoch]
        avg    = sum(vals) / len(vals)
        ts_iso = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).isoformat()
        buckets.append({
            "timestamp": ts_iso,
            "value":     round(avg, 4),
            "unit":      unit,
            "samples":   len(vals),
        })

    return {
        "entity_id":    entity_id,
        "unit":         unit,
        "hours":        hours,
        "bucket_mins":  bucket_mins,
        "record_count": len(points),
        "buckets":      buckets,
        "range": {
            "start": points[0][0].isoformat(),
            "end":   points[-1][0].isoformat(),
        },
    }


# ── Entity Tools: Analyse ─────────────────────────────────────────────────────

@router.get("/ha/analyse")

async def ha_analyse():
    """
    Analyse the HA entity registry for FHAI-published entities.
    Cross-references HA states against FHAI's entity definitions to detect:
      - orphans:    entities in HA with no matching FHAI definition (stale/old prefix)
      - duplicates: multiple HA entities with the same unique_id suffix
      - naming:     HA friendly_name differs from current FHAI entity name

    Returns: { issues, issue_count, franklinwh_count, total_entities }
    Adapted from FEM ha_analyse — rewritten as async REST (FHAI has no WS client).
    """
    import httpx
    from src.services import db
    from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES

    ha_host = await db.get_config_value("ha_host", "")
    ha_token = await db.get_config_value("ha_token", "")
    if not ha_host or not ha_token:
        raise HTTPException(status_code=503, detail="HA not configured — add host and token in Home Automation → Configuration")

    # Fetch all HA states via REST (FHAI uses async httpx, not WebSocket)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{ha_host.rstrip('/')}/api/states",
                headers={"Authorization": f"Bearer {ha_token}"},
            )
        r.raise_for_status()
        all_states = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"HA API error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach HA: {e}")

    # Filter to FHAI-owned entities (prefix: franklinwh_*)
    fhai_states = [s for s in all_states if "franklinwh_" in s.get("entity_id", "")]
    total = len(all_states)
    franklinwh_count = len(fhai_states)

    # Build known unique_id set from FHAI entity definitions
    gateways = await db.get_all_gateways()
    known_unique_ids: set[str] = set()
    known_names: dict[str, str] = {}  # unique_id -> expected friendly_name

    for gw in gateways:
        short_id = gw["short_id"]
        for ent in AGATE_ENTITIES:
            uid = f"franklinwh_{short_id}_{ent.slug}"
            known_unique_ids.add(uid)
            known_names[uid] = ent.name
        # Battery entities: we don't know their short_id without live data — skip for now
        # A future improvement can look up from DB

    # Collect seen unique_ids for duplicate detection
    seen_uids: dict[str, list[str]] = {}  # uid -> list of entity_ids
    for state in fhai_states:
        attrs = state.get("attributes", {})
        uid = attrs.get("unique_id") or state.get("entity_id", "").split(".")[-1]
        # HA REST doesn't expose unique_id directly in states — derive from friendly_name/entity_id
        # Use entity_id as proxy key for duplicate detection
        eid = state.get("entity_id", "")
        seen_uids.setdefault(eid, []).append(eid)

    issues = []

    for state in fhai_states:
        eid = state.get("entity_id", "")
        attrs = state.get("attributes", {})
        friendly_name = attrs.get("friendly_name", "")

        # Derive unique_id from entity_id structure: franklinwh_{short_id}_{slug}
        # entity_id pattern: sensor.franklinwh_99900001_grid_status → uid = franklinwh_99900001_grid_status
        uid_candidate = eid.split(".", 1)[-1]  # strip domain prefix

        if uid_candidate not in known_unique_ids:
            # Could be a battery entity (unknown short_id) or genuine orphan
            # Mark as potential orphan — user can confirm
            issues.append({
                "type": "orphan",
                "severity": "warning",
                "entity_id": eid,
                "unique_id": uid_candidate,
                "message": f"No matching FHAI entity definition found for '{uid_candidate}'. May be stale or from a different prefix.",
                "suggested": None,
            })
            continue

        # Naming drift check: HA friendly_name vs FHAI definition
        expected_name = known_names.get(uid_candidate, "")
        if expected_name and friendly_name and expected_name.lower() not in friendly_name.lower():
            issues.append({
                "type": "naming",
                "severity": "info",
                "entity_id": eid,
                "unique_id": uid_candidate,
                "message": f"Friendly name mismatch: HA has '{friendly_name}', FHAI defines '{expected_name}'.",
                "suggested": expected_name,
            })

    return {
        "issues": issues,
        "issue_count": len(issues),
        "franklinwh_count": franklinwh_count,
        "total_entities": total,
        "orphan_count": sum(1 for i in issues if i["type"] == "orphan"),
        "naming_count": sum(1 for i in issues if i["type"] == "naming"),
    }


# ── Entity Tools: Repair ──────────────────────────────────────────────────────

class RepairRequest(BaseModel):
    dry_run: bool = True
    actions: list[dict]  # [{ type: "tombstone"|"republish", entity_id, ha_type, unique_id }]


@router.post("/ha/repair")
async def ha_repair(req: RepairRequest):
    """
    Repair FHAI HA entity issues.
    - tombstone: publish null MQTT retained payload → HA removes entity from registry
    - republish: trigger a fresh discovery publish for the entity

    dry_run=True: returns what would happen without sending MQTT messages.
    Adapted from FEM repair logic — uses MQTT tombstoning instead of WS registry deletes.
    """
    from src.main import get_app_state
    from src.services import db

    state = get_app_state()
    publisher = state.get("publisher")
    if not publisher:
        raise HTTPException(status_code=503, detail="MQTT publisher not running")

    results = []

    for action in req.actions:
        action_type = action.get("type")
        entity_id = action.get("entity_id", "")
        ha_type = action.get("ha_type", "sensor")
        unique_id = action.get("unique_id", entity_id.split(".", 1)[-1])

        try:
            if action_type == "tombstone":
                # Tombstoning publishes into the shared discovery prefix, where
                # every other MQTT integration in the house also lives. This
                # accepted whatever unique_id it was handed, so one malformed
                # row could have removed a neighbour's entity. Only ours.
                from src.services.retained_cleanup import OUR_IDENTITY_ROOT

                if not unique_id.startswith(OUR_IDENTITY_ROOT):
                    results.append({
                        "entity_id": entity_id,
                        "action": "tombstone",
                        "status": "refused",
                        "detail": f"'{unique_id}' is not a FranklinWH entity — refusing to remove it",
                    })
                    continue

                if not req.dry_run:
                    publisher.tombstone_discovery(ha_type=ha_type, unique_id=unique_id)
                results.append({
                    "entity_id": entity_id,
                    "action": "tombstone",
                    "status": "dry_run" if req.dry_run else "sent",
                    "topic": f"{publisher.discovery_prefix}/{ha_type}/{unique_id}/config",
                })

            elif action_type == "republish":
                # Trigger a full discovery republish for the owning gateway
                # Extract short_id from unique_id (franklinwh_{short_id}_{slug})
                parts = unique_id.split("_")
                short_id = parts[1] if len(parts) >= 3 else None
                if not short_id:
                    results.append({"entity_id": entity_id, "action": "republish", "status": "error", "detail": "Cannot derive short_id from unique_id"})
                    continue

                if not req.dry_run:
                    registry = state.get("registry")
                    if registry:
                        await registry.trigger_discovery(short_id)
                results.append({
                    "entity_id": entity_id,
                    "action": "republish",
                    "status": "dry_run" if req.dry_run else "triggered",
                    "short_id": short_id,
                })
            else:
                results.append({"entity_id": entity_id, "action": action_type, "status": "error", "detail": "Unknown action type"})

        except Exception as e:
            results.append({"entity_id": entity_id, "action": action_type, "status": "error", "detail": str(e)})

    fixed = sum(1 for r in results if r["status"] in ("sent", "triggered"))
    errors = sum(1 for r in results if r["status"] == "error")

    return {
        "dry_run": req.dry_run,
        "results": results,
        "fixed": fixed,
        "errors": errors,
    }


# ── Internal State Resolution ────────────────────────────────────────────────

#: Entities already reported as missing. A configured entity that does not
#: exist is a standing configuration fault, not an event: the Smart Dispatch
#: micro ticker re-reads it every 30 seconds, so without this one typo fills
#: the log forever. Say it once, loudly, then stop.
_MISSING_ENTITIES: set[str] = set()


def entity_id_looks_valid(entity_id: str) -> bool:
    """Whether `entity_id` is shaped like a Home Assistant entity id.

    Home Assistant ids are `domain.object_id`. A bare word can never resolve,
    whatever put it there, and is worth catching before it is polled forever.
    """
    entity_id = (entity_id or "").strip()
    if "." not in entity_id:
        return False
    domain, _, object_id = entity_id.partition(".")
    return bool(domain) and bool(object_id)


async def get_ha_state(entity_id: str) -> Optional[dict]:
    """Fetch the state of a single entity from HA."""
    if not entity_id_looks_valid(entity_id):
        if entity_id not in _MISSING_ENTITIES:
            _MISSING_ENTITIES.add(entity_id)
            logger.warning(
                "configured entity %r is not a valid Home Assistant entity id "
                "(expected domain.object_id) — it will never resolve; fix or "
                "clear it in the screen that configured it", entity_id,
            )
        return None

    state = await _ha_get(f"/states/{entity_id}", quiet_404=entity_id in _MISSING_ENTITIES)
    if state is None:
        _MISSING_ENTITIES.add(entity_id)
    else:
        _MISSING_ENTITIES.discard(entity_id)
    return state


# ── Multi-HA & Multi-Device CRUD API Routes ─────────────────────────────────

class HAInstanceUpsert(BaseModel):
    id: Optional[str] = None
    alias: str
    host: str
    token: Optional[str] = None
    enabled: Optional[bool] = True
    is_default: Optional[bool] = False


class NotificationDeviceUpsert(BaseModel):
    id: Optional[str] = None
    ha_instance_id: str
    alias: str
    service_target: str
    enabled: Optional[bool] = True
    owner_username: Optional[str] = None


class EntityRenameRequest(BaseModel):
    confirm: str = ""
    renames: list[dict] = []


@router.get("/ha/entity-rename/plan")
async def ha_entity_rename_plan():
    """Entities of ours whose id predates the current scheme. Read-only.

    Home Assistant assigns an entity id once and keeps it, so publishing
    `object_id` (0.6.59) fixed new entities and left existing ones alone.
    """
    from src.services import entity_rename

    renames = await entity_rename.plan()
    return {"ok": True, "count": len(renames), "renames": renames}


@router.post("/ha/entity-rename/apply")
async def ha_entity_rename_apply(req: EntityRenameRequest):
    """Rename entities onto the current scheme. Destructive, so confirmed.

    Home Assistant does not rewrite YAML, so anything referring to an old id —
    a dashboard, a script, an automation — must be updated to match.
    """
    from src.services import entity_rename

    if req.confirm.strip().lower() != "rename":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required — renaming breaks references to the old ids.")

    # Re-plan and intersect, so a stale list from a browser tab cannot rename
    # something that has since changed.
    current = {r["from"]: r for r in await entity_rename.plan()}
    if req.renames:
        wanted = [current[r["from"]] for r in req.renames if r.get("from") in current]
    else:
        wanted = list(current.values())

    result = await entity_rename.apply(wanted)
    return {"ok": True, **result}


@router.get("/ha/entity-survey")
async def ha_entity_survey():
    """FranklinWH entities already in Home Assistant, and who owns each.

    Read-only. Adoption — renaming a registry row to take over an old id — is a
    separate, previewed, explicitly confirmed action.
    """
    from src.services import entity_adoption
    from src.services import db as _db

    try:
        prefix = await _db.get_config_value("mqtt_entity_prefix", "") or ""
    except Exception:
        prefix = ""
    # The template carries {short_id}; the stable part is everything before it.
    our_prefix = prefix.split("{")[0]
    result = await entity_adoption.survey(our_unique_prefix=our_prefix)
    if not result.get("checked"):
        return result

    # Pair each leftover with the entity that now does its job, and say whether
    # it carries recorder history. Without the history an adoption is cosmetic;
    # with it, the Energy dashboard keeps its years of data.
    adoptable = result["foreign"] + result["orphaned"]
    result["foreign"] = entity_adoption.map_to_ours(result["foreign"], result["ours"])
    result["orphaned"] = entity_adoption.map_to_ours(result["orphaned"], result["ours"])

    try:
        ages = await entity_adoption.statistics_ages([e["entity_id"] for e in adoptable])
    except Exception:
        ages = {}
    for bucket in ("foreign", "orphaned"):
        for ent in result[bucket]:
            ent["statistics"] = ages.get(ent["entity_id"], {"has_statistics": False})

    return result


@router.get("/ha/conflicts")
async def ha_conflicts():
    """Other integrations polling the same FranklinWH cloud account. Read-only."""
    from src.services import integration_conflicts

    return await integration_conflicts.detect()


class DisableConflictRequest(BaseModel):
    domain: str
    # Typed, not a checkbox. Disabling an integration this add-on does not own
    # must be a deliberate act, and must not be reachable by a stray POST.
    confirm: str = ""


@router.post("/ha/conflicts/disable")
async def ha_conflicts_disable(req: DisableConflictRequest):
    """Disable a competing FranklinWH integration, at the user's explicit request.

    Never called automatically. Disables rather than deletes, so it can be put
    back from Home Assistant's own Integrations page.
    """
    from src.services import integration_conflicts

    if req.confirm.strip().lower() != "disable":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required — this disables an integration outside this add-on.",
        )

    result = await integration_conflicts.disable(req.domain)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Could not disable it.")
    return result


@router.get("/ha/instances")
async def get_ha_instances():
    """Return all configured Home Assistant instances, with tokens masked."""
    instances = await db.get_ha_instances()
    for inst in instances:
        if inst.get("token"):
            inst["token"] = "********"
    return {"ok": True, "instances": instances}


@router.post("/ha/instances")
async def upsert_ha_instance(req: HAInstanceUpsert):
    """
    Save or update a Home Assistant instance.
    Includes a live ping check against the server before saving to verify token/host.
    """
    token = req.token
    existing = None
    if req.id:
        existing = await db.get_ha_instance(req.id)
    
    # If token is not provided or masked, try to retrieve from db
    if not token or token == "********" or token.startswith("masked_"):
        if existing:
            token = existing.get("token")
        else:
            raise HTTPException(status_code=400, detail="Token is required for a new instance")
    
    if not token:
        raise HTTPException(status_code=400, detail="Token cannot be empty")

    # Perform reachability check
    url = f"{req.host.rstrip('/')}/api/"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            ha_name = data.get("location_name", "Home Assistant")
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Connection validation failed: {exc}"
        )
    
    # Upsert to database
    inst = {
        "id": req.id,
        "alias": req.alias,
        "host": req.host.rstrip("/"),
        "token": token,
        "enabled": int(req.enabled),
        "is_default": int(req.is_default)
    }
    await db.upsert_ha_instance(inst)
    return {
        "ok": True,
        "message": f"Successfully validated and saved instance '{req.alias}' (connected to '{ha_name}')"
    }


@router.delete("/ha/instances/{instance_id}")
async def delete_ha_instance(instance_id: str):
    """Delete a Home Assistant instance, cascading to associated devices."""
    await db.delete_ha_instance(instance_id)
    return {"ok": True, "message": "Instance deleted successfully"}


@router.get("/ha/devices")
async def get_notification_devices():
    """List all configured notification devices."""
    devices = await db.get_notification_devices()
    return {"ok": True, "devices": devices}


@router.post("/ha/devices")
async def upsert_notification_device(req: NotificationDeviceUpsert):
    """Add or update a notification device mapping."""
    inst = await db.get_ha_instance(req.ha_instance_id)
    if not inst:
        raise HTTPException(status_code=400, detail="Target Home Assistant instance does not exist")
    
    if req.owner_username:
        owner = await db.get_user(req.owner_username)
        if not owner:
            raise HTTPException(status_code=400, detail=f"Owner user '{req.owner_username}' does not exist")

    dev = {
        "id": req.id,
        "ha_instance_id": req.ha_instance_id,
        "alias": req.alias,
        "service_target": req.service_target,
        "enabled": int(req.enabled),
        "owner_username": req.owner_username
    }
    await db.upsert_notification_device(dev)
    return {"ok": True, "message": "Device configuration saved successfully"}


@router.delete("/ha/devices/{device_id}")
async def delete_notification_device(device_id: str):
    """Delete a notification device mapping."""
    await db.delete_notification_device(device_id)
    return {"ok": True, "message": "Device mapping removed successfully"}


@router.get("/ha/instances/{instance_id}/targets")
async def get_instance_targets(instance_id: str):
    """Query live notification endpoints (targets) from the specific HA instance."""
    inst = await db.get_ha_instance(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Home Assistant instance not found")
    
    from src.services.ha_autoconfig import resolve_instance_token

    host = inst.get("host", "")
    token = resolve_instance_token(inst)
    if not host or not token:
        return {"ok": False, "targets": [], "error": "Instance is not fully configured"}
    
    url = f"{host.rstrip('/')}/api/services"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "targets": [], "error": f"HA returned {resp.status_code}: {resp.text[:200]}"}

        services_list = resp.json()
        targets = []
        for domain_obj in services_list:
            if domain_obj.get("domain") != "notify":
                continue
            for svc_name, svc_meta in domain_obj.get("services", {}).items():
                label = svc_meta.get("name") or svc_name.replace("_", " ").title()
                targets.append({"service": svc_name, "label": label})

        # Sort: mobile_app_* first, then everything else
        targets.sort(key=lambda t: (0 if t["service"].startswith("mobile_app") else 1, t["service"]))
        return {"ok": True, "targets": targets, "error": None}

    except Exception as exc:
        logger.error(f"Failed to discover notify targets for instance {instance_id}: {exc}")
        return {"ok": False, "targets": [], "error": str(exc)}


