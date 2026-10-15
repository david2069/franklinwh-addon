"""HTTP control endpoint — dispatches commands directly from the UI (Control tab).

Route: POST /api/gateways/{short_id}/control
Body:  {"command": str, "value": str}

This mirrors the MQTT command topics (franklinwh/{id}/control/{slug}/set)
but lets the admin UI send commands without needing an MQTT broker in the loop.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services import db
from src.app_state import get_app_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["control"])


# Valid control commands with human-readable labels and value hints
CONTROL_COMMANDS = {
    # Generator manual start/stop. Advertised because it works: the library
    # method is misnamed (it posts `manuSw`, a manual switch, not the operating
    # mode) but manuSw IS the correct field for start/stop.
    #
    # Generator MODE and the SoC thresholds are deliberately absent — the cloud
    # API exposes no setter for any of them, so advertising them would invite an
    # attempt that can only refuse. See DEF-GEN-MODE-WRITES-MANUSW.
    "generator_run": {
        "label": "Generator Run",
        "values": ["true", "false"],
        "type": "toggle",
        "description": (
            "Manual start/stop. Hardware permits a manual start only while "
            "off-grid. The value mapping is unverified upstream."
        ),
    },
    "operating_mode": {
        "label": "Operating Mode",
        "values": ["backup_only", "self_consumption", "tou"],
        "type": "select",
    },
    "storm_hedge_enabled": {
        "label": "Storm Hedge",
        "values": ["true", "false"],
        "type": "toggle",
    },
    "storm_hedge_config": {
        "label": "Storm Hedge Advanced",
        "type": "json",
        "description": "JSON payload mapping stormEn, advanceTime, and setAdvanceBackupTime"
    },
    "battery_backup_reserve": {
        "label": "Battery Backup Reserve",
        "min": 0, "max": 100, "unit": "%",
        "type": "slider",
    },
    # grid_charge_enabled / grid_discharge_enabled were advertised here and
    # implemented nowhere: dispatch_command has no branch for either, so calling
    # one returned "Unknown or untranslated command slug" — the endpoint
    # accepting a command the dispatcher then calls invalid. They are documented
    # in this module's own docstring too, which is how the gap survived.
    #
    # Left advertised deliberately rather than removed: the capability is real
    # in the cloud API (set_grid_charge / set_grid_discharge) and removing the
    # advertisement would hide a gap instead of recording it. Marked so callers
    # and tests can see the state.
    "grid_charge_enabled": {
        "unimplemented": True,
        "label": "Grid Charge",
        "values": ["true", "false"],
        "type": "toggle",
    },
    "grid_discharge_enabled": {
        "unimplemented": True,
        "label": "Grid Discharge",
        "values": ["true", "false"],
        "type": "toggle",
    },
    # Grid PCS controls (sent by Grid Import & Export modal)
    "grid_import_unlimited": {
        "label": "Grid Import Unlimited",
        "values": ["true", "false"],
        "type": "toggle",
    },
    "grid_export_unlimited": {
        "label": "Grid Export Unlimited",
        "values": ["true", "false"],
        "type": "toggle",
    },
    "grid_import_limit": {
        "label": "Grid Import Limit kW",
        "type": "number",
    },
    "grid_export_limit": {
        "label": "Grid Export Limit kW",
        "type": "number",
    },
    "dispatch_power": {
        "label": "Dispatch Power kW",
        "type": "number",
    },
    "dispatch_duration": {
        "label": "Dispatch Duration minutes",
        "type": "number",
    },
    "dispatch_target_soc": {
        "label": "Dispatch Target SOC (%)",
        "type": "number",
    },
    "dispatch_stop_soc": {
        "label": "Dispatch Stop SOC (% floor for discharge)",
        "type": "number",
    },
    "dispatch_method": {
        "label": "Dispatch Method",
        "type": "string",
    },
    "dispatch_action": {
        "label": "Dispatch Action",
        "values": ["Charge", "Discharge", "Stop"],
        "type": "select",
    },
    # Battery SOC limits — persisted to app_config, used as dispatch defaults
    "min_discharge_soc": {
        "label": "Min Discharge SOC (%)",
        "min": 0, "max": 95, "unit": "%",
        "type": "slider",
    },
    "max_charge_soc": {
        "label": "Max Charge SOC (%)",
        "min": 20, "max": 100, "unit": "%",
        "type": "slider",
    },
    # Emergency backup slugs
    "emergency_backup_duration":      {"label": "Emergency Backup Duration",      "type": "number"},
    "emergency_backup_duration_type": {"label": "Emergency Backup Duration Type", "type": "string"},
    "emergency_backup_resume_mode":   {"label": "Emergency Backup Resume Mode",   "type": "string"},
    # TOU saved dispatches
    "tou_saved_dispatches": {"label": "TOU Saved Dispatch Preset", "type": "string"},
    # Off-grid mode
    "off_grid_mode": {"label": "Off-Grid Mode", "type": "toggle"},
}


class ControlRequest(BaseModel):
    """Accept both 'command' (canonical) and 'slug' (legacy frontend alias)."""
    command: str = ""
    slug: str = ""    # frontend sends slug= instead of command=
    value: str
    source: str = "user_ui"  # user_ui | smart_dispatch | ha_automation

    def model_post_init(self, __context) -> None:
        # Normalise: if only slug is provided, copy it to command
        if not self.command and self.slug:
            self.command = self.slug
        elif not self.slug and self.command:
            self.slug = self.command


@router.get("/gateways/{short_id}/control/commands")
async def list_control_commands(short_id: str):
    """Return the list of supported control commands with their metadata."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")
    return {"short_id": short_id, "commands": CONTROL_COMMANDS}


