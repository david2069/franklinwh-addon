"""Gateway API routes — Phase 5 (credentials, hot-registry, validate/start/stop)."""
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.services import db
from src.models.gateway import make_short_id
from src.services.bms_history import bms_history_manager
from src.app_state import get_app_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gateways"])

# ── Cloud Discovery 60-second TTL cache ───────────────────────
_discovery_cache: dict = {"ts": 0.0, "payload": None}
_DISCOVERY_CACHE_TTL = 60  # seconds


def _get_registry():
    return get_app_state().get("registry")


# ── Request models ────────────────────────────────────────────

class GatewayAddRequest(BaseModel):
    full_serial: str
    name: str = ""
    model: str = ""
    site_id: str = ""
    # Discovery returns both; only the id was ever kept, so the dashboard could
    # show nothing but "Site 3447" above "SITE ID: 3447".
    site_name: str = ""
    site_address: str = ""
    email: str = ""
    password: str = ""
    poll_interval: int = 30


class GatewayUpdateRequest(BaseModel):
    name: str | None = None
    model: str | None = None
    email: str | None = None
    password: str | None = None
    enabled: bool | None = None
    poll_interval: int | None = None  # seconds, 10–300; applied live without restart

class BmsSessionSaveRequest(BaseModel):
    battery_sn: str
    session_name: str
    data: dict | None = None


class NetworkConfigResponse(BaseModel):
    """Read-only view of gateway network connectivity state (Batch K).

    All fields best-effort — any may be null if the upstream cloud/gateway
    hasn't reported the field yet or the poll loop is stalled. Consumers
    should treat null as "unknown", not "misconfigured".
    """
    short_id: str
    active_interface: str | None = None
    wifi_signal: int | None = None
    mobile_signal: int | None = None
    network_connection_code: int | None = None
    api_connection_status: str | None = None
    grid_connection_state: str | None = None
    grid_frequency: float | None = None
    relays: dict[str, int | bool | None] = {}
    poll_status: str | None = None
    last_poll_at: float | None = None
    stale_polls_dropped: int | None = None


class NetworkConfigUpdate(BaseModel):
    """Placeholder for future writable network config. All fields optional
    because they don't work yet — see PUT handler for the 501 response
    shape. Kept here so the OpenAPI schema documents the intended future
    surface for API clients / dashboards / HA blueprints.
    """
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    ethernet_mode: str | None = None
    ethernet_static_ip: str | None = None
    ethernet_static_netmask: str | None = None
    ethernet_static_gateway: str | None = None
    ethernet_static_dns: list[str] | None = None
    cellular_apn: str | None = None

class BmsRecordTaskRequest(BaseModel):
    battery_sn: str
    count: int = 20
    interval: int = 30


class SystemOnboardRequest(BaseModel):
    email: str
    password: str

@router.post("/system/onboard")
async def system_onboard(req: SystemOnboardRequest):
    from src.services.integration_manager import IntegrationManager
    registry = _get_registry()
    mgr = IntegrationManager(registry)
    result = await mgr.discover_and_onboard(req.email, req.password)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown Onboarding Error"))
    return result


# ── CRUD endpoints ────────────────────────────────────────────

async def _hydrate_gateway_fields(gw: dict, registry) -> dict:
    full_serial = gw.get("full_serial", "")
    gw["has_credentials"] = await db.has_credentials(full_serial)
    
    # Hydrate site_name and topology flags from profile_json (populated at gateway registration
    # via discover(tier=3) — these are authoritative cached static discovery fields).
    try:
        profile = json.loads(gw.get("profile_json") or "{}")
        # The gateways.site_name COLUMN wins. Two stores hold this — the column
        # (schema v61, written at registration and by the startup backfill) and
        # profile_json (written by discover(tier=3)) — and this line used to
        # overwrite the column with the profile unconditionally. On an install
        # whose profile predates site capture, that replaced a good name with
        # "", so the dashboard fell back to "Site 3447" while the address
        # beside it, read straight from the column, displayed correctly.
        gw["site_name"]          = (gw.get("site_name") or "").strip() or profile.get("site_name", "")
        # Captured at discovery from the cloud's zoneInfo since the beginning,
        # and never surfaced. It matters more than most metadata here: TOU
        # blocks are local wall-clock, so reading a schedule without knowing
        # which local is guesswork — and this project has already shipped four
        # separate UTC/local mix-ups.
        gw["timezone"]           = profile.get("timezone", "")
        # Device topology flags (DEF-HAS-SOLAR: use cached discover flag, NOT solar_kw > 0)
        gw["has_solar"]          = profile.get("has_solar", False)
        gw["has_smart_circuits"] = profile.get("has_smart_circuits", False)
        gw["has_generator"]      = profile.get("has_generator", False)
        gw["has_apbox"]          = profile.get("has_apbox", False)
        gw["has_ahub"]           = profile.get("has_ahub", False)
        # Hardware identity
        gw["sku"]                = profile.get("sku", "")
        gw["hw_version"]         = profile.get("hw_version")
        gw["firmware"]           = profile.get("firmware", "")
        gw["solar_detail"]       = profile.get("solar_detail", "")
        gw["ac_type"]            = profile.get("ac_type") or profile.get("grid_type") or "Unknown"
    except Exception:
        # Unparseable profile_json must not erase a name the column already has.
        gw["site_name"] = (gw.get("site_name") or "").strip()
        gw["ac_type"] = "Unknown"
    gw.pop("profile_json", None)

    WORK_MODE_LABELS = {
        1: "Time of Use",
        2: "Self-Consumption",
        3: "Emergency Backup",
        "Time-of-Use": "Time of Use",
        "Time of Use": "Time of Use",
        "Self-Consumption": "Self-Consumption",
        "Emergency Backup": "Emergency Backup",
    }

    if registry:
        live = registry.get_status(gw["short_id"])
        if live:
            gw["poll_status"] = live.get("poll_status", "unknown")
            gw["last_poll_age_s"] = live.get("last_poll_age_s")
            gw["last_error"] = live.get("last_error")
            last_data = live.get("last_data") or {}
            gw["battery_soc"] = last_data.get("battery_soc")
            gw["battery_kw"] = last_data.get("battery_kw")
            gw["home_kw"] = last_data.get("home_kw")
            gw["solar_kw"] = last_data.get("solar_kw")
            gw["grid_kw"] = last_data.get("grid_kw")
            # Decode operating_mode integer → label (fallback to raw value)
            raw_mode = last_data.get("operating_mode")
            gw["operating_mode"] = WORK_MODE_LABELS.get(raw_mode, raw_mode)
            # operating_mode_id must be the INTEGER (1/2/3) for JS activeMode matcher
            gw["operating_mode_id"] = last_data.get("operating_mode_id") or last_data.get("work_mode") or last_data.get("mode", {}).get("work_mode")

            gw["wifi_connected"] = last_data.get("wifi_connected")
            gw["grid_connected"] = last_data.get("grid_connected")
            gw["grid_connection_state"] = last_data.get("grid_connection_state")
            gw["network_type"] = last_data.get("network_type")
            # System Status sidebar fields
            gw["tou_active"] = last_data.get("tou_active", {})
            gw["tou_next"] = last_data.get("tou_next", {})
            gw["capacity"] = last_data.get("capacity", {})
            gw["backup_reserve_soc"] = last_data.get("backup_reserve_soc")
            # Phase C: SOC limit defaults — prefer in-memory context (fastest), fallback to DB
            svc_ref = registry.get_gateway(gw["short_id"])
            ctx = getattr(svc_ref, "context", {}) if svc_ref else {}
            gw["min_discharge_soc"] = ctx.get("min_discharge_soc", await db.get_config_value(f"gw_{gw['short_id']}_min_discharge_soc", 30))
            gw["max_charge_soc"]    = ctx.get("max_charge_soc",    await db.get_config_value(f"gw_{gw['short_id']}_max_charge_soc",    95))
            gw["battery_count"] = last_data.get("battery_count") or last_data.get("apower_count") or gw["capacity"].get("battery_count", 0)
            gw["apower_serial_numbers"] = last_data.get("apower_serial_numbers", [])
            gw["ambient_temp"] = last_data.get("agate", {}).get("ambient_temp") or last_data.get("ambient_temp")
            gw["battery_heater_state"] = last_data.get("battery", {}).get("heater_state") or last_data.get("battery_heater_state", False)
            
            # Derive exact accessory list from Cloud profile metadata, removing the legacy mock.
            accessories = []
            profile_acc = gw.get("profile", {}) if "profile" in gw else json.loads(gw.get("profile_json") or "{}")
            if isinstance(profile_acc, str):
                try: profile_acc = json.loads(profile_acc)
                except: profile_acc = {}
            
            if profile_acc.get("has_smart_circuits") or any(k.startswith("smart_circuit_") for k in last_data.keys()):
                accessories.append("smart_circuits")
            if profile_acc.get("has_generator") or last_data.get("generator_enabled"):
                accessories.append("generator")
            
            raw_acc = last_data.get("accessories", accessories)
            if gw.get("grid_type") == "off_grid":
                gw["accessories"] = [a for a in raw_acc if a != "mac1"]
            else:
                gw["accessories"] = raw_acc

            # Gateways tab table fields
            # Runtime mode / run status (for Run Status column)
            mode_d = last_data.get("mode", {})
            gw["runtime_mode"] = mode_d.get("runtime_mode") or last_data.get("runtime_mode")
            gw["work_mode_desc"] = mode_d.get("work_mode_desc") or last_data.get("work_mode_desc")
            gw["run_status_desc"] = last_data.get("run_status_desc")
            # Expose active_dispatch_name for the sidebar Schedule badge (Custom vs Running)
            gw["active_dispatch_name"] = last_data.get("status", {}).get("active_dispatch_name")
            # --- RAW diagnostic fields for VPP troubleshooting ---
            gw["_dbg_run_status"]    = last_data.get("run_status")       # int e.g. 2=Discharging
            gw["_dbg_tou_mode"]      = last_data.get("tou_mode")         # int from runtimeData.mode
            gw["_dbg_tou_mode_desc"] = last_data.get("tou_mode_desc")    # str from runtimeData.name
            gw["_dbg_work_mode"]     = last_data.get("work_mode")        # int 1/2/3
            gw["_dbg_device_status"] = last_data.get("device_status")    # int
            gw["_dbg_runtime_mode"]  = mode_d.get("runtime_mode")        # computed str
            gw["_dbg_effective_mode"] = last_data.get("effective_mode")   # new franklinwh-cloud field
            # Reserve SoCs — all three mode-specific fields exposed for dashboard SOC ring
            gw["self_reserve_soc"] = last_data.get("self_reserve_soc")
            gw["tou_reserve_soc"]  = last_data.get("tou_reserve_soc")
            # Connectivity / signal (from agate sub-dict, fallback to root)
            agate_d = last_data.get("agate", {})
            gw["wifi_signal"] = agate_d.get("wifi_signal") or last_data.get("wifi_signal")
            gw["mobile_signal"] = agate_d.get("mobile_signal") or last_data.get("mobile_signal")
            gw["network_connection"] = agate_d.get("network_connection") or last_data.get("network_connection")
            # Relay states (for Connectivity column row 2)
            power_d = last_data.get("power", {})
            relays = power_d.get("relays", {})
            gw["grid_relay1"] = relays.get("grid1") if relays else last_data.get("grid_relay1")
            gw["generator_relay"] = relays.get("generator") if relays else last_data.get("generator_relay")
            gw["solar_relay1"] = relays.get("solar1") if relays else last_data.get("solar_relay1")
    return gw


