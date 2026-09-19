"""
GatewayRegistry — manages the lifecycle of all GatewayService instances.

Responsibilities:
  - Load registered gateways from DB on startup
  - Start/stop individual GatewayService instances
  - Expose aggregate status for all gateways
  - Allow hot-add and hot-remove of gateways at runtime (Phase 5 UI)
"""
import json
import logging
from typing import Optional

from src.services.gateway_service import GatewayService
from src.services import db

logger = logging.getLogger(__name__)


class GatewayRegistry:
    """
    Singleton-style registry holding all active GatewayService instances.
    Stored in app_state["registry"] during lifespan.
    """

    def __init__(self, poll_interval: int = 30, on_data=None):
        self._services: dict[str, GatewayService] = {}
        self._poll_interval = poll_interval
        self._on_data = on_data  # MQTT callback, injected in Phase 3
        self._locks: dict[str, dict] = {} # short_id -> {type, value, timestamp}

    def set_exclusive_lock(self, short_id: str, lock_type: str, value: any):
        import time
        sid = short_id.upper()
        self._locks[sid] = {
            "type": lock_type,
            "value": value,
            "at": time.time()
        }
        logger.info(f"[{sid}] Exclusive lock SET: {lock_type}={value}")

    def clear_exclusive_lock(self, short_id: str, lock_type: str = None):
        sid = short_id.upper()
        if sid in self._locks:
            if not lock_type or self._locks[sid]["type"] == lock_type:
                old = self._locks.pop(sid)
                logger.info(f"[{sid}] Exclusive lock CLEARED: {old['type']}")

    def get_exclusive_lock(self, short_id: str) -> Optional[dict]:
        return self._locks.get(short_id.upper())

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def start_all(self) -> int:
        """
        Load all enabled gateways from DB and start their poll loops.
        Returns the number of gateways started.
        """
        gateways = await db.get_all_gateways()
        started = 0
        for gw in gateways:
            if not gw.get("enabled", 1):
                logger.info(f"[{gw['short_id']}] Disabled — skipping")
                continue
            try:
                await self._start_gateway_record(gw)
                started += 1
            except Exception as exc:
                logger.error(f"[{gw['short_id']}] Failed to start: {exc}")
        logger.info(f"GatewayRegistry: {started}/{len(gateways)} gateways started")
        return started

    async def stop_all(self) -> None:
        """Stop all running GatewayService instances."""
        for short_id, svc in list(self._services.items()):
            logger.info(f"Stopping gateway {short_id}...")
            await svc.stop()
        self._services.clear()

    # ------------------------------------------------------------------
    # Individual gateway management
    # ------------------------------------------------------------------

    async def start_gateway(self, short_id: str) -> bool:
        """
        Start poll loop for a specific gateway (loads credentials from DB).
        Returns True if started, False if not found or already running.
        """
        sid = short_id.upper()
        if sid in self._services and self._services[sid].is_running:
            logger.warning(f"[{sid}] Already running")
            return False
        gw = await db.get_gateway(sid)
        if not gw:
            logger.error(f"[{sid}] Not found in DB")
            return False
        await self._start_gateway_record(gw)
        return True

    async def stop_gateway(self, short_id: str) -> bool:
        """Stop a specific gateway's poll loop. Returns True if was running."""
        sid = short_id.upper()
        svc = self._services.get(sid)
        if not svc:
            return False
        await svc.stop()
        del self._services[sid]
        return True

    async def restart_gateway(self, short_id: str) -> bool:
        """Stop + re-start (e.g. after credential update)."""
        sid = short_id.upper()
        await self.stop_gateway(sid)
        return await self.start_gateway(sid)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_gateway(self, short_id: str) -> Optional[GatewayService]:
        return self._services.get(short_id.upper())

    def get_gateways(self) -> list[GatewayService]:
        return list(self._services.values())

    def get_status(self, short_id: str) -> Optional[dict]:
        svc = self._services.get(short_id.upper())
        return svc.status.to_dict() if svc else None

    def get_all_status(self) -> list[dict]:
        return [svc.status.to_dict() for svc in self._services.values()]

    def is_running(self, short_id: str) -> bool:
        svc = self._services.get(short_id.upper())
        return svc is not None and svc.is_running

    async def dispatch_command(self, identifier: str, slug: str, value: str, source: str = "ha_automation", force: bool = False) -> dict:
        """Route a control command to the appropriate GatewayService by either short_id or full_serial."""
        svc = self._services.get(identifier.upper())
        if not svc:
            # Fallback: Check if the identifier matches ANY of the active GatewayService full_serials or short_ids (case-insensitive)
            # This handles cases where Home Assistant natively broadcasts back onto the full 20-character base topic.
            for s in self._services.values():
                if str(s.full_serial).lower() == identifier.lower() or str(s.short_id).lower() == identifier.lower():
                    svc = s
                    break

        if not svc:
            return {"ok": False, "short_id": identifier, "error": f"Gateway {identifier!r} not running"}

        # Orchestration Lock: Reject automated commands when HEMS is in active AUTO mode
        try:
            sd_cfg = await db.get_smart_dispatch_config(svc.short_id)
            strategy = sd_cfg.get("strategy_mode", "disabled")
            if strategy == "auto" and source == "ha_automation" and not force:
                logger.warning(f"[{svc.short_id}] Orchestration Lock Active: Rejected automated {slug}={value!r} from ha_automation")
                return {
                    "ok": False,
                    "error": "Orchestration Lock Active: HEMS is in active AUTO mode. External automations are locked out."
                }
        except Exception as e:
            logger.error(f"[{svc.short_id}] Failed to check strategy_mode for orchestration lock: {e}")

        return await svc.dispatch_command(slug, value)


    def count(self) -> int:
        return len(self._services)

    def running_count(self) -> int:
        return sum(1 for svc in self._services.values() if svc.is_running)

    def get_site_snapshot(self) -> dict:
        """
        Aggregate telemetry from all active gateways to form a Virtual Site Meter.
        Crucial for multi-gateway sites where grid/battery/solar are distributed.
        """
        running = [svc for svc in self._services.values() if svc.is_running]
        if not running:
            return {
                "p_fhp": 0.0, "p_uti": 0.0, "p_sun": 0.0, "p_ld": 0.0,
                "soc_avg": 0.0, "soc_min": 0.0, "soc_max": 0.0,
                "count": 0, "is_off_grid": False, "is_three_phase": False
            }

        p_fhp = 0.0  # Battery (W)
        p_uti = 0.0  # Grid (W)
        p_sun = 0.0  # Solar (W)
        p_ld  = 0.0  # Load (W)
        socs  = []
        is_off_grid = False
        is_three_phase = False

        for svc in running:
            data = svc.status.last_data or {}
            
            # Power aggregation (kW -> W conversion for precision if needed, but we stick to float kW)
            # Standard mappings from GatewayService._normalise_stats
            p_fhp += float(data.get("battery_kw", 0.0))
            p_uti += float(data.get("grid_kw", 0.0))
            p_sun += float(data.get("solar_kw", 0.0))
            p_ld  += float(data.get("home_kw", 0.0))
            
            soc = data.get("battery_soc")
            if soc is not None:
                socs.append(float(soc))
            
            if data.get("off_grid") == "ON":
                is_off_grid = True
            
            # Site Topology detection
            grid_type = svc.context.get("grid_profile_name", "").lower()
            if "three" in grid_type or "415" in grid_type:
                is_three_phase = True

        return {
            "p_fhp": round(p_fhp, 3),
            "p_uti": round(p_uti, 3),
            "p_sun": round(p_sun, 3),
            "p_ld":  round(p_ld, 3),
            "soc_avg": round(sum(socs) / len(socs), 1) if socs else 0.0,
            "soc_min": min(socs) if socs else 0.0,
            "soc_max": max(socs) if socs else 0.0,
            "count": len(running),
            "is_off_grid": is_off_grid,
            "is_three_phase": is_three_phase
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _start_gateway_record(self, gw: dict) -> None:
        short_id = gw["short_id"]
        full_serial = gw.get("full_serial", short_id)

        # Load from gateway_credentials table (design v2)
        creds = await db.get_credentials(full_serial)
        if creds:
            email = creds.get("email", "")
            password = creds.get("password", "")
        else:
            # Legacy fallback: try credentials_json in gateway row
            creds_raw = gw.get("credentials_json") or "{}"
            try:
                legacy = json.loads(creds_raw) if isinstance(creds_raw, str) else creds_raw
            except json.JSONDecodeError:
                legacy = {}
            email = legacy.get("email", "")
            password = legacy.get("password", "")
            if email and password:
                logger.info(f"[{full_serial}] Migrating credentials from legacy field")
                await db.upsert_credentials(full_serial, email, password, source="migration")

        if not email:
            logger.warning(
                f"[{full_serial}] No credentials stored — poll suspended until credentials are set"
            )

        svc = GatewayService(
            short_id=short_id,
            full_serial=full_serial,
            credentials={"email": email, "password": password},
            poll_interval=self._poll_interval,
            on_data=self._on_data,
        )
        svc.context["name"] = gw.get("name", "")
        
        profile_raw = gw.get("profile_json") or "{}"
        try:
            svc.context["profile"] = json.loads(profile_raw)
        except Exception:
            svc.context["profile"] = {}
            
        svc.start()
        self._services[short_id.upper()] = svc
        logger.info(f"[{full_serial}] GatewayService registered and started")