@router.post("/gateways/{short_id}/control")
async def send_control_command(short_id: str, req: ControlRequest, force: bool = False):
    """
    Dispatch a control command to the gateway's Cloud API client.
    Returns {ok, slug, value, result} or {ok: false, error}.
    """
    # 1. System Orchestrator: Check for active Smart Dispatch lock
    sd_cfg = await db.get_smart_dispatch_config(short_id)
    strategy = sd_cfg.get("strategy_mode", "disabled")
    
    # "auto", "passive", "active", "proactive" are active execution modes
    is_sd_active = strategy in ("auto", "passive", "active", "proactive")
    
    # Orchestration Lock: Reject automated YAML commands when HEMS is in active AUTO mode
    if strategy == "auto" and req.source == "ha_automation" and not force:
        raise HTTPException(
            status_code=409,
            detail="Orchestration Lock Active: HEMS is in active AUTO mode. External automations are locked out."
        )

    # Only block "dispatch_action" commands (manual charge/discharge)
    if is_sd_active and req.command == "dispatch_action" and req.value != "Stop" and not force:
        raise HTTPException(
            status_code=409, 
            detail=f"Smart Dispatch is currently active ({strategy}). Manual override requires confirmation."
        )

    # Validate gateway exists
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found")

    # Enforce UI Override Lock: 120-minute HOLD intent on manual changes
    if req.source == "user_ui" and req.command in ("dispatch_action", "operating_mode", "battery_backup_reserve", "battery_tou_reserved_soc", "tou_saved_dispatches"):
        try:
            from src.services.intent_lock import manager as intent_manager
            intent_manager.request_intent(
                gateway_serial=gw.get("full_serial") or short_id,
                action="HOLD",
                rule_id="user_ui_override",
                priority=0,
                duration_mins=120
            )
            logger.info(f"[{short_id}] UI Override Lock: Registered 120-minute HOLD intent due to manual {req.command} command")
        except Exception as e:
            logger.error(f"[{short_id}] Failed to register UI Override HOLD intent: {e}")


    # Validate command is known
    # Allow smart circuit commands dynamically without full pre-registration
    if req.command not in CONTROL_COMMANDS and not req.command.startswith("smart_circuit_"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown command {req.command!r}. Valid: {list(CONTROL_COMMANDS)}"
        )

    # Route through registry -> GatewayService -> Cloud API
    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Gateway registry not initialised")

    # 2. Lock Orchestration: If manual dispatch starts, set an exclusive lock
    if req.command == "dispatch_action":
        if req.value in ("Charge", "Discharge"):
            # Set lock in registry (volatile, cleared on Stop or service restart)
            registry.set_exclusive_lock(short_id, "manual_dispatch", req.value)
        elif req.value == "Stop":
            registry.clear_exclusive_lock(short_id, "manual_dispatch")

    result = await registry.dispatch_command(short_id, req.command, req.value, source=req.source, force=force)
    if not result.get("ok"):
        logger.error(f"[{short_id}] HTTP control failed: {req.command}={req.value!r} -> {result}")
        raise HTTPException(status_code=400, detail=result.get("error", "Command failed"))
        
    logger.info(f"[{short_id}] HTTP control: {req.command}={req.value!r} -> {result}")
    return result