@router.get("/gateways")
async def list_gateways():
    """Return all registered gateways with live poll status and credential status merged in."""
    gateways = await db.get_all_gateways()
    registry = _get_registry()

    for gw in gateways:
        await _hydrate_gateway_fields(gw, registry)
    return gateways




@router.post("/gateways", status_code=201)
async def add_gateway(req: GatewayAddRequest):
    """Register a gateway, validate and store credentials, hot-start poll loop."""
    if not req.email or not req.password:
        raise HTTPException(status_code=422, detail="Email and password are required")
    if not req.full_serial or len(req.full_serial) < 8:
        raise HTTPException(status_code=422, detail="Full serial must be at least 8 characters")
    short_id = make_short_id(req.full_serial)
    full_serial = req.full_serial.upper()
    # Validate credentials against Cloud API before saving
    from src.routes.api_gateways import _discover_gateways
    val = await _discover_gateways(req.email, req.password)
    validated_at = None
    profile = {}
    if val.get("ok"):
        validated_at = __import__('datetime').datetime.utcnow().isoformat()
        await db.audit_credential(full_serial, "validated", "ui", "Credentials validated on registration")
        
        # Formal Phase A Discovery extraction (Zero Hardcoding mandate)
        try:
            from franklinwh_cloud import FranklinWHCloud
            import dataclasses
            fwh = FranklinWHCloud(email=req.email, password=req.password)
            await fwh.login()
            await fwh.select_gateway(full_serial)
            raw_snapshot = await fwh.discover(tier=3)
            snapshot = dataclasses.asdict(raw_snapshot) if dataclasses.is_dataclass(raw_snapshot) else raw_snapshot
            agate_info = snapshot.get("agate", {})
            profile = {
                "has_solar": snapshot.get("flags", {}).get("solar", False),
                "has_smart_circuits": snapshot.get("accessories", {}).get("has_smart_circuits", False),
                "has_generator": snapshot.get("accessories", {}).get("has_generator", False),
                "has_apbox": snapshot.get("accessories", {}).get("has_apbox", False),
                "sku": agate_info.get("sku", ""),
                "firmware": agate_info.get("firmware", ""),
                "model_name": req.model or agate_info.get("model_name", "aGate"),
                # Phase 117: hydrate site identity so publisher can build shared site HA device
                "site_name": getattr(req, "site_name", "") or "",
                "site_id":   req.site_id or "",
            }
            
            # Immediately hydrate the known batteries from Cloud discovery
            from src.services.db import upsert_battery
            battery_units = snapshot.get("batteries", {}).get("units", [])
            for i, b in enumerate(battery_units):
                sn = b.get("serial")
                if sn:
                    kw = b.get("rated_power_kw", 5.0)
                    kwh = b.get("rated_capacity_kwh", 13.6)
                    # We are using await here because this is an async route
                    await upsert_battery(sn[-8:], sn, short_id, kw, kwh, i + 1)

        except Exception as e:
            logger.warning(f"Failed to isolate dynamic cloud metadata during manual UI registration: {e}")
            
    else:
        await db.audit_credential(full_serial, "failed", "ui", val.get("error", "Unknown"))
        raise HTTPException(status_code=401, detail=f"Credential validation failed: {val.get('error', 'Unknown error')}")
    
    # Store the exact gateway topology extracted from the cloud.
    await db.upsert_gateway(
        short_id=short_id,
        full_serial=full_serial,
        name=req.name,
        site_id=req.site_id,
        site_name=req.site_name,
        site_address=req.site_address,
        model=profile.get("model_name") or req.model,
        profile=profile,
        credentials={},   # no longer stored in gateway row
    )
    await db.upsert_credentials(full_serial, req.email, req.password, source="ui", validated_at=validated_at)
    registry = _get_registry()
    if registry:
        await registry.start_gateway(short_id)

    # Greenfield Boot Recovery: Hot-Start Suspended Services 
    state = get_app_state()
    if state.get("setup_required", False):
        logger.info("Greenfield Onboarding Complete: Awaking suspended background services...")
        if state.get("publisher"):
            state["publisher"].start()
        if state.get("listener"):
            state["listener"].start()
            
        from src.services.bms_history import bms_history_manager
        bms_history_manager.start()
        
        if state.get("scheduler"):
            state["scheduler"].start()
            
        state["setup_required"] = False
        logger.info("Suspended services successfully hot-booted!")

    # Coordinates drive the solar forecast, and were only obtainable by pressing
    # a button that called a method the cloud client does not have — so a fresh
    # install had no location and therefore no forecast, with nothing saying why.
    # Best-effort: a gateway that registers is more useful than one that fails
    # registration because Home Assistant had no latitude set.
    try:
        from src.services import site_location

        existing = await db.get_config_value(f"lat_{short_id}", "")
        if not existing:
            loc = await site_location.resolve(registry.get_gateway(short_id))
            if loc:
                await site_location.propagate(loc["lat"], loc["lng"], short_id=short_id)
                logger.info(
                    f"Location for {short_id} resolved from {loc['source']}: "
                    f"{loc['lat']}, {loc['lng']}"
                )
            else:
                logger.info(
                    f"No location for {short_id} — set it on the Solar Setup tab, "
                    "or set Home Assistant's own latitude/longitude."
                )
    except Exception:
        logger.warning("Could not resolve site location at registration", exc_info=True)

    logger.info(f"Gateway registered + credentials saved: {short_id} ({full_serial})")
    return {"short_id": short_id, "full_serial": full_serial, "name": req.name}

@router.post("/gateways/{short_id}/bms/trigger")
async def trigger_bms_fetch(short_id: str):
    """Force an immediate downstream poll of the aPower BMS 16-cell variables."""
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")
    gw = registry.get_gateway(short_id)
    if not gw or not gw._client:
        raise HTTPException(status_code=404, detail="Gateway offline or client absent")
    
    try:
        apowers = gw.status.last_data.get("apower_serial_numbers", [])
        if isinstance(apowers, str):
            apowers = [x.strip() for x in apowers.split(",") if x.strip()]
            
        from src.services.db import upsert_battery
        
        bms_dict = {}
        for i, sn in enumerate(apowers):
            if not sn: continue
            
            try:
                await upsert_battery(sn[-8:], sn, short_id, 5.0, 13.6, i + 1)
            except Exception as db_ex:
                logger.warning(f"Failed to store battery {sn} in DB during explicit fetch: {db_ex}")
            try:
                b_info = await gw._client.get_bms_info(sn)
                bms_dict[sn] = b_info
            except Exception as e:
                logger.warning(f"Failed to proxy BMS MQTT trace for {sn}: {e}")
        
        # Normalise the raw BMS dict → bms_units list (same structure as _normalise_stats)
        from src.services.gateway_service import GatewayService as _GWS
        bms_units = _GWS._normalise_bms_raw(bms_dict)

        # Flush into last_data immediately so any concurrent load() returns fresh units
        # before the background _poll_once task completes its full stats cycle.
        if gw.status.last_data and isinstance(gw.status.last_data, dict):
            gw.status.last_data["bms_units"] = bms_units

        gw._client_cache["bms"] = bms_dict

        # Immediately invoke normalise to guarantee the cache structure is flushed forward to frontend on UI race conditions
        import asyncio
        asyncio.create_task(gw._poll_once())

        return {"ok": True, "bms_units": bms_units}
    except Exception as e:
        logger.error(f"Failed to orchestrate explicit BMS polling loop: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gateways/{short_id}/bms/history")
async def get_bms_history(short_id: str):
    """Return up to 100 recent live BMS cell voltage/temp snapshots for charting."""
    return bms_history_manager.get_history(short_id)


# ── Persistent BMS Chart Sessions ──────────────────────────────

@router.post("/gateways/{short_id}/bms/record_task")
async def start_bms_recording_daemon(short_id: str, req: BmsRecordTaskRequest):
    """Fire-and-forget recording orchestration to prevent frontend suspension loss."""
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")
    gw = registry.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway offline")
        
    import asyncio
    asyncio.create_task(gw.schedule_bms_recording(req.battery_sn, req.count, req.interval))
    return {"ok": True, "detail": "Daemon instantiated"}

@router.post("/gateways/{short_id}/bms/sessions")
async def save_bms_session_route(short_id: str, req: BmsSessionSaveRequest):
    """Save the dynamic RAM buffer of a specific aPower unit into the SQLite persistence layer."""
    payload = req.data
    if not payload:
        history = bms_history_manager.get_history(short_id)
        payload = history.get(req.battery_sn, {})
        
    if not payload or not payload.get("times"):
        raise HTTPException(status_code=400, detail="Buffer is null or empty. Chart contains no data points.")
    
    sid = await db.save_bms_session(short_id, req.battery_sn, req.session_name, json.dumps(payload))
    return {"ok": True, "session_id": sid}

@router.get("/gateways/{short_id}/bms/sessions")
async def list_bms_sessions_route(short_id: str, battery_sn: str = None):
    """Return a lightweight list of recorded chart sessions."""
    return await db.get_bms_sessions(short_id, battery_sn)