# ------------------------------------------------------------------
# Advanced Live Controls (Operating Mode)
# ------------------------------------------------------------------

async def _get_gateway_service(short_id: str):
    """Helper to fetch the active GatewayService or raise 404/503."""
    gw = await db.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id!r} not found in DB")
    registry = get_app_state().get("registry")
    if not registry:
        raise HTTPException(status_code=503, detail="Gateway registry not initialized")
    gw_svc = registry.get_gateway(short_id)
    if not gw_svc:
        raise HTTPException(status_code=503, detail="Gateway service not running")
    return gw_svc

@router.get("/gateways/{short_id}/mode/current")
async def get_operating_mode(short_id: str):
    """Fetch current mode straight from Cloud API."""
    gw_svc = await _get_gateway_service(short_id)
    return await gw_svc.get_operating_mode()

@router.get("/gateways/{short_id}/mode/reserves")
async def get_mode_reserves(short_id: str):
    """Fetch all backup reserves straight from Cloud API."""
    gw_svc = await _get_gateway_service(short_id)
    return await gw_svc.get_mode_soc_reserves()

@router.get("/gateways/{short_id}/mode/tou-raw")
async def get_tou_raw(short_id: str):
    """Return complete raw getGatewayTouListV2 response — all fields, unfiltered.

    Used by the Operating Mode diagnostics modal to show the full Cloud API
    payload including id, oldIndex, dischargeDepthSoc, multiSOCFlag, etc.
    Do NOT use this for business logic — consume /mode/reserves for that.
    """
    gw_svc = await _get_gateway_service(short_id)
    return await gw_svc.get_tou_raw()

class SetModeRequest(BaseModel):
    work_mode: int
    soc: int | None = None
    forever: int | None = None
    next_mode: int | None = None
    duration: int | None = None

@router.post("/gateways/{short_id}/mode/set")
async def set_operating_mode(short_id: str, req: SetModeRequest):
    """Change the aGate operating mode (Cloud API)."""
    gw_svc = await _get_gateway_service(short_id)
    res = await gw_svc.set_operating_mode(req.work_mode, req.soc, req.forever, req.next_mode, req.duration)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Cloud API rejected mode transition"))
    return {"ok": True}

class UpdateReserveRequest(BaseModel):
    soc: int
    work_mode: int

@router.patch("/gateways/{short_id}/mode/reserve")
async def update_reserve_soc(short_id: str, req: UpdateReserveRequest):
    """Update Reserve SOC for a specific mode."""
    gw_svc = await _get_gateway_service(short_id)
    res = await gw_svc.update_reserve_soc(req.soc, req.work_mode)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Cloud API rejected reserve SOC update"))
    
    # Note: `res["result"]` contains the raw Cloud backend output if needed.
    return {"ok": True, "result": res.get("result", {})}


@router.get("/gateways/{short_id}/control/backup-history")
async def get_backup_history(short_id: str, limit: int = 20):
    """Return recent backup/reserve audit events for this gateway."""
    rows = await db.get_backup_history(short_id, limit=limit)
    return {"ok": True, "short_id": short_id, "history": rows}