@router.get("/gateways/bms/sessions/{session_id}")
async def load_bms_session_route(session_id: str):
    """Retrieve the explicit JSON payload matrix for chart instantiation."""
    data = await db.get_bms_session_data(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        data["payload"] = json.loads(data["data_json"])
    except Exception:
        data["payload"] = {}
    data.pop("data_json", None)
    return data

@router.delete("/gateways/bms/sessions/{session_id}")
async def delete_bms_session_route(session_id: str):
    ok = await db.delete_bms_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.get("/gateways/{short_id}")
async def get_gateway(short_id: str):
    """Return full detail for one gateway (no credentials)."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    batteries = await db.get_batteries_for_gateway(short_id)
    gw["batteries"] = batteries
    gw.pop("credentials_json", None)
    registry = _get_registry()
    await _hydrate_gateway_fields(gw, registry)
    return gw


@router.patch("/gateways/{short_id}", status_code=200)
async def update_gateway(short_id: str, req: GatewayUpdateRequest):
    """Update gateway name/model/credentials. Validates + saves credentials to new table."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    full_serial = gw["full_serial"]
    creds_changed = bool(req.email and req.password)
    
    new_enabled = req.enabled if req.enabled is not None else gw.get("enabled", 1)
    
    # We must preserve the existing profile metadata explicitly so it's not destroyed
    profile_raw = gw.get("profile_json") or "{}"
    try:
        profile = json.loads(profile_raw)
    except Exception:
        profile = {}

    await db.upsert_gateway(
        short_id=short_id,
        full_serial=full_serial,
        name=req.name if req.name is not None else gw["name"],
        model=req.model if req.model is not None else gw["model"],
        profile=profile,
        credentials={},
        enabled=new_enabled
    )
    if creds_changed:
        # Validate before saving
        from src.routes.api_gateways import _discover_gateways
        val = await _discover_gateways(req.email, req.password)
        if val.get("ok"):
            validated_at = __import__('datetime').datetime.utcnow().isoformat()
            await db.upsert_credentials(full_serial, req.email, req.password, source="ui", validated_at=validated_at)
            logger.info(f"[{full_serial}] Credentials updated and validated")
        else:
            await db.audit_credential(full_serial, "failed", "ui", val.get("error", ""))
            raise HTTPException(status_code=401, detail=f"Credential validation failed: {val.get('error', 'Unknown error')}")

    # Apply poll_interval live (no restart required — takes effect on next poll sleep cycle)
    if req.poll_interval is not None:
        new_poll = max(10, min(300, req.poll_interval))  # enforce 10s–300s bounds
        profile["poll_interval"] = new_poll
        await db.upsert_gateway(
            short_id=short_id,
            full_serial=full_serial,
            name=req.name if req.name is not None else gw["name"],
            model=req.model if req.model is not None else gw["model"],
            profile=profile,
            credentials={},
            enabled=new_enabled,
        )
        # Patch live service immediately — no restart needed
        registry_ref = _get_registry()
        if registry_ref:
            live_gw = registry_ref.get_gateway(short_id)
            if live_gw:
                live_gw._poll_interval = new_poll
                logger.info(f"[{short_id}] poll_interval updated live → {new_poll}s")

            
    registry = _get_registry()
    if registry:
        if req.enabled is False and gw.get("enabled", 1):
            await registry.stop_gateway(short_id)
            logger.info(f"[{full_serial}] Opt-in retracted. Gateway listener stopped.")
        elif req.enabled is True and not gw.get("enabled", 1):
            await registry.start_gateway(short_id)
            logger.info(f"[{full_serial}] Opt-in granted. Gateway listener started.")
        elif creds_changed and new_enabled:
            await registry.restart_gateway(short_id)
            logger.info(f"[{full_serial}] Poll loop restarted after credential update")
            
    return {"ok": True, "short_id": short_id}


@router.delete("/gateways/{short_id}", status_code=204)
async def remove_gateway(short_id: str):
    """Stop poll loop and remove gateway from DB."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    registry = _get_registry()
    if registry:
        await registry.stop_gateway(short_id)
    await db.delete_gateway(short_id)
    logger.info(f"Gateway removed: {short_id}")


# ── Poll lifecycle control ────────────────────────────────────

@router.post("/gateways/{short_id}/start", status_code=200)
async def start_gateway_poll(short_id: str):
    """Start (or restart) the poll loop for a gateway."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    await registry.restart_gateway(short_id)
    return {"ok": True, "short_id": short_id, "action": "started"}


@router.post("/gateways/{short_id}/stop", status_code=200)
async def stop_gateway_poll(short_id: str):
    """Stop the poll loop without removing from DB."""
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    stopped = await registry.stop_gateway(short_id)
    if not stopped:
        return {"ok": False, "short_id": short_id, "action": "was_not_running"}
    return {"ok": True, "short_id": short_id, "action": "stopped"}


@router.post("/gateways/{short_id}/poll", status_code=200)
async def force_poll_gateway(short_id: str):
    """Trigger an immediate out-of-cycle poll for a single gateway.
    Calls _poll_once() on the running GatewayService and returns the refreshed snapshot."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    svc = registry.get_gateway(short_id)
    if not svc:
        raise HTTPException(status_code=409, detail=f"Gateway {short_id!r} is not running — start it first")
    try:
        data = await svc._poll_once()
        return {"ok": True, "short_id": short_id, "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Status ────────────────────────────────────────────────────

@router.get("/gateways/{short_id}/status")
async def gateway_status(short_id: str):
    """Live poll status from the running GatewayService."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    registry = _get_registry()
    if registry:
        live = registry.get_status(short_id)
        if live:
            return live
    return {
        "short_id": short_id,
        "poll_status": "not_started",
        "last_seen": gw.get("last_seen"),
        "mqtt_published": False,
    }


@router.get("/gateways/{short_id}/data")
async def gateway_last_data(short_id: str):
    """Return the full normalised last_data dict for a gateway (includes bms_units, tou_active, etc.).

    Always injects apower_units from profile_json (populated at startup from get_apower_info).
    This ensures firmware data is available without requiring a BMS trigger.
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    registry = _get_registry()
    data = {}
    if registry:
        svc = registry.get_gateway(short_id)
        if svc and svc.status.last_data:
            data = dict(svc.status.last_data)
            # Inject apower_info from client cache if not already in last_data
            if "apower_info" not in data and svc._client_cache.get("apower_info"):
                data["apower_info"] = svc._client_cache["apower_info"]
    # Always inject apower_units from profile_json (Tier-A static firmware data)
    # This makes firmware visible immediately on page load — no BMS trigger required.
    try:
        profile = json.loads(gw.get("profile_json") or "{}") if gw.get("profile_json") else {}
        apower_units = profile.get("apower_units")
        if apower_units:
            data["apower_units_profile"] = apower_units
    except Exception:
        pass
    return data


# ── Network (Batch K, 2026-07-18) ─────────────────────────────────────────
# Consolidates gateway connectivity fields under a single dedicated route
# so API clients / HA blueprints / dashboards don't have to string-scrape
# the general /api/gateways/{id} response and don't have to know which
# fields live under `agate.*` vs the root of last_data.
#
# Read is fully wired; write is a documented 501 stub because upstream
# franklinwh_cloud doesn't expose network-config write operations. When
# a direct-to-aGate HTTP interface is integrated we'll fill in the PUT
# body without breaking the response contract.


# Franklin cloud's network_connection integer code → human label.
# Kept next to the route so the mapping evolves atomically with the API.
_NETWORK_CONNECTION_CODE_MAP: dict[int, str] = {
    0: "Offline",
    1: "Ethernet",
    2: "WiFi",
    3: "WiFi",
    4: "4G Mobile",
    5: "5G Mobile",
    9: "VPP",
}


@router.get("/gateways/{short_id}/network", response_model=NetworkConfigResponse)
async def get_gateway_network(short_id: str):
    """Read the gateway's current network connectivity state.

    Best-effort: null fields mean "not reported by the upstream cloud/
    gateway yet" — treat as unknown, not misconfigured.
    """
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")

    registry = _get_registry()
    ld: dict = {}
    poll_status = None
    last_poll_at = None
    if registry:
        svc = registry.get_gateway(short_id)
        if svc:
            if svc.status.last_data:
                ld = dict(svc.status.last_data)
            poll_status = getattr(svc.status, "poll_status", None)
            last_poll_at = getattr(svc.status, "last_poll_at", None)

    agate = ld.get("agate", {}) or {}
    power = ld.get("power", {}) or {}
    relays_raw = power.get("relays", {}) or {}

    # Prefer the decoded string from agate.network_connection; fall back
    # to decoding the root integer via _NETWORK_CONNECTION_CODE_MAP.
    active_interface = agate.get("network_connection")
    conn_code = ld.get("network_connection")
    conn_code_int: int | None = None
    if isinstance(conn_code, (int, float)):
        conn_code_int = int(conn_code)
    elif isinstance(conn_code, str) and conn_code.isdigit():
        conn_code_int = int(conn_code)
    if not active_interface and conn_code_int is not None:
        active_interface = _NETWORK_CONNECTION_CODE_MAP.get(conn_code_int)

    # Relays consolidated shape — matches _hydrate_gateway_fields but
    # normalises to bool | int where possible.
    def _cast(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return int(v)
        return v

    # Prefer power.relays value when present (even if 0 — a "0 = off"
    # relay is distinct from "unknown"). Fall back to root fields only
    # when the power-dict value is None/absent — `or` chains would
    # incorrectly overwrite a legit 0 with a truthy root fallback.
    def _pick(primary_key: str, fallback_key: str):
        primary = relays_raw.get(primary_key)
        return _cast(primary if primary is not None else ld.get(fallback_key))

    relays = {
        "grid1":       _pick("grid1",       "grid_relay1"),
        "grid2":       _pick("grid2",       "grid_relay2"),
        "solar1":      _pick("solar1",      "solar_relay1"),
        "solar2":      _pick("solar2",      "pv_relay2"),
        "generator":   _pick("generator",   "generator_relay"),
        "black_start": _pick("blackStart",  "black_start_relay"),
        "apbox":       _pick("apbox",       "bfpv_apbox_relay"),
    }

    return NetworkConfigResponse(
        short_id=short_id,
        active_interface=active_interface,
        wifi_signal=agate.get("wifi_signal") or ld.get("wifi_signal"),
        mobile_signal=agate.get("mobile_signal") or ld.get("mobile_signal"),
        network_connection_code=conn_code_int,
        api_connection_status=ld.get("api_connection_status"),
        grid_connection_state=ld.get("grid_connection_state"),
        grid_frequency=ld.get("grid_frequency"),
        relays=relays,
        poll_status=poll_status,
        last_poll_at=last_poll_at,
        stale_polls_dropped=ld.get("stale_polls_dropped"),
    )


@router.put("/gateways/{short_id}/network", status_code=501)
async def update_gateway_network(short_id: str, req: NetworkConfigUpdate):
    """Write network config — NOT YET SUPPORTED.

    The upstream FranklinWH cloud API does not expose network-settings
    writes; support requires a direct-to-aGate HTTP interface that FHAI
    doesn't currently talk to. Returns 501 with a documented error shape
    so API clients can render the "not supported" state cleanly today
    and swap to real behaviour when the capability lands.

    Kept as a real route (not just OpenAPI docs) so callers can trial
    against the API and get a stable 501 answer without silent 404s.
    """
    # Verify gateway exists so we don't 501 on a 404-worthy path
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")

    provided = {k: v for k, v in req.model_dump(exclude_none=True).items()}
    raise HTTPException(
        status_code=501,
        detail={
            "message": "Network configuration writes are not supported yet.",
            "reason": (
                "FranklinWH cloud API does not expose network-settings write; "
                "support requires a direct-to-aGate HTTP interface which is "
                "not yet integrated into FHAI. Track upstream franklinwh_cloud "
                "capability + local aGate discovery for future implementation."
            ),
            "supported_fields_now": [],
            "fields_provided": list(provided.keys()),
            "future_capability_schema": [
                "wifi_ssid", "wifi_password",
                "ethernet_mode", "ethernet_static_ip", "ethernet_static_netmask",
                "ethernet_static_gateway", "ethernet_static_dns",
                "cellular_apn",
            ],
        },
    )


@router.get("/gateways/{short_id}/dispatch/status")
async def get_dispatch_status(short_id: str):
    """Return live dispatch status including elapsed/remaining seconds for countdown display.

    Used by the frontend to drive the Dispatch Status countdown timer.
    Returns immediately (no cloud API call) — reads in-memory state from cloud_dispatch.
    """
    registry = _get_registry()
    if not registry:
        return {"active": False, "elapsed_s": 0, "remaining_s": 0, "state": "Standby"}
    svc = registry.get_gateway(short_id)
    if not svc:
        return {"active": False, "elapsed_s": 0, "remaining_s": 0, "state": "Standby"}
    try:
        last_data = svc.status.last_data if svc.status else None
        dispatch_status = await svc.cloud_dispatch.status(last_data=last_data)
        # Inject live battery SOC from last_data so Dispatch Status card shows current_soc
        if last_data and "current_soc" not in dispatch_status:
            soc_val = last_data.get("battery_soc")
            if soc_val is not None:
                dispatch_status["current_soc"] = round(float(soc_val), 1)
        return dispatch_status
    except Exception as exc:
        logger.warning(f"[{short_id}] dispatch/status error: {exc}")
        return {"active": False, "elapsed_s": 0, "remaining_s": 0, "state": "Standby", "error": str(exc)}



_live_power_history = {}  # { short_id: [timestamps] }
LIVE_POWER_LIMIT = 20     # Max requests
LIVE_POWER_WINDOW = 300   # per 5 minutes

@router.get("/gateways/{short_id}/telemetry/live_power")
async def get_gateway_live_power(short_id: str, request: Request):
    """Isolated real-time fetch of electrical properties without polluting the background stats loop. Rate limited."""
    
    # ── Rate Limiter ──
    now = time.time()
    history = _live_power_history.get(short_id, [])
    history = [ts for ts in history if now - ts < LIVE_POWER_WINDOW]
    if len(history) >= LIVE_POWER_LIMIT:
        _live_power_history[short_id] = history
        raise HTTPException(status_code=429, detail="Live telemetry rate limit exceeded. Please wait before polling again.")
    history.append(now)
    _live_power_history[short_id] = history
    
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    gw = registry.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway not found")
        
    client = await gw._get_or_create_client()
    try:
        power = await client.get_power_info()
        return {
            "grid_voltage1": power.get("gridVol1"),
            "grid_voltage2": power.get("gridVol2"),
            "grid_line_voltage": power.get("gridLineVol"),
            "grid_current1": power.get("gridCurr1"),
            "grid_current2": power.get("gridCurr2"),
            "gridFreq": power.get("gridFreq"),
            "relays": {
                "grid1": power.get("gridRelayStat"),
                "grid2": power.get("gridRelay2"),
                "generator": power.get("oilRelayStat"),
                "solar1": power.get("solarRelayStat"),
                "blackStart": power.get("blackStartRelay"),
                "pv2": power.get("pvRelay2"),
                "apbox": power.get("BFPVApboxRelay"),
            }
        }
    except json.JSONDecodeError as e:
        logger.error(f"[{short_id}] Malformed MQTT response (JSONDecodeError) in live_power: {e}")
        # Return a partial object or raise a more specific exception that the UI can handle
        # Instead of 500, we could return 503 or 504. 
        # But the requirement says "preventing server-side 500 crashes" and "gracefully catch and report".
        raise HTTPException(
            status_code=502, 
            detail="Cloud returned malformed data. The gateway may be slow to respond or offline."
        )
    except Exception as e:
        logger.error(f"[{short_id}] Failed to fetch live power: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch live power metrics: {str(e)}")

@router.get("/gateways/{short_id}/compliance/{request_type}")
async def gateway_grid_compliance(short_id: str, request_type: int):
    """Proxy the Grid Compliance payload from the FranklinWH Cloud API."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not initialised")
        
    svc = registry.get_gateway(short_id)
    if not svc or not getattr(svc, "_client", None):
        raise HTTPException(status_code=503, detail="Gateway service offline or cloud client absent")
        
    try:
        data = await svc._client.get_grid_profile_info(requestType=request_type)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch grid compliance (req={request_type}) for {short_id}: {e}")
        raise HTTPException(status_code=500, detail="Grid Compliance API trace failed")



@router.get("/gateways/status/all")
async def all_gateway_status():
    """Live poll status for all registered gateways."""
    registry = _get_registry()
    if registry:
        return registry.get_all_status()
    return []


# ── Credential validation + gateway discovery ─────────────────

@router.post("/gateways/{short_id}/validate", status_code=200)
async def validate_gateway_credentials(short_id: str):
    """Test stored Cloud API credentials for this gateway."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    creds = json.loads(gw.get("credentials_json") or "{}")
    email = creds.get("email", "")
    password = creds.get("password", "")
    if not email:
        return {"ok": False, "error": "No credentials stored. Save credentials first."}
    result = await _discover_gateways(email, password)
    if result["ok"]:
        # Check if this gateway's serial is still in the account
        serials = [g.get("serial", "") for g in result.get("gateways", [])]
        if gw["full_serial"] not in serials and serials:
            result["warning"] = (
                f"Gateway {gw['full_serial']!r} is not in the discovered list "
                f"for this account. It may have been unlinked."
            )
    return result


@router.post("/gateways/validate_credentials", status_code=200)
async def validate_credentials_inline(body: dict):
    """Validate email+password and return discovered gateways (used in Add Gateway form)."""
    email = body.get("email", "")
    password = body.get("password", "")
    if not email or not password:
        return {"ok": False, "error": "Email and password required"}
    return await _discover_gateways(email, password)


@router.post("/gateways/discover", status_code=200)
async def discover_gateways(body: dict):
    """Discover all gateways linked to a FranklinWH account."""
    email = body.get("email", "")
    password = body.get("password", "")
    if not email or not password:
        return {"ok": False, "error": "Email and password required"}
    return await _discover_gateways(email, password)


@router.get("/gateways/discover/linked", status_code=200)
async def discover_linked_gateways():
    """Discover all gateways linked to the existing FranklinWH account credentials."""
    creds = await db.get_all_credentials()
    if not creds:
        return {"ok": False, "error": "No account credentials found. Please sign in."}
    
    # We enforce a single-account paradigm, so any credential row contains the universal account
    primary = creds[0]
    email = primary.get("email")
    password = primary.get("password")
    
    if not email or not password:
        return {"ok": False, "error": "Account credentials corrupted. Please re-authenticate."}
        
    return await _discover_gateways(email, password)


@router.get("/gateways/cloud/available", status_code=200)
async def get_available_gateways():
    """Return Cloud-discovered gateways that are NOT yet registered in the local DB.

    Uses stored account credentials (single-account paradigm).
    Results are cached for 60 seconds to avoid hammering the FranklinWH Cloud API
    on repeated modal open/close.

    Response shape:
      {
        "ok": true,
        "cached": false,
        "account_email": "u***@example.com",
        "total_available": 2,
        "sites": [
          {
            "site_id": "1234",
            "site_name": "My Home",
            "address": "...",
            "gateways": [
              {"serial": "...", "short_id": "...", "name": "...",
               "model": "...", "online": true}
            ]
          }
        ]
      }
    """
    global _discovery_cache

    # ── 1. Load stored credentials ────────────────────────────────
    creds = await db.get_all_credentials()
    if not creds:
        return {
            "ok": False,
            # A flag, not a sentence to string-match. The message also used to
            # point at a "setup wizard" that does not exist for credentials —
            # the only wizard is the security one — so it was a dead end
            # naming a door that was not there.
            "needs_credentials": True,
            "error": "Sign in to your FranklinWH account to discover gateways.",
            "sites": [],
            "total_available": 0,
        }
    primary = creds[0]
    email = primary.get("email", "")
    password = primary.get("password", "")
    if not email or not password:
        return {
            "ok": False,
            "error": "Account credentials are corrupted. Please re-authenticate via the wizard.",
            "sites": [],
            "total_available": 0,
        }

    # ── 2. Check 60-second TTL cache ─────────────────────────────
    now = time.monotonic()
    if _discovery_cache["payload"] is not None and (now - _discovery_cache["ts"]) < _DISCOVERY_CACHE_TTL:
        # Still re-filter against DB on cache hit (a gateway could have been added during the TTL)
        registered_short_ids = {gw["short_id"] for gw in await db.get_all_gateways()}
        logger.debug("get_available_gateways: serving from 60s cache")
        return _project_discovery(
            _discovery_cache["payload"], registered_short_ids, cached=True
        )

    # ── 3. Call Cloud API ─────────────────────────────────────────
    result = await _discover_gateways(email, password)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error", "Cloud discovery failed"),
            "sites": [],
            "total_available": 0,
        }

    # ── 4. Group by site ──────────────────────────────────────────
    all_gateways: list[dict] = result.get("gateways", [])
    sites_map: dict = {}
    for gw in all_gateways:
        sid = str(gw.get("site_id") or "ungrouped")
        if sid not in sites_map:
            sites_map[sid] = {
                "site_id": sid,
                "site_name": gw.get("site") or ("Ungrouped" if sid == "ungrouped" else f"Site {sid}"),
                "address": gw.get("address", ""),
                "gateways": [],
            }
        serial = gw.get("serial", "")
        sites_map[sid]["gateways"].append({
            "serial":   serial,
            "short_id": serial[-8:] if len(serial) >= 8 else serial,
            "name":     gw.get("name") or gw.get("serial", ""),
            "model":    gw.get("model", ""),
            "online":   gw.get("online", False),
        })

    full_sites = list(sites_map.values())

    # Cache the full (pre-filter) result
    _discovery_cache["ts"] = now
    _discovery_cache["payload"] = {
        "ok": True,
        "cached": False,
        "account_email": _mask_email(email),
        "account": result.get("account") or classify_account(None),
        "sites": full_sites,
        "total_available": len(all_gateways),
    }

    # ── 5. Filter out already-registered gateways ─────────────────
    registered_short_ids = {gw["short_id"] for gw in await db.get_all_gateways()}
    response = _project_discovery(
        _discovery_cache["payload"], registered_short_ids, cached=False
    )

    logger.info(
        f"get_available_gateways: {len(all_gateways)} discovered, "
        f"{len(all_gateways) - response['total_available']} already registered, "
        f"{response['total_available']} available"
    )

    return response


def _project_discovery(payload: dict, registered_short_ids: set, *, cached: bool) -> dict:
    """Filter already-registered gateways out of a cached discovery payload.

    Both the cache-hit and the fresh path return through here. They used to
    build their response dicts independently, which is how `account` shipped on
    one and not the other — the response shape now has a single definition.
    """
    out = dict(payload)
    out["cached"] = cached
    out["sites"] = _filter_sites(payload.get("sites", []), registered_short_ids)
    out["total_available"] = sum(len(s["gateways"]) for s in out["sites"])
    return out


def _filter_sites(sites: list, registered_short_ids: set) -> list:
    """Remove already-registered gateways from discovered site lists.
    Drops empty sites from the result.
    """
    filtered = []
    for site in sites:
        available = [
            gw for gw in site.get("gateways", [])
            if gw.get("short_id") not in registered_short_ids
        ]
        if available:
            filtered.append({**site, "gateways": available})
    return filtered


def _mask_email(email: str) -> str:
    """Mask email for display: keep first 3 chars + domain."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return local[:3] + "***@" + domain


class GatewayLinkedAddRequest(BaseModel):
    full_serial: str
    name: str = ""
    model: str = ""
    site_id: str = ""

@router.post("/gateways/linked", status_code=201)
async def add_linked_gateway(req: GatewayLinkedAddRequest):
    """Register a new gateway inherited from the existing authenticated account."""
    creds = await db.get_all_credentials()
    if not creds:
        raise HTTPException(status_code=401, detail="No active account session found.")
        
    primary = creds[0]
    
    # Construct a payload mimicking the standard /gateways POST structure
    add_req = GatewayAddRequest(
        full_serial=req.full_serial,
        name=req.name,
        model=req.model,
        site_id=req.site_id,
        email=primary.get("email"),
        password=primary.get("password"),
        poll_interval=30
    )
    
    # Pass execution to the heavy Phase A registration engine
    return await add_gateway(add_req)


async def _discover_gateways(email: str, password: str) -> dict:
    """Authenticate and return all gateways linked to this account, grouped by Site.

    Uses `get_site_and_device_info()` to retrieve the full site topology,
    allowing the frontend Setup Wizard to group and register multiple gateways.
    """
    try:
        from franklinwh_cloud.client import TokenFetcher, Client

        fetcher = TokenFetcher(email, password)
        await fetcher.get_token()   # raises InvalidCredentialsException on bad creds
        
        # Instantiate client with a dummy gateway to access account-level endpoints
        client = Client(fetcher, gateway="none")
        raw = await client.get_site_and_device_info()

        gateways = _parse_gateway_list(raw)
        await _resolve_models(gateways)
        return {
            "ok": True,
            "email": email,
            "gateways": gateways,
            # fetcher.info is the login result, already in hand — reading it
            # costs no extra call to the cloud.
            "account": classify_account(getattr(fetcher, "info", None)),
        }

    except ImportError:
        return {"ok": False, "error": "franklinwh-cloud library not installed"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# Mirrors franklinwh_cloud.auth.LOGIN_TYPE_USER / LOGIN_TYPE_INSTALLER. Duplicated
# rather than imported so discovery keeps working when the library is absent —
# the ImportError path in _discover_gateways is a supported state.
_ACCOUNT_TYPE_USER = 0
_ACCOUNT_TYPE_INSTALLER = 1


def classify_account(info: dict | None) -> dict:
    """Decide whether a login belongs to an installer, from the login result.

    The cloud authenticates homeowners and installers through one endpoint,
    `appUserOrInstallerLogin`, discriminated by a `type` field in the form body.
    FWHAI always sends 0, so we are always holding a homeowner-scoped session —
    but the response still reports what the account *is*, and an account that can
    log in as an installer can see every system that installer commissioned.

    `installerId` is deliberately not a signal. A homeowner's record carries the
    id of whoever installed their system; treating that as "this is an installer"
    would flag nearly every professionally commissioned home.

    Pure: takes the login result dict, hits no network.

    Unverified against a real installer login — see #42. We always send type=0,
    so only `userTypes` can catch one; if the cloud narrows that field to the
    type you authenticated as, this never fires. Verified for homeowners only.
    """
    info = info or {}
    user_types = [t for t in (info.get("userTypes") or []) if isinstance(t, int)]
    current_type = info.get("currentType")
    distributor_id = info.get("distributorId")
    affiliates = info.get("affiliateCompany") or []

    reasons = []
    if current_type == _ACCOUNT_TYPE_INSTALLER:
        reasons.append("signed in as an installer")
    if _ACCOUNT_TYPE_INSTALLER in user_types:
        reasons.append("this account can sign in as an installer")
    if distributor_id is not None:
        reasons.append("linked to a distributor")
    if affiliates:
        reasons.append("linked to a company account")

    return {
        "is_installer": bool(reasons),
        "current_type": current_type,
        "user_types": user_types,
        "reasons": reasons,
        # None means "we could not tell" — an older library, or a login result
        # without the field — and must not be read as "confirmed homeowner".
        "known": current_type is not None or bool(user_types),
    }


async def _resolve_models(gateways: list[dict]) -> None:
    """Name each gateway's model from the device catalog, in place.

    `sysHdVersion` is an integer hardware version and the catalog maps it to a
    product: 102 is "aGate X-01-AU", the Australian aGate X. Discovery used to
    render the integer as "aGate 102", which is not a product FranklinWH sells.

    A version the catalog does not know is reported as a hardware version —
    "aGate (hw 107)" — rather than invented. That reads as "we do not have this
    one yet", which is true, instead of asserting a model that does not exist.
    """
    for gw in gateways:
        if gw.get("model"):
            continue
        hw = gw.get("hw_version")
        if hw in (None, ""):
            continue
        try:
            row = await db.get_device_model(int(hw))
        except Exception:
            row = None
        if row:
            gw["model"] = row.get("model") or row.get("name") or ""
            gw["model_family"] = row.get("name") or ""
        else:
            gw["model"] = f"aGate (hw {hw})"
            logger.info(
                f"Gateway hardware version {hw} is not in the device catalog — "
                "reporting it as a hardware version rather than guessing a model."
            )


def _parse_gateway_list(raw) -> list[dict]:
    """Normalise get_site_and_device_info() response into a flat list of gateways.

    Extracts devices embedded inside `basicDeviceInfoVOList` for each site.
    """
    sites = raw.get("result") or []
    if not isinstance(sites, list):
        if isinstance(sites, dict):
            sites = list(sites.values())
        else:
            sites = []

    gateways = []
    for site in sites:
        if not isinstance(site, dict):
            continue
            
        site_id = site.get("siteId")
        site_name = site.get("siteName") or ""
        address = site.get("completeAddress") or ""
        devices = site.get("basicDeviceInfoVOList") or []
        
        for item in devices:
            if not isinstance(item, dict):
                continue
            
            # Model name, resolved from the device catalog rather than made up.
            #
            # This used to paste the hardware version after "aGate" when the
            # cloud sent no model, producing "aGate 102" — 102 being
            # sysHdVersion, an integer, presented to the user as a product name.
            # The catalog has had the answer all along: hw_version_int 102 is
            # "aGate X-01-AU", the Australian aGate X.
            #
            # Resolution is filled in by the caller, which has database access;
            # this function stays pure. An unresolved version is reported as a
            # hardware version, not dressed up as a model.
            model_name = item.get("model") or item.get("modelName") or ""
            hw_version = item.get("sysHdVersion")
                
            gateways.append({
                "serial":      item.get("gatewayId") or item.get("id") or item.get("serial") or "",
                "name":        item.get("gatewayName") or item.get("name") or "",
                "model":       model_name,
                "hw_version":  hw_version,
                "online":      item.get("status") == 1,
                "active":      item.get("activeStatus") == 1 if "activeStatus" in item else True,
                "site_id":     site_id,
                "site":        site_name,
                "address":     address,
                "account":     item.get("account") or "",
            })
            
    return gateways



# ── System-wide rate-limiter controls ─────────────────────────────────────────

class RateLimitsResponse(BaseModel):
    guard_type: str = "fhai_client_guard"
    note: str = "FHAI self-imposed guard — NOT a FranklinWH cloud server limit. Exists to catch rogue polling/misconfigured code."
    calls_last_minute: int = 0
    calls_last_hour: int = 0
    calls_today: int = 0
    limit_per_minute: int = 120
    limit_per_hour: int = 1500
    daily_budget: int = 15000
    remaining_daily: int | None = None
    is_throttled: bool = False


class RateLimitsPatch(BaseModel):
    calls_per_minute: int | None = None   # 0 = unlimited
    calls_per_hour: int | None = None     # 0 = unlimited
    daily_budget: int | None = None       # 0 = unlimited


@router.get("/system/rate-limits", response_model=RateLimitsResponse)
async def get_system_rate_limits():
    """
    Return live RateLimiter snapshot for all gateways (global guard — all share same limits).
    Reads from the first connected gateway's rate_limiter instance.
    """
    from fastapi import HTTPException
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not available")

    # Find first live gateway with a client
    snapshot = {}
    for gw in registry._services.values():
        try:
            rl = gw._client.rate_limiter if gw._client else None
            if rl:
                snapshot = rl.snapshot()
                break
        except Exception:
            continue

    # If no live gateway, return persisted/default config values
    from src.services import db as _db
    per_min  = int(await _db.get_config_value("rate_limit_per_minute",  120))
    per_hour = int(await _db.get_config_value("rate_limit_per_hour",   1500))
    daily    = int(await _db.get_config_value("rate_limit_daily_budget", 15000))

    return RateLimitsResponse(
        calls_last_minute=snapshot.get("calls_last_minute", 0),
        calls_last_hour=snapshot.get("calls_last_hour", 0),
        calls_today=snapshot.get("calls_today", 0),
        limit_per_minute=snapshot.get("limit_per_minute", per_min),
        limit_per_hour=snapshot.get("limit_per_hour", per_hour),
        daily_budget=snapshot.get("daily_budget", daily),
        remaining_daily=snapshot.get("remaining_daily"),
        is_throttled=snapshot.get("is_throttled", False),
    )


@router.patch("/system/rate-limits")
async def patch_system_rate_limits(req: RateLimitsPatch):
    """
    Update FHAI's self-imposed API rate limiter guard (global — applies to all gateways).
    Mutates live RateLimiter instances in-place (preserves sliding window history).
    Persists to app_config SQLite — survives restart.
    """
    from fastapi import HTTPException
    from src.services import db as _db
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not available")

    # Persist to DB
    if req.calls_per_minute is not None:
        await _db.set_config_value("rate_limit_per_minute", str(req.calls_per_minute))
    if req.calls_per_hour is not None:
        await _db.set_config_value("rate_limit_per_hour", str(req.calls_per_hour))
    if req.daily_budget is not None:
        await _db.set_config_value("rate_limit_daily_budget", str(req.daily_budget))

    # Mutate live RateLimiter instances (no window reset — preserves sliding history)
    updated = 0
    for gw in registry._services.values():
        try:
            rl = gw._client.rate_limiter if gw._client else None
            if rl:
                if req.calls_per_minute is not None:
                    rl.calls_per_minute = req.calls_per_minute
                if req.calls_per_hour is not None:
                    rl.calls_per_hour = req.calls_per_hour
                if req.daily_budget is not None:
                    rl.daily_budget = req.daily_budget
                updated += 1
        except Exception:
            continue

    return {
        "status": "ok",
        "gateways_updated": updated,
        "persisted": True,
        "note": "Changes applied live and persisted. Will restore on next restart.",
    }


@router.post("/system/rate-limits/reset")
async def reset_system_rate_limits():
    """Reset rate limits to FHAI recommended defaults (120/min, 1500/hr, 15000/day)."""
    return await patch_system_rate_limits(RateLimitsPatch(
        calls_per_minute=120,
        calls_per_hour=1500,
        daily_budget=15000,
    ))


@router.get("/system/poll-interval")
async def get_system_poll_interval():
    """Return current poll interval (seconds) for all active gateways."""
    registry = _get_registry()
    intervals = []
    if registry:
        for gw in registry._services.values():
            try:
                intervals.append(gw._poll_interval)
            except Exception:
                pass
    value = intervals[0] if intervals else 30
    return {"poll_interval": value, "all_intervals": intervals}


class PollIntervalRequest(BaseModel):
    poll_interval: int  # seconds, 10–300

@router.patch("/system/poll-interval")
async def patch_system_poll_interval(req: PollIntervalRequest):
    """
    Apply poll_interval globally to all registered gateways (no restart required).
    Directly mutates _poll_interval on each live GatewayService instance.
    """
    from fastapi import HTTPException
    new_poll = max(10, min(300, req.poll_interval))
    registry = _get_registry()
    if not registry:
        raise HTTPException(status_code=503, detail="Registry not available")

    results = []
    for short_id, svc in registry._services.items():
        try:
            svc._poll_interval = new_poll
            logger.info(f"[{short_id}] poll_interval updated globally → {new_poll}s")
            results.append({"short_id": short_id, "status": "ok"})
        except Exception as e:
            results.append({"short_id": short_id, "status": "error", "detail": str(e)})

    # Persist to DB for all registered gateways (best-effort)
    try:
        gw_rows = await db.list_gateways()
        for gw in gw_rows:
            profile = gw.get("profile") or {}
            if isinstance(profile, str):
                import json
                profile = json.loads(profile)
            profile["poll_interval"] = new_poll
            await db.upsert_gateway(
                short_id=gw["short_id"],
                full_serial=gw.get("full_serial", gw["short_id"]),
                name=gw.get("name", ""),
                model=gw.get("model", ""),
                profile=profile,
                credentials={},
                enabled=gw.get("enabled", 1),
            )
    except Exception as e:
        logger.warning(f"poll_interval global persist partial failure: {e}")

    return {"status": "ok", "poll_interval": new_poll, "results": results}


async def backfill_site_names() -> int:
    """Fill in site names for gateways registered before they were stored.

    Only the site id was kept at registration, so the dashboard rendered
    "Site 3447" above "SITE ID: 3447" — the same number twice, and one no user
    recognises. New registrations now carry the name; installs that predate
    this have nothing to show, and the names live in the cloud where a
    migration cannot reach them.

    One discovery call, and only while a name is actually missing. Best-effort
    throughout: a gateway whose name cannot be resolved keeps showing its id,
    which is what it was already doing.

    Returns the number of gateways updated.
    """
    try:
        gateways = await db.get_all_gateways()
    except Exception:
        logger.debug("site backfill: could not read gateways", exc_info=True)
        return 0

    missing = [g for g in gateways if not (g.get("site_name") or "").strip()]
    if not missing:
        return 0

    try:
        creds = await db.get_all_credentials()
    except Exception:
        creds = []
    if not creds:
        logger.debug("site backfill: no stored credentials")
        return 0

    cred = creds[0]
    try:
        discovered = await _discover_gateways(cred.get("email", ""), cred.get("password", ""))
    except Exception as exc:
        logger.debug(f"site backfill: discovery failed: {exc!r}")
        return 0

    by_serial = {
        str(g.get("serial") or ""): g for g in (discovered or {}).get("gateways", [])
    }

    updated = 0
    for gw in missing:
        found = by_serial.get(str(gw.get("full_serial") or ""))
        if not found:
            continue
        site_name = (found.get("site") or "").strip()
        if not site_name:
            continue
        try:
            await db.upsert_gateway(
                short_id=gw["short_id"],
                full_serial=gw["full_serial"],
                name=gw.get("name") or "",
                site_id=str(found.get("site_id") or gw.get("site_id") or ""),
                site_name=site_name,
                site_address=(found.get("address") or "").strip(),
                model=gw.get("model") or "",
            )
            updated += 1
        except Exception:
            logger.debug(f"site backfill: could not update {gw['short_id']}", exc_info=True)

    if updated:
        logger.info(f"Site names backfilled for {updated} gateway(s)")
    return updated
