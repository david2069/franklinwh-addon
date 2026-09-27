"""

Gateway service — one instance per registered aGate serial.

Responsibilities:
  - Hold Cloud API credentials for this gateway
  - Run an async poll loop (get_stats every poll_interval seconds)
  - Fan out results: write to rolling metrics DB + notify MQTT publisher
  - Expose live status (poll_status, last_poll_age_s, last_error)
  - Handle errors with exponential backoff (max 5 min)
  - Dispatch control commands to the Cloud API (Phase 6)

Lifecycle:
  start()            → spawns asyncio Task
  stop()             → cancels task, awaits clean shutdown
  dispatch_command() → async, calls Cloud API directly

Supported control commands (slug → method):
  operating_mode          → set_mode(mode_value)
  storm_guard_enabled     → set_storm_guard(bool)
  battery_backup_reserve  → set_backup_reserve(percent: int)
  grid_charge_enabled     → set_grid_charge(bool)
  grid_discharge_enabled  → set_grid_discharge(bool)
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Optional

from src.services.cloud_dispatch import AsyncCloudDispatchService

logger = logging.getLogger(__name__)

# Back-off: 30s, 60s, 120s, 240s, 300s (cap)
BACKOFF_STEPS = [30, 60, 120, 240, 300]

# TOU dispatch ID → human label (matches FEM schedule UI)
# Derived, not retyped. This map was one of six copies that had already
# drifted — some listed six codes, some five, none listed 0.
from src.services.dispatch_codes import labels as _dispatch_labels

_DISPATCH_ID_LABELS: dict[int, str] = _dispatch_labels()

# TOU wave (tariff) type → human label
# Super Off-Peak is value 4 in the schedule UI (confirmed from schedule.html)
_WAVE_TYPE_LABELS: dict[int, str] = {
    0: "Off-Peak",
    1: "Mid-Peak",
    2: "On-Peak",
    3: "Super Off-Peak",  # may appear as 3 in API responses
    4: "Super Off-Peak",  # value used in FEM schedule UI dropdown
}


# ── Helper functions (must be defined before COMMAND_DISPATCH) ─

def _str_to_bool(v: str) -> bool:
    """Convert MQTT payload string to bool. '1'/'true'/'on' -> True, else False."""
    return str(v).lower() in ("1", "true", "on", "yes")


@dataclass
class GatewayStatus:
    short_id: str
    poll_status: str = "stopped"   # stopped | starting | ok | error | retrying
    last_poll_at: Optional[float] = None
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    consecutive_errors: int = 0
    first_error_time: Optional[float] = None     # start of current outage burst
    first_error_signature: Optional[str] = None  # exception-class signature of first failure in burst
    mqtt_published: bool = False
    last_data: dict = field(default_factory=dict)
    # When last_data was last actually WRITTEN (GH #35).
    #
    # Distinct from last_poll_at on purpose. Both stale-window drop paths
    # update last_poll_at even though they deliberately do not publish the
    # payload, so last_poll_age_s measures "when did we last try", not "how
    # old is the data we are serving". During a cloud degradation the two
    # diverge without limit: polls keep succeeding-and-dropping every 30s
    # while the retained data silently ages. Anything that ACTS on telemetry
    # must gate on this field, not on last_poll_age_s.
    last_data_at: Optional[float] = None

    @property
    def last_poll_age_s(self) -> Optional[int]:
        if self.last_poll_at is None:
            return None
        return int(time.time() - self.last_poll_at)

    @property
    def last_data_age_s(self) -> Optional[int]:
        """Seconds since last_data was last written. None if never populated."""
        if self.last_data_at is None:
            return None
        return int(time.time() - self.last_data_at)

    def to_dict(self) -> dict:
        return {
            "short_id": self.short_id,
            "poll_status": self.poll_status,
            "last_poll_age_s": self.last_poll_age_s,
            "last_data_age_s": self.last_data_age_s,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "mqtt_published": self.mqtt_published,
            "last_data": self.last_data,
        }


class GatewayService:
    """
    Manages one aGate — poll loop, status tracking, fan-out.

    :param short_id:        Last 8 chars of aGate serial.
    :param full_serial:     Full serial for Cloud API calls.
    :param credentials:     {"email": str, "password": str}
    :param poll_interval:   Seconds between polls (default 30).
    :param on_data:         Async callback(short_id, data_dict) — called after each successful poll.
                            Phase 3 will inject the MQTT publish callback here.
    :param client_factory:  Async callable(email, pw) → client — injected for testability.
    """

    def __init__(
        self,
        short_id: str,
        full_serial: str,
        credentials: dict,
        poll_interval: int = 30,
        on_data: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        client_factory: Optional[Callable] = None,
    ):
        self.short_id = short_id.upper()
        self.full_serial = full_serial.upper()
        self._credentials = credentials
        self._poll_interval = poll_interval
        self._on_data = on_data
        self._client_factory = client_factory or self._default_client_factory

        self.status = GatewayStatus(short_id=short_id)
        self._polling_tasks: dict[str, asyncio.Task] = {}
        # New: stagger secondary auxiliary API calls to save quota
        self._poll_counter: dict[str, int] = {}
        self._client_cache: dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[Any] = None
        import datetime as _dt_init
        self.context: dict[str, Any] = {
            "started_at": _dt_init.datetime.now(_dt_init.timezone.utc).isoformat(),
        }
        self._pcs_cache: dict[str, Any] = {}
        self._pcs_cache_time: float = 0
        self.cloud_dispatch = AsyncCloudDispatchService(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the poll loop as a background asyncio Task."""
        if self._task and not self._task.done():
            logger.warning(f"[{self.short_id}] Already running — ignoring start()")
            return
        self.status.poll_status = "starting"
        self._task = asyncio.create_task(
            self._run_loop(), name=f"gateway-{self.short_id}"
        )
        logger.info(f"[{self.short_id}] Poll loop started")

    async def stop(self) -> None:
        """Cancel the poll loop and wait for it to finish."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.status.poll_status = "stopped"
        if self._client:
            if hasattr(self._client, "aclose"):
                await self._client.aclose()
            elif hasattr(self._client, "close"):
                await self._client.close()
            self._client = None
        logger.info(f"[{self.short_id}] Poll loop stopped")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _get_or_create_client(self) -> Any:
        if self._client is not None:
            return self._client
        email = self._credentials.get("email", "")
        password = self._credentials.get("password", "")
        if not email or not password:
            self.status.poll_status = "no_credentials"
            raise RuntimeError("no_credentials")
        self._client = await self._client_factory(email, password, self.full_serial)
        return self._client

    def _handle_token_expiration(self, exc: Exception) -> bool:
        """Check if exception is a Token Expiration and clear client cache if so."""
        err_str = str(exc).lower()
        if "token expired" in err_str or "invalid token" in err_str:
            logger.warning(f"[{self.short_id}] Token expired or invalid. Destroying API client cache to force re-authentication.")
            self._client = None
            return True
        return False


    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        backoff_idx = 0
        while True:
            try:
                client = await self._get_or_create_client()

                # Maintain staggered poll counter
                self._poll_counter[self.short_id] = self._poll_counter.get(self.short_id, 0) + 1
                counter = self._poll_counter[self.short_id]

                # Electrical metrics (voltage, current, frequency, extended relays) are
                # populated every 12th poll via the library's include_electrical flag.
                # Library (Layer 6 / CACHING_STRATEGY.md): once a single include_electrical=True
                # poll fires, all 211 fields hold their last-known-good values on all
                # subsequent fast polls — no manual sticky cache needed.
                include_elec = (counter % 12 == 0)
                stats = await client.get_stats(include_electrical=include_elec)

                # The library sets is_stale when the cloud answered HTTP 200 with
                # result: null — a known FranklinWH glitch — and handed back the
                # last good reading rather than zeros. Its own CACHING_STRATEGY.md
                # puts the obligation on us: "Consumers must check stats.is_stale
                # before writing to HA / MQTT."
                #
                # We were not checking it. We had our own guess instead — a
                # soc==0 sentinel — which detected the symptom from BEFORE the
                # library started substituting last-known-good. Since then the
                # values look plausible, so the guess never fires and stale
                # readings were published as though they were live.
                self.context["_last_stats_is_stale"] = bool(getattr(stats, "is_stale", False))
                results = {"stats": stats}
                


                # Phase 110: Relocate fast-changing secondary properties to core telemetry
                # Current TOU block — gives active tariff name, dispatch code, remaining time.
                # TOU blocks change on 30-minute schedule boundaries; calling every poll (~30s)
                # makes 60× more calls than necessary. Cadence: every 6th cycle (~3 min).
                # Belt-and-suspenders: library cache TTL also set to 60s at client creation.
                # 3-minute lag is acceptable — TOU block durations are 30 min minimum.
                if counter % 6 == 0 or counter == 1:
                    try:
                        results["tou_info"] = await client.get_tou_info(option=1)
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch TOU info: {e}")

                if counter % 12 == 0 or counter == 1:
                    try:
                        results["weather"] = await client.get_weather()
                    except Exception as e:
                        logger.warning(f"[{self.short_id}] Failed to fetch weather: {e}")
                    try:
                        results["progressing_storms"] = await client.get_progressing_storm_list()
                    except Exception as e:
                        logger.warning(f"[{self.short_id}] Failed to fetch progressing storm list: {e}")

                    
                # accessories_power (cmdType 353 via sendMqtt).
                # DEFAULT_CACHE enforces a 120s TTL at the library level — no per-tick guard needed.
                # get_stats() also surfaces switch_1_load/switch_2_load so last cached value covers any gap.
                try:
                    results["accessories_power"] = await client.get_accessories_power_info(option="0")
                except Exception as e:
                    logger.debug(f"[{self.short_id}] Failed to fetch accessories power info: {e}")

                # Tertiary accessories (fetch ONLY ONCE at startup. Static config only.)
                profile = self.context.get("profile") or {}
                if counter == 1:
                    # 1. Smart Circuits
                    if "smart_circuits" in profile:
                        results["smart_circuits"] = profile.get("smart_circuits")
                    elif "accessories" in profile and "smart_circuits" in profile["accessories"]:
                        results["smart_circuits"] = profile["accessories"]["smart_circuits"]
                    else:
                        try:
                            results["smart_circuits"] = await client.get_smart_circuits_info()
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Failed to fetch smart circuits: {e}")
                            
                    # 2. Generator Config
                    if "generator" in profile:
                        results["generator"] = profile.get("generator")
                    else:
                        try:
                            results["generator"] = await client.get_generator_info()
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Failed to fetch generator info: {e}")
                            
                    # 3. Power Control Params
                    try:
                        ps = await client.get_power_control_settings()
                        results["power_settings"] = ps
                        # Extract Site DNA fields for matrix orchestration
                        _p = ps.get("result", ps) if isinstance(ps, dict) else (ps if hasattr(ps, "notControlExportSolar") else {})
                        self.context["not_control_export_solar"] = _p.get("notControlExportSolar") if isinstance(_p, dict) else getattr(_p, "notControlExportSolar", None)
                        self.context["grid_feed_max"] = _p.get("globalGridDischargeMax") if isinstance(_p, dict) else getattr(_p, "globalGridDischargeMax", None)
                        self.context["grid_max"] = _p.get("globalGridChargeMax") if isinstance(_p, dict) else getattr(_p, "globalGridChargeMax", None)
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch power settings: {e}")
                        
                    # 4. Global Hardware Identity
                    if "device_info" in profile:
                        results["device_info"] = profile.get("device_info")
                    else:
                        try:
                            results["device_info"] = await client.get_device_info()
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Failed to fetch device info: {e}")
                    # ── Static field seeding from profile_json (Tier A — never re-read from cloud) ──
                    # As of profile_version=2, IntegrationManager writes ALL static fields at
                    # onboarding time. Read them here; only fall back to cloud APIs if the profile
                    # predates the enrichment (profile_version < 2 or field absent).
                    _pv = int(profile.get("_profile_version", 1))
                    _profile_enriched = (_pv >= 2)

                    # Model / firmware / conn_type — previously required get_home_gateway_list()
                    _fw_from_profile = profile.get("firmware", "")
                    if _fw_from_profile:
                        self.context["firmware_version"]      = _fw_from_profile
                        self.context["cloud_software_version"] = _fw_from_profile
                    _hw = profile.get("hw_version")
                    _sku = profile.get("sku", "")
                    _mdl = profile.get("model") or profile.get("model_name", "")
                    if _mdl:
                        self.context["device_model_full"] = f"{_mdl} ({_sku})" if _sku else _mdl
                    _ct = profile.get("conn_type")
                    if _ct is not None:
                        self.context["gw_conn_type"] = _ct

                    if not _profile_enriched:
                        # Legacy profile (pre-v2) — fall back to get_home_gateway_list() once
                        # so existing installs continue to work until next re-onboarding.
                        try:
                            _gw_list_res = await client.get_home_gateway_list()
                            _gw_list = _gw_list_res.get("result", []) if isinstance(_gw_list_res, dict) else []
                            _gw_entry = next((g for g in _gw_list if g.get("id") == self.full_serial), _gw_list[0] if _gw_list else {})
                            if _gw_entry:
                                _fw = _gw_entry.get("version", "")
                                if _fw:
                                    self.context["firmware_version"] = _fw
                                    if not self.context.get("cloud_software_version"):
                                        self.context["cloud_software_version"] = _fw
                                _hw_ver_int = _gw_entry.get("sysHdVersion")
                                if _hw_ver_int is not None:
                                    try:
                                        from franklinwh_cloud.const import FRANKLINWH_MODELS
                                        _model_info = FRANKLINWH_MODELS.get(int(_hw_ver_int), {})
                                        _model_str = _model_info.get("model", f"aGate (HW v{_hw_ver_int})")
                                        _sku_str = _model_info.get("sku", "")
                                        self.context["device_model_full"] = f"{_model_str} ({_sku_str})" if _sku_str else _model_str
                                    except Exception as _me:
                                        logger.debug(f"[{self.short_id}] Model lookup failed: {_me}")
                                _conn = _gw_entry.get("connType")
                                if _conn is not None:
                                    self.context["gw_conn_type"] = _conn
                            logger.debug(f"[{self.short_id}] Legacy profile: seeded model/firmware from gateway list")
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Legacy profile: gateway list seed failed: {e}")
                    else:
                        logger.debug(f"[{self.short_id}] profile_v2: model/firmware/conn read from DB — no cloud call needed")

                    # Grid compliance profile — Tier A, set by installer at commissioning.
                    # Read from profile_json if available (populated by IntegrationManager v2+).
                    # Fall back to API only for legacy profiles.
                    _gp_from_profile = profile.get("grid_profile_name", "")
                    if _gp_from_profile:
                        self.context["grid_profile_name"] = _gp_from_profile
                        logger.debug(f"[{self.short_id}] Grid profile from DB: '{_gp_from_profile}' — no cloud call needed")
                    elif not self.context.get("grid_profile_name"):
                        try:
                            _gp_res = await client.get_grid_profile_info(requestType=1)
                            _gp_list = _gp_res.get("list", []) if isinstance(_gp_res, dict) else []
                            _current_id = _gp_res.get("currentId")
                            _gp_name = ""
                            if _gp_list and _current_id is not None:
                                _match = next((p for p in _gp_list if p.get("id") == _current_id), None)
                                _gp_name = _match.get("name", "") if _match else ""
                            if _gp_name:
                                self.context["grid_profile_name"] = _gp_name
                                # Extract electricityType (Phase 0 Discovery)
                                self.context["electricity_type"] = _gp_res.get("electricityType") if isinstance(_gp_res, dict) else None
                                logger.info(f"[{self.short_id}] Grid compliance profile (API fallback): {_gp_name} (id={_current_id})")
                            else:
                                logger.debug(f"[{self.short_id}] Grid profile not resolved (currentId={_current_id})")
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Failed to fetch grid profile: {e}")
                    else:
                        logger.debug(f"[{self.short_id}] Grid profile already in context: '{self.context['grid_profile_name']}' — skipping")

                    # Phase B: Cache software version from device_info for persistence across polls
                    di_startup = results.get("device_info", {})
                    if not isinstance(di_startup, dict):
                        di_startup = self._dataclass_to_dict(di_startup) if di_startup else {}
                    sw_startup = di_startup.get("softwareVersion", "")
                    if sw_startup:
                        self.context["cloud_software_version"] = sw_startup
                    # Phase D: record first successful poll time for integration_startup_time entity
                    import datetime as _dt_fp
                    if "first_poll_at" not in self.context:
                        self.context["first_poll_at"] = _dt_fp.datetime.now(_dt_fp.timezone.utc).isoformat()
                            
                    # 4b. aPower Info (Firmwares, Capacity)
                    # These are STATIC Tier-A facts — fetched ONCE at startup and
                    # persisted to profile_json.apower_units so they survive restarts
                    # and are always available in /data without a BMS trigger.
                    try:
                        _ai_raw = await client.get_apower_info()
                        results["apower_info"] = _ai_raw
                        # Persist firmware fields to profile_json.apower_units
                        _ai_result = _ai_raw if isinstance(_ai_raw, list) else (
                            _ai_raw.get("result", []) if isinstance(_ai_raw, dict) else []
                        )
                        if _ai_result:
                            try:
                                from src.services import db as _db
                                _gw_row = await _db.get_gateway(self.short_id)
                                if _gw_row:
                                    _prof = json.loads(_gw_row.get("profile_json") or "{}") if _gw_row.get("profile_json") else {}
                                    _stored_units = {u.get("serial", ""): u for u in _prof.get("apower_units", [])}
                                    for _u in _ai_result:
                                        _sn = _u.get("apowerSn", "")
                                        if not _sn:
                                            continue
                                        _entry = _stored_units.get(_sn, {"serial": _sn})
                                        # Map API camelCase → profile snake_case
                                        for _api_k, _prof_k in [
                                            ("fpgaVer", "fpga_ver"), ("dcdcVer", "dcdc_ver"),
                                            ("invVer",  "inv_ver"),  ("bmsVer",  "bms_ver"),
                                            ("blVer",   "bl_ver"),   ("thVer",   "th_ver"),
                                            ("peHwVer", "pe_hw_ver"),
                                            ("ratedCapacity", "rated_kwh"), ("ratedPower", "rated_kw"),
                                        ]:
                                            _v = _u.get(_api_k)
                                            if _v is not None and _v != "":
                                                _entry[_prof_k] = _v
                                        _stored_units[_sn] = _entry
                                    _prof["apower_units"] = list(_stored_units.values())
                                    await _db.upsert_gateway(
                                        short_id=self.short_id,
                                        full_serial=self.full_serial,
                                        name=_gw_row.get("name", ""),
                                        site_id=_gw_row.get("site_id", ""),
                                        model=_gw_row.get("model", ""),
                                        profile=_prof,
                                        credentials={},
                                        enabled=_gw_row.get("enabled", 1),
                                    )
                                    logger.info(f"[{self.short_id}] aPower firmware persisted to profile_json for {len(_ai_result)} unit(s)")
                            except Exception as _pe:
                                logger.warning(f"[{self.short_id}] Failed to persist apower firmware to profile: {_pe}")
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch apower info: {e}")
                            
                    # 5. Network Interfaces
                    if "network_info" in profile:
                        results["network_info"] = profile.get("network_info")
                    else:
                        try:
                            results["network_info"] = await client.get_network_info()
                        except Exception as e:
                            logger.debug(f"[{self.short_id}] Failed to fetch network info: {e}")

                    # 6. Fallback or generic fetches that don't belong in fast-poll
                    try:
                        results["tou_schedule"] = await client.get_tou_dispatch_detail()
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch TOU schedule: {e}")
                    
                    try:
                        # User Mandate: read the user-assigned cloud gateway name ONCE at startup.
                        # Write it to the DB so it persists across restarts — never re-read on polls.
                        # Generic placeholder names ('aGate', 'agate x', '') are treated as
                        # "not yet resolved" so the cloud-assigned name always wins on first boot.
                        _GENERIC_NAMES = {"agate", "agate x", ""}
                        _current_name = (self.context.get("name") or "").strip().lower()
                        _name_is_generic = _current_name in _GENERIC_NAMES or _current_name.startswith("100")
                        det = await client.get_device_detail()
                        res = det.get("result", {}) if isinstance(det, dict) else {}
                        true_name = (res.get("gatewayName") or "").strip()
                        if true_name and _name_is_generic:
                            logger.info(f"[{self.short_id}] Persisting cloud gateway name: '{true_name}' (was: '{self.context.get('name', '')}')")
                            self.context["name"] = true_name
                            try:
                                from src.services import db
                                gw_row = await db.get_gateway(self.short_id)
                                if gw_row:
                                    profile = json.loads(gw_row.get("profile_json") or "{}") if gw_row.get("profile_json") else {}
                                    await db.upsert_gateway(
                                        short_id=self.short_id,
                                        full_serial=self.full_serial,
                                        name=true_name,
                                        site_id=gw_row.get("site_id", ""),
                                        model=gw_row.get("model", ""),
                                        profile=profile,
                                        credentials={},
                                        enabled=gw_row.get("enabled", 1)
                                    )
                            except Exception as db_err:
                                logger.warning(f"[{self.short_id}] Failed to persist gateway name to DB: {db_err}")
                        else:
                            logger.debug(f"[{self.short_id}] Gateway name already resolved: '{self.context.get('name', '')}' — skipping cloud read")
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch device detail for gateway name: {e}")


                    try:
                        results["reserves"] = await client.get_all_mode_soc()
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch mode reserves: {e}")

                    # 7. Storm Hedge Settings — seed from API on startup so entities reflect real values
                    try:
                        results["storm_settings"] = await client.get_storm_settings()
                        # DEF-STORM-LEAD-FIX: Always re-seed ha_input_cache from API so
                        # storm_backup_lead reflects the real value on every startup poll.
                        # Previously only seeded if key was absent, which broke re-population
                        # after restarts where the cache was cleared.
                        _api_storm = results["storm_settings"]
                        if isinstance(_api_storm, dict):
                            _api_storm_res = _api_storm.get("result", _api_storm)
                            _hic = self.context.setdefault("ha_input_cache", {})
                            # Always overwrite from API on startup (user commands take over after)
                            if _api_storm_res.get("enableStorm") is not None:
                                _hic["storm_hedge"] = "ON" if _api_storm_res.get("enableStorm") else "OFF"
                            if _api_storm_res.get("setAdvanceBackupTime") is not None:
                                _hic["storm_strategy"] = int(_api_storm_res.get("setAdvanceBackupTime", 0))
                            if _api_storm_res.get("advanceBackupTime") is not None:
                                # advanceBackupTime is already in minutes — do NOT multiply by 60
                                _hic["storm_backup_lead"] = int(float(_api_storm_res.get("advanceBackupTime", 0)))
                    except Exception as e:
                        logger.debug(f"[{self.short_id}] Failed to fetch storm settings: {e}")

                    # 8. Persist discovered Site DNA to database
                    try:
                        from src.services import db as _db
                        _gw_row = await _db.get_gateway(self.short_id)
                        if _gw_row:
                            await _db.upsert_gateway(
                                short_id=self.short_id,
                                full_serial=self.full_serial,
                                name=_gw_row.get("name", ""),
                                site_id=_gw_row.get("site_id", ""),
                                model=_gw_row.get("model", ""),
                                profile=json.loads(_gw_row.get("profile_json") or "{}"),
                                enabled=bool(_gw_row.get("enabled", 1)),
                                electricity_type=self.context.get("electricity_type"),
                                grid_feed_max=self.context.get("grid_feed_max"),
                                grid_max=self.context.get("grid_max"),
                                not_control_export_solar=1 if self.context.get("not_control_export_solar") else 0 if self.context.get("not_control_export_solar") is not None else None
                            )
                            logger.info(f"[{self.short_id}] Site DNA discovery persisted to database.")
                    except Exception as _dns_err:
                        logger.warning(f"[{self.short_id}] Failed to persist Site DNA: {_dns_err}")

                # AP-4 compliance: BMS data (cell voltages, temps, SoH) is NOT polled in the
                # background. get_bms_info issues sequential sendMQTT calls and must only be
                # called on explicit user action (Battery tab Refresh) or a user-configured
                # Automation with the record_bms action type.
                # Removed: unconditional counter % 30 slow-poll (was firing every ~15 min).

                # Update client cache with any newly fetched secondary/tertiary data before merging
                for k in ["smart_circuits", "generator", "accessories_power", "power_settings", "tou_schedule", "tou_info", "raw_mode", "device_info", "apower_info", "network_info", "bms", "reserves", "storm_settings", "weather", "progressing_storms"]:
                    if k in results:
                        self._client_cache[k] = results[k]


                # Merge cache into results so they are ALWAYS present for HA
                results.update(self._client_cache)

                # Normalise the entire combined payload for the MQTT publisher dispatch
                data = self._normalise_stats(results)

                # ── Batch I drop path ─────────────────────────────────────
                # _normalise_stats flags cloud stale-window payloads (200 OK
                # with configured mode + all runtime zeros). When flagged:
                # skip _store_metrics + _on_data + last_data write so the
                # sensor + Reporting page keep showing the last-known-good
                # state. Treat as a soft outage (poll counted, no data
                # propagated). Backoff isn't reset — a real successful poll
                # will reset it on the next iteration.
                if isinstance(data, dict) and data.get("_fhai_suspect_stale_window"):
                    self.status.poll_status = "stale_window_dropped"
                    self.status.last_poll_at = time.time()
                    # NOTE: intentionally do NOT clear last_error / consecutive_errors
                    # here — we didn't actually recover from a real outage; a
                    # stale-window is a soft failure. But do NOT set last_data
                    # or fan out. Just await the next poll cycle.
                    _next_after = self._poll_interval
                    await asyncio.sleep(_next_after)
                    continue

                # If we were in an outage burst, log a single recovery summary
                if self.status.consecutive_errors > 0 and self.status.first_error_time:
                    burst_dur = int(time.time() - self.status.first_error_time)
                    logger.info(
                        f"[{self.short_id}] Poll recovered after {self.status.consecutive_errors} "
                        f"consecutive failures over {burst_dur}s "
                        f"(first error: {self.status.first_error_signature or 'unknown'})"
                    )

                self.status.poll_status = "ok"
                self.status.last_poll_at = time.time()
                self.status.last_error = None
                self.status.consecutive_errors = 0
                self.status.first_error_time = None
                self.status.first_error_signature = None
                self.status.last_data = data
                self.status.last_data_at = time.time()   # GH #35
                backoff_idx = 0

                # db.touch_gateway() existed and was called from nowhere, so
                # gateways.last_seen was written once at registration and never
                # again. The column read "Last Seen" and showed the moment the
                # container last started — on a gateway polling healthily every
                # few seconds. Throttled because a poll is frequent and this is
                # a write; the column is read by a human, not a scheduler.
                _now = time.time()
                if _now - getattr(self, "_last_seen_written_at", 0) >= 60:
                    self._last_seen_written_at = _now
                    try:
                        from src.services import db as _db
                        await _db.touch_gateway(self.short_id)
                    except Exception:
                        logger.debug(f"[{self.short_id}] last_seen update failed", exc_info=True)

                # ── Aegis Healing & Periodic Drift Verification ──
                try:
                    from src.services import db as _db
                    cfg = await _db.get_smart_dispatch_config(self.short_id)
                    snapshot_json = cfg.get("baseline_tou_snapshot")
                    if snapshot_json:
                        expires_at_iso = cfg.get("active_override_expires_at")
                        expired = False
                        if expires_at_iso:
                            import datetime as _dt
                            now_utc = _dt.datetime.now(_dt.timezone.utc)
                            expires_at = _dt.datetime.fromisoformat(expires_at_iso)
                            if now_utc > expires_at:
                                expired = True
                        
                        if expired:
                            logger.info(f"[{self.short_id}] Aegis Healing: Active override has expired! Checking gateway schedule for healing.")
                            tou_res = await self.get_tou_schedule()
                            if tou_res.get("ok"):
                                detail_obj = tou_res.get("detail") or {}
                                res_data = detail_obj.get("result") or {}
                                strategy_list = res_data.get("strategyList") or []

                                # Record a schedule that changed outside this
                                # app — almost always the FranklinWH app, which
                                # is where tariff and TOU settings are usually
                                # edited. Until now every snapshot came from a
                                # write FWHAI made itself, so such a change left
                                # no trace and local state went stale silently.
                                try:
                                    from src.services import db as _db
                                    await _db.record_observed_tou(self.short_id, strategy_list)
                                except Exception:
                                    logger.debug(
                                        "[%s] observed-TOU check failed", self.short_id,
                                        exc_info=True,
                                    )

                                is_override_active = False
                                for season in strategy_list:
                                    for day_type in season.get("dayTypeVoList", []):
                                        for period in day_type.get("detailVoList", []):
                                            if period.get("briefDescribe") == "HEMS_OVERRIDE_ACTIVE" or period.get("rampTime") == 99:
                                                is_override_active = True
                                                break
                                
                                if not is_override_active:
                                    default_vo = res_data.get("detailDefaultVo") or {}
                                    for period in default_vo.get("touDispatchList") or []:
                                        if period.get("briefDescribe") == "HEMS_OVERRIDE_ACTIVE" or period.get("rampTime") == 99:
                                            is_override_active = True
                                            break
                                            
                                if is_override_active:
                                    logger.warning(f"[{self.short_id}] Aegis Healing: Gateway schedule still has override active after expiration! Reverting to baseline.")
                                    baseline_list = json.loads(snapshot_json)
                                    restore_res = await self.set_tou_schedule_multi(baseline_list)
                                    if restore_res.get("ok"):
                                        logger.info(f"[{self.short_id}] Aegis Healing: Baseline schedule successfully restored on gateway.")
                                        await _db.upsert_smart_dispatch_config(
                                            self.short_id,
                                            baseline_tou_snapshot=None,
                                            active_override_uuid=None,
                                            active_override_expires_at=None
                                        )
                                        from src.services.db import log_admin_audit
                                        await log_admin_audit(
                                            event="Aegis Restoration",
                                            source="system",
                                            user="system",
                                            details=f"🛡️ Aegis Healing applied: successfully restored original baseline TOU schedule on gateway {self.short_id}."
                                        )
                                    else:
                                        logger.error(f"[{self.short_id}] Aegis Healing: Revert attempt failed: {restore_res.get('error')}")
                                else:
                                    logger.info(f"[{self.short_id}] Aegis Healing: Gateway schedule already restored. Clearing DB snapshot columns.")
                                    await _db.upsert_smart_dispatch_config(
                                        self.short_id,
                                        baseline_tou_snapshot=None,
                                        active_override_uuid=None,
                                        active_override_expires_at=None
                                    )
                except Exception as aegis_exc:
                    logger.error(f"[{self.short_id}] Aegis Healing drift verification failed: {aegis_exc}")

                # ── TOU anomaly detection (Gap 3) — LOG ONLY, never auto-reset ──
                # Cookbook spec: calling reset_tou_mode() from a poll loop is prohibited.
                # This log is the signal for the user to trigger /schedule/health/reset manually.
                # This log is the signal for the user to trigger /schedule/health/reset manually.
                _tou_run_status = data.get("run_status", -1)
                _tou_block = data.get("tou_block") or {}
                _expected_dispatch = _tou_block.get("dispatch_code") if isinstance(_tou_block, dict) else None
                if _tou_run_status == 0 and _expected_dispatch in ("GRID_EXPORT", "GRID_CHARGE"):
                    logger.warning(
                        f"[{self.short_id}] TOU anomaly: gateway in Standby (run_status=0) "
                        f"during expected {_expected_dispatch} block — "
                        f"use Schedule tab \u2192 Reset TOU to recover."
                    )

                # Seed apower serial numbers into context so BMS slow-poll always has them
                _apwr_sns_data = data.get("apower_serial_numbers")
                if _apwr_sns_data and not self.context.get("apower_serial_numbers"):
                    if isinstance(_apwr_sns_data, list):
                        self.context["apower_serial_numbers"] = _apwr_sns_data
                    elif isinstance(_apwr_sns_data, str):
                        try:
                            import json as _j
                            self.context["apower_serial_numbers"] = _j.loads(_apwr_sns_data)
                        except Exception:
                            self.context["apower_serial_numbers"] = [_apwr_sns_data]


                # ── System Orchestrator: Manual Lock Watchdog ──────────────────
                from src.main import get_app_state
                registry = get_app_state().get("registry")
                if registry:
                    lock = registry.get_exclusive_lock(self.short_id)
                    if lock and lock.get("type") == "manual_dispatch":
                        # If the underlying cloud dispatch is no longer active, clear the lock.
                        # Add a 60s grace period to allow Cloud API to acknowledge the new command.
                        lock_age = time.time() - lock.get("at", 0)
                        if not self.cloud_dispatch.is_active and lock_age > 60:
                            logger.info(f"[{self.short_id}] Manual dispatch ended (expired) — clearing registry lock.")
                            registry.clear_exclusive_lock(self.short_id)

                # Append connection diagnostics to payload
                data["api_connection_status"] = self.status.poll_status
                data["api_last_error"] = ""
                import datetime
                if self.status.last_error_time:
                    data["api_error_time"] = datetime.datetime.fromtimestamp(self.status.last_error_time).isoformat()
                else:
                    data["api_error_time"] = ""

                # Fan-out: pass full_serial so publisher uses correct topic segment
                await self._store_metrics(data)
                if self._on_data:
                    await self._on_data(self.full_serial, data)

                # Feature (Solar Setup): Auto-detect MPPT and aPBox Solar
                solar_hw = data.get("solar_hardware", {})
                if not getattr(self, "_mppt_auto_registered", False) and solar_hw.get("mppt_en_flag"):
                    from src.services.db import auto_register_hardware_solar
                    asyncio.create_task(auto_register_hardware_solar(self.short_id, "mppt"))
                    self._mppt_auto_registered = True
                
                if not getattr(self, "_apbox_auto_registered", False) and (solar_hw.get("remote_solar_mode", 0) > 0 or solar_hw.get("remote_solar_enabled")):
                    from src.services.db import auto_register_hardware_solar
                    asyncio.create_task(auto_register_hardware_solar(self.short_id, "apbox"))
                    self._apbox_auto_registered = True


                # Phase 105 & 109 & 117: Zero-Touch HA MQTT Hub Bootstrapping
                if not self.status.mqtt_published:
                    try:
                        from src.main import get_app_state
                        publisher = get_app_state().get("publisher")
                        if publisher:
                            from src.services.db import get_config_value
                            import pathlib
                            uid = await get_config_value("fhai_instance_label", "FHAI")
                            publisher.instance_uid = uid
                            app_version = "unknown"
                            try:
                                app_version = pathlib.Path("/app/VERSION").read_text().strip()
                            except Exception:
                                pass
                                
                            dev_info = results.get("device_info", {})
                            model = dev_info.get("deviceModel", "aGate")
                            fw = dev_info.get("firmwareVersion", "")
                            
                            publisher.publish_device_info(
                                self.full_serial, 
                                self.context.get("name", ""), 
                                model, fw, app_version
                            )
                            
                            # Build dynamic TOU preset options for MQTT discovery
                            _presets_mgr = get_app_state().get("schedule_presets")
                            _tou_options = ["Stop / Restore"]
                            if _presets_mgr is not None:
                                try:
                                    _tou_options += [p["name"] for p in _presets_mgr.list_presets() if not p.get("unverified")]
                                except Exception:
                                    pass

                            # The device block's firmware comes from
                            # profile["firmware"], which is the *cloud* profile's
                            # key and is often absent — so Home Assistant showed
                            # "Firmware: FHAI: v0.6.46", this integration's own
                            # version, while the gateway's real firmware sat in
                            # context["firmware_version"] and rendered correctly
                            # on the Firmware Version entity beside it. Pass the
                            # resolved value rather than hoping the raw profile
                            # carries it. Copied, not mutated: the context
                            # profile is shared.
                            _disc_profile = dict(self.context.get("profile") or {})
                            if not (_disc_profile.get("firmware") or "").strip():
                                _resolved_fw = (self.context.get("firmware_version") or "").strip()
                                if _resolved_fw:
                                    _disc_profile["firmware"] = _resolved_fw

                            # "AGT-R1V1-AU · 1× APR-05K13V1-AU" — the aGate's
                            # SKU and the aPower fleet, resolved where the
                            # catalog already is rather than in the publisher.
                            # Same again for the model. The device read
                            # "aGate" — the bare fallback — because
                            # profile["model"] is empty while the resolved
                            # "aGate X-01-AU (AGT-R1V1-AU)" sits in
                            # device_model_full. Hand the publisher the combined
                            # string and let it split model from SKU, which is
                            # the FEM rule it already applies.
                            if not (_disc_profile.get("model") or "").strip():
                                _model_full = (self.context.get("device_model_full") or "").strip()
                                if _model_full and _model_full != "aGate":
                                    _disc_profile["model"] = _model_full

                            _hw_summary = (self.context.get("hw_summary") or "").strip()
                            if _hw_summary:
                                _disc_profile["hw_summary"] = _hw_summary

                            # Whether the user has forced a hardware gate on or
                            # off. Read here because publish_discovery is
                            # synchronous and these live in the database.
                            try:
                                from src.services.hw_gates import load_overrides
                                _disc_profile["_gate_overrides"] = await load_overrides()
                            except Exception:
                                logger.debug(f"[{self.short_id}] gate overrides unavailable", exc_info=True)

                            publisher.publish_discovery(
                                self.full_serial,
                                self.context.get("name", ""),
                                _disc_profile,
                                data.get("bms_units", []),
                                data,
                                tou_preset_options=_tou_options
                            )
                            try:
                                await publisher.publish_forecast_loads_discovery()
                            except Exception as fld_exc:
                                logger.error(f"[{self.short_id}] Failed to dispatch Forecast Loads HA Discovery payload: {fld_exc}")
                            self.status.mqtt_published = True
                    except Exception as pub_exc:
                        logger.error(f"[{self.short_id}] Failed to dispatch initial HA Discovery payload: {pub_exc}")

                await asyncio.sleep(self._poll_interval)

            except asyncio.CancelledError:
                raise  # propagate — task is being stopped

            except Exception as exc:
                # No credentials — suspend poll, no retry
                if str(exc) == "no_credentials":
                    self.status.poll_status = "no_credentials"
                    # Wait indefinitely — service must be restarted with new creds
                    try:
                        await asyncio.Future()  # yields forever until cancellation
                    except asyncio.CancelledError:
                        raise
                    return

                # Auth failures (401/403/Bad Password) — suspend poll to prevent lockout
                err_str = str(exc).lower()
                is_auth_error = any(kw in err_str for kw in ("401", "403", "unauthorized", "forbidden", "invalid password", "invalid credentials", "auth failed", "login fail"))
                
                if self._handle_token_expiration(exc):
                    pass # Handled by clearing client; continue straight to backoff sleep
                elif is_auth_error:
                    self.status.poll_status = "auth_failed"
                    logger.error(
                        f"[{self.short_id}] Authentication failed. Suspending poll to prevent account lockout. Error: {exc}"
                    )
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        raise
                    return

                self.status.consecutive_errors += 1
                self.status.last_error = str(exc)
                self.status.last_error_time = time.time()
                exc_sig = type(exc).__name__
                if self.status.consecutive_errors == 1:
                    self.status.first_error_time = time.time()
                    self.status.first_error_signature = exc_sig

                # Circuit Breaker implementation
                if self.status.consecutive_errors >= 5:
                    self.status.poll_status = "circuit_breaker_tripped"
                    burst_dur = int(time.time() - (self.status.first_error_time or time.time()))
                    logger.critical(
                        f"[{self.short_id}] Circuit Breaker Tripped: 5 consecutive failures "
                        f"over {burst_dur}s (last: {exc}). Suspending poll for 5 minutes."
                    )
                    try:
                        await asyncio.sleep(300)
                    except asyncio.CancelledError:
                        raise
                    # After cooldown, reset backoff to probe normally
                    backoff_idx = 0
                    self.status.consecutive_errors = 0
                    self.status.first_error_time = None
                    self.status.first_error_signature = None
                    continue

                wait = BACKOFF_STEPS[min(backoff_idx, len(BACKOFF_STEPS) - 1)]
                backoff_idx += 1

                if self.status.consecutive_errors == 1:
                    self.status.poll_status = "error"
                else:
                    self.status.poll_status = "retrying"

                # Log volume control: ERROR on first failure, DEBUG on subsequent
                # retries when the exception type is unchanged. A recovery summary
                # (logged from the success path) or the circuit-breaker CRITICAL
                # always closes out the burst, so nothing is silently lost.
                same_sig = exc_sig == self.status.first_error_signature
                if self.status.consecutive_errors == 1 or not same_sig:
                    logger.error(
                        f"[{self.short_id}] Poll error (attempt {self.status.consecutive_errors}, "
                        f"type={exc_sig}): {exc}. Retrying in {wait}s"
                    )
                else:
                    logger.debug(
                        f"[{self.short_id}] Poll error (attempt {self.status.consecutive_errors}, "
                        f"same type={exc_sig}): {exc}. Retrying in {wait}s"
                    )
                
                # Force emit the current error snapshot so HA dashboard displays it instantly
                if self._on_data:
                    err_data = self.status.last_data.copy() if self.status.last_data else {}
                    err_data["api_connection_status"] = self.status.poll_status
                    err_data["api_last_error"] = self.status.last_error or ""
                    import datetime
                    if self.status.last_error_time:
                        err_data["api_error_time"] = datetime.datetime.fromtimestamp(self.status.last_error_time).isoformat()
                    else:
                        err_data["api_error_time"] = ""
                    await self._on_data(self.full_serial, err_data)

                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    raise

    async def _poll_once(self) -> dict:
        """Call Cloud API and return a normalised data dict."""
        client = await self._get_or_create_client()
        stats = await client.get_stats()
        results = {"stats": stats}
        results.update(self._client_cache)
        data = self._normalise_stats(results)

        # Never let a degraded snapshot replace good telemetry.
        #
        # This path is the out-of-cycle poll behind POST /gateways/{id}/poll,
        # which EVERY page load fires via app.js refreshGateway(). It builds
        # `results` from get_stats() plus whatever _client_cache happens to
        # hold — not the full sweep the scheduled loop performs. When either
        # source comes back thin, _normalise_stats yields a snapshot with no
        # batteries, and this assignment used to publish it unconditionally,
        # blanking the cached telemetry until the next scheduled poll.
        #
        # Measured before this guard: baseline soc=42.4/count=1, POST /poll
        # returns 200 in 0.42s, then /data reports soc=0.0/count=0 for ~12-15s
        # before recovering. Downstream that is GH #6 — the Control tab reads
        # once on load, caches the zeros, and has no timer to re-read, so it
        # sits at 0% while the top nav (bound to the store, which keeps
        # polling) corrects itself seconds later. Exactly the reported
        # "Control tab and top nav disagree".
        #
        # battery_count is the honest discriminator: a flat battery still
        # reports count>=1, so a drop to 0 means the payload lost the aPower
        # inventory rather than the battery going away. A genuine 0% SoC is
        # still published normally.
        prev = self.status.last_data or {}

        # Primary: honour the canonical Batch I stale-window detector. The
        # scheduled poll loop has checked this since Batch I (see the drop
        # path near line 571) but this out-of-cycle path never did, so a
        # forced poll could publish a payload the scheduled loop would have
        # rejected. One detector, both paths.
        if isinstance(data, dict) and data.get("_fhai_suspect_stale_window"):
            self.status.poll_status = "stale_window_dropped"
            logger.warning(
                f"[{self.short_id}] _poll_once: dropping stale-window payload — "
                f"{data.get('_fhai_suspect_stale_window')}"
            )
            return prev or data

        # Secondary: inventory loss. Distinct from the detector above, which
        # keys off SoC sentinels — this catches a payload that kept a
        # plausible SoC but lost the aPower list entirely.
        prev_count = ((prev.get("capacity") or {}).get("battery_count")) or 0
        new_count = ((data.get("capacity") or {}).get("battery_count")) or 0
        if prev_count > 0 and new_count == 0:
            logger.warning(
                f"[{self.short_id}] _poll_once: discarding degraded snapshot "
                f"(battery_count {prev_count} -> 0); keeping previous telemetry"
            )
            return prev

        self.status.last_data = data
        self.status.last_data_at = time.time()   # GH #35
        return data
    def _dataclass_to_dict(self, obj: Any) -> Any:
        try:
            from enum import Enum
            if hasattr(obj, "__dataclass_fields__"):
                return {
                    f.name: self._dataclass_to_dict(getattr(obj, f.name))
                    for f in getattr(obj, "__dataclass_fields__").values()
                }
            if isinstance(obj, list):
                return [self._dataclass_to_dict(i) for i in obj]
            if isinstance(obj, dict):
                return {k: self._dataclass_to_dict(v) for k, v in obj.items()}
            if hasattr(obj, "_asdict"):  # NamedTuple support
                return self._dataclass_to_dict(obj._asdict())
            if isinstance(obj, Enum):
                return obj.value  # Use .value ('Connected', 'Outage', etc.) not .name ('CONNECTED')
            return obj
        except Exception:
            return str(obj)

    _WORK_MODE_DESC_MAP = {
        1: "Time-of-Use",
        2: "Self-Consumption",
        3: "Emergency Backup",
        4: "Off Grid",
    }

    def _get_work_mode_desc(self, work_mode) -> str:
        """Translate a work_mode integer to a human-readable description."""
        try:
            return self._WORK_MODE_DESC_MAP.get(int(work_mode), f"Mode {work_mode}")
        except (TypeError, ValueError):
            return str(work_mode)

    def _normalise_stats(self, results: dict) -> dict:
        """
        Convert combined franklinwh-cloud nested structures to a flat dict.
        Extracts core stats, smart circuits, and generator into a shared key-value root.
        """
        stats = results.get("stats", {})
        
        if hasattr(stats, "__dict__") or hasattr(stats, "__dataclass_fields__"):
            d = self._dataclass_to_dict(stats)
        elif isinstance(stats, dict):
            d = stats.copy()
        else:
            d = {"raw": str(stats)}

        # Pass through apower_info from results → last_data so the Battery tab UI can
        # display per-unit firmware versions, ratedCapacity, remainingPower, etc.
        # apower_info is fetched at counter==1 (startup) and cached in _client_cache.
        if "apower_info" in results and results["apower_info"] is not None:
            d["apower_info"] = results["apower_info"]

        # Flatten franklinwh-cloud v0.2.0 Stats structure if present
        if "current" in d and isinstance(d["current"], dict):
            merged = {}
            if "totals" in d and isinstance(d["totals"], dict):
                merged.update(d["totals"])
                
                # Expose specific safe mapped keys for Energy Parity
                d["home_load_today"] = d["totals"].get("home_load", 0)
                d["grid_import_today"] = d["totals"].get("grid_import", 0)
                d["grid_export_today"] = d["totals"].get("grid_export", 0)
                d["generator_today"] = d["totals"].get("generator", 0)
                d["solar_today"] = d["totals"].get("solar", 0)
                
            merged.update(d["current"])
            # Update root level mapped keys safely over the merged product
            merged["home_load_today"] = d.get("home_load_today", 0)
            merged["grid_import_today"] = d.get("grid_import_today", 0)
            merged["grid_export_today"] = d.get("grid_export_today", 0)
            merged["generator_today"] = d.get("generator_today", 0)
            merged["solar_today"] = d.get("solar_today", 0)
            # Preserve root-level fields not in current/totals (e.g. network_connection,
            # off_grid_mode, battery_soc, mode, apower_serial_numbers, etc.) so they
            # survive the flatten. current/totals keys take priority.
            _SKIP_ROOT = {"current", "totals"}
            for _k, _v in d.items():
                if _k not in _SKIP_ROOT and _k not in merged:
                    merged[_k] = _v
            d = merged

        # Weather and Storm Telemetry Integration
        weather_raw = results.get("weather")
        weather_dict = self._dataclass_to_dict(weather_raw) if not isinstance(weather_raw, dict) else weather_raw
        if isinstance(weather_dict, dict):
            temp = weather_dict.get("temperature") or weather_dict.get("temp") or weather_dict.get("tempC") or 0.0
            humidity = weather_dict.get("humidity") or 0
            condition = weather_dict.get("condition") or weather_dict.get("weather") or weather_dict.get("status") or "Unknown"
            icon = weather_dict.get("weatherIcon") or weather_dict.get("icon") or ""
            pressure = weather_dict.get("pressure") or weather_dict.get("barometric_pressure") or weather_dict.get("pressureHpa") or 1013
            
            d["weather_temp"] = float(temp)
            d["weather_humidity"] = int(humidity)
            d["weather_condition"] = str(condition)
            d["weather_icon"] = str(icon)
            d["weather_pressure"] = int(pressure)
        else:
            d["weather_temp"] = 0.0
            d["weather_humidity"] = 0
            d["weather_condition"] = "Unknown"
            d["weather_icon"] = ""
            d["weather_pressure"] = 1013

        storms_raw = results.get("progressing_storms")
        storms_dict = self._dataclass_to_dict(storms_raw) if not isinstance(storms_raw, dict) else storms_raw
        active_storm_count = 0
        if isinstance(storms_dict, dict):
            storm_list = storms_dict.get("list") or storms_dict.get("storms")
            if isinstance(storm_list, list):
                active_storm_count = len(storm_list)
            else:
                active_storm_count = storms_dict.get("count") or storms_dict.get("total") or 0
                try:
                    active_storm_count = int(active_storm_count)
                except (TypeError, ValueError):
                    active_storm_count = 0
        elif isinstance(storms_dict, list):
            active_storm_count = len(storms_dict)
            
        d["active_storm_count"] = active_storm_count
        d["active_storm_warning"] = 1 if active_storm_count > 0 else 0
        
        if "gen_info" in d and isinstance(d["gen_info"], dict):
            gi = d["gen_info"]
            # 0=Off/Auto Schedule? API: 1=Auto, 2=Manual On, 0=Off.
            raw_mode = gi.get("generatorMode", 0)
            d["generator_mode"] = "Auto Schedule" if raw_mode == 1 else "Manual"
            d["generator_running"] = "ON" if raw_mode == 2 else "OFF"
            d["generator_start_soc"] = gi.get("socLowThreshold", 20)
            d["generator_stop_soc"] = gi.get("socHighThreshold", 80)
            d["generator_enabled"] = gi.get("generatorInstalled", False)
            
        if "sc_info" in d and isinstance(d["sc_info"], dict):
            switches = d.sc_info.get("switchs", []) if hasattr(d, "sc_info") else d["sc_info"].get("switchs", [])
            for i, sw in enumerate(switches):
                idx = i + 1
                state_str = "ON" if sw.get("state", 0) == 1 else "OFF"
                mode_str = "Schedule" if sw.get("workMode", 0) == 1 else "Manual"
                d[f"smart_circuit_{idx}"] = state_str
                d[f"smart_circuit_{idx}_mode"] = mode_str
                
        # Status mappings compatibility for HA Integrator format (dashboard + mqtt)
        if "battery_kw" not in d and "battery_use" in d:
            d["battery_kw"] = d.get("battery_use")
        if "home_kw" not in d and "home_load" in d:
            d["home_kw"] = d.get("home_load")
        if "solar_kw" not in d and "solar_production" in d:
            d["solar_kw"] = d.get("solar_production")
        if "grid_kw" not in d and "grid_use" in d:
            d["grid_kw"] = d.get("grid_use")
        # Phase 16: Additional deep-parsing rules (flattening integration complexity)
        # 1. Mode string translations
        mode_dict = d.get("mode", {})
        # raw_mode merging removed since work_mode is reliably provided by get_stats()

        if "work_mode" in mode_dict:
            mode_dict["work_mode_desc"] = self._get_work_mode_desc(mode_dict["work_mode"])
            
        # Phase 103: 'Runtime Mode' Parity Synthesis
        # Derived strictly from work_mode_desc vs running status to mirror the mobile app exactly.
        
        # Ensure mode_dict has fallback from root if absent
        if "work_mode" not in mode_dict and "work_mode" in d:
            mode_dict["work_mode"] = d["work_mode"]
            
        if "work_mode_desc" not in mode_dict and "work_mode_desc" in d:
            mode_dict["work_mode_desc"] = d["work_mode_desc"]
            
        if "work_mode" in mode_dict and "work_mode_desc" not in mode_dict:
            mode_dict["work_mode_desc"] = self._get_work_mode_desc(mode_dict["work_mode"])

        # DEF-OPMODE-CANONICAL: Always re-derive work_mode_desc from the integer using
        # _WORK_MODE_DESC_MAP to get canonical hyphenated form ("Time-of-Use" not
        # "Time of Use"). The cloud library returns space-separated strings which don't
        # match the HA select entity options, causing HA to show "unknown".
        _wm_int_raw = mode_dict.get("work_mode") or d.get("work_mode")
        try:
            _wm_int_raw = int(_wm_int_raw)
        except (TypeError, ValueError):
            _wm_int_raw = 0
        if _wm_int_raw in self._WORK_MODE_DESC_MAP:
            mode_dict["work_mode_desc"] = self._WORK_MODE_DESC_MAP[_wm_int_raw]
            mode_dict["work_mode"] = _wm_int_raw

        # DEF-OPMODE-FIX: Cache last known valid work_mode_desc so it persists across polls
        # where work_mode=0 (empty_stats fallback) or is temporarily absent after a mode switch.
        _wm_int_now = mode_dict.get("work_mode", 0)
        _wm_desc_now = mode_dict.get("work_mode_desc", "")
        try:
            _wm_int_now = int(_wm_int_now)
        except (TypeError, ValueError):
            _wm_int_now = 0
        if _wm_int_now in (1, 2, 3, 4):
            self.context["last_work_mode_desc"] = _wm_desc_now
            self.context["last_work_mode"] = _wm_int_now
        elif not _wm_desc_now or _wm_desc_now.startswith("Mode ") or _wm_desc_now == "Unknown":
            _cached_desc = self.context.get("last_work_mode_desc")
            _cached_mode = self.context.get("last_work_mode")
            if _cached_desc:
                mode_dict["work_mode_desc"] = _cached_desc
            if _cached_mode:
                mode_dict["work_mode"] = _cached_mode

        # VPP detection — franklinwh_cloud/const/modes.py RUN_STATUS:
        # run_status_desc is derived from runtimeData.mode (NOT runtimeData.run_status).
        # franklinwh-cloud now also provides `effective_mode` which mirrors the Frank app label.
        effective_mode = d.get("effective_mode", "")
        run_status_str = d.get("run_status_desc") or d.get("run_status_dec", "")
        
        # Phase 108: Reconcile Mode with Manual Dispatch Lock
        from src.main import get_app_state
        registry = get_app_state().get("registry")
        manual_lock = registry.get_exclusive_lock(self.short_id) if registry else None
        
        if (effective_mode and effective_mode.lower() == "vpp mode") \
                or str(run_status_str).lower() == "vpp mode":
            mode_dict["runtime_mode"] = "VPP Mode"
            d["run_status"] = 9
            d["run_status_desc"] = "VPP mode"
            # VPP is a deterministic override — clear any pending native-mode
            # hysteresis candidate so the debounce state doesn't stale-fire
            # once VPP releases (backlog 2026-07-12: SC↔TOU oscillation).
            self.context.pop("_rt_native_candidate", None)
            self.context.pop("_rt_native_candidate_count", None)
        elif manual_lock and manual_lock.get("type") == "manual_dispatch":
            action = manual_lock.get("value", "Active")
            mode_dict["runtime_mode"] = f"Manual {action}"
            self.context.pop("_rt_native_candidate", None)
            self.context.pop("_rt_native_candidate_count", None)
            
            # Read target power from context cache (ha_input_cache.dispatch_power or default to 2.0)
            transient_cache = self.context.get("ha_input_cache", {})
            dispatch_power = float(transient_cache.get("dispatch_power", 2.0))
            
            if action == "Charge":
                d["battery_kw"] = -dispatch_power
                d["battery_use"] = -dispatch_power
                d["run_status"] = 1
                d["run_status_desc"] = "Charging"
            elif action == "Discharge":
                d["battery_kw"] = dispatch_power
                d["battery_use"] = dispatch_power
                d["run_status"] = 2
                d["run_status_desc"] = "Discharging"
        else:
            # Prioritize dedicated mode polling (work_mode_desc) over the potentially stale effective_mode
            _raw_native = mode_dict.get("work_mode_desc") or effective_mode or "Unknown"
            # Hysteresis (Batch H, backlog 2026-07-12) — the FranklinWH cloud API
            # returns racy snapshots that can flip runtime_mode between values
            # like "Self-Consumption" and "Time-of-Use" on adjacent polls even
            # when the gateway hasn't actually changed mode. Sibling
            # franklinwh-cloud HA integration republishes each flip as a sensor
            # state change (evidence: 98/98 SC↔TOU oscillation over 24h, median
            # gap 200s). Debounce here so FHAI's OWN runtime_mode sensor + the
            # SD engine's VPP gate (Batch G) don't over-react to transient flips.
            #
            # Rule: require N consecutive polls agreeing on a NEW value before
            # promoting it to the published state. VPP + Manual paths above
            # bypass this (deterministic overrides). "Unknown" bypasses too
            # (we never want to promote Unknown as a stable state).
            #
            # N defaults to 3 (configurable via FHAI_RT_MODE_STABLE_POLLS env);
            # at ~30-60s poll cadence that's 90-180s lag on genuine changes
            # while blocking single-poll flickers entirely.
            import os as _os
            try:
                _stable_n = max(1, int(_os.environ.get("FHAI_RT_MODE_STABLE_POLLS", "3")))
            except (TypeError, ValueError):
                _stable_n = 3
            _published = self.context.get("_rt_native_published")
            if _raw_native == "Unknown":
                # Never publish Unknown as a stable state — keep the last
                # known published value (falls through to raw only on
                # first boot with no prior state).
                _out = _published or _raw_native
            elif _published is None:
                # First observation — latch it as the published state so
                # subsequent stable polls are "same as published" (no
                # spurious candidate accumulation).
                self.context["_rt_native_published"] = _raw_native
                self.context.pop("_rt_native_candidate", None)
                self.context.pop("_rt_native_candidate_count", None)
                _out = _raw_native
            elif _raw_native == _published:
                # Steady state — clear any candidate, publish as-is.
                self.context.pop("_rt_native_candidate", None)
                self.context.pop("_rt_native_candidate_count", None)
                _out = _published
            else:
                _candidate = self.context.get("_rt_native_candidate")
                if _candidate == _raw_native:
                    _cnt = int(self.context.get("_rt_native_candidate_count", 1)) + 1
                    if _cnt >= _stable_n:
                        # Candidate has stabilised — promote
                        self.context["_rt_native_published"] = _raw_native
                        self.context.pop("_rt_native_candidate", None)
                        self.context.pop("_rt_native_candidate_count", None)
                        _out = _raw_native
                    else:
                        self.context["_rt_native_candidate_count"] = _cnt
                        _out = _published
                else:
                    # New candidate — start counting from 1, keep publishing prior
                    self.context["_rt_native_candidate"] = _raw_native
                    self.context["_rt_native_candidate_count"] = 1
                    _out = _published
                    logger.debug(
                        f"[{self.short_id}] runtime_mode hysteresis: "
                        f"raw={_raw_native!r} candidate (need {_stable_n} consecutive), "
                        f"keeping published={_published!r}"
                    )
            mode_dict["runtime_mode"] = _out

        # Inject operating_mode_id for easier UI consumption
        d["operating_mode_id"] = _wm_int_now

        # Phase D4: off_grid_mode — derived from stats root field
        off_grid_raw = d.get("off_grid_mode") or d.get("off_grid")
        mode_dict["off_grid"] = "ON" if off_grid_raw in (1, True, "true", "1", "ON", "on") else "OFF"

        d["mode"] = mode_dict
        # DEF-OPMODE-FIX: operating_mode entity should carry the text description, not raw int
        d["operating_mode"] = mode_dict.get("work_mode_desc") or d.get("work_mode_desc") or self.context.get("last_work_mode_desc", "Unknown")
            
        # Phase 107 + D6: Transient Compound States Boot Initialization (defaults aligned to FEM)
        transient_cache = self.context.get("ha_input_cache", {})
        d["dispatch_duration"] = transient_cache.get("dispatch_duration", 60)       # FEM default: 60 min
        d["dispatch_target_soc"] = transient_cache.get("dispatch_target_soc", 100)
        d["dispatch_power"] = transient_cache.get("dispatch_power", 2.0)            # FEM default: 2.0 kW
        d["dispatch_method"] = transient_cache.get("dispatch_method", "Cloud TOU")
        d["emergency_backup_duration"] = transient_cache.get("emergency_backup_duration", 60)
        d["emergency_backup_duration_type"] = transient_cache.get("emergency_backup_duration_type", "Indefinite")
        d["emergency_backup_resume_mode"] = transient_cache.get("emergency_backup_resume_mode", "Self-Consumption")
        d["tou_saved_dispatches"] = transient_cache.get("tou_saved_dispatches", "Stop / Restore")
            
        # Extract abstracted switch power from stats root first so it is available for fallback logic
        for i in range(1, 4):
            if d.get(f"switch_{i}_load") is not None:
                d[f"smart_circuit_{i}_power"] = d.get(f"switch_{i}_load", 0)
                d[f"smart_circuit_{i}_energy"] = d.get(f"switch_{i}_use", 0)
            if d.get(f"switch_{i}_state") is not None:
                d[f"smart_circuit_{i}_state"] = "ON" if d.get(f"switch_{i}_state") in (1, "1", "ON", True) else "OFF"
            
        # Parity flatteners for Cloud-only accessories
        if "smart_circuits" in results:
            s = self._dataclass_to_dict(results["smart_circuits"]) if not isinstance(results["smart_circuits"], dict) else results["smart_circuits"]
            if isinstance(s, dict):
                # Support V3 (franklinwh-cloud abstracted object mode), V1 (switchs array), and V2 (flat dict)
                if "1" in s and isinstance(s["1"], dict) and "name" in s["1"]:
                    for n in range(1, 4):
                        str_n = str(n)
                        if str_n in s:
                            c = s[str_n]
                            d[f"smart_circuit_{n}_name"] = c.get("name", f"Circuit {n}")
                            raw_mode = c.get("mode", 0)
                            # Ensure we map API mode 1 to 'Manual' and 0 to 'Schedule'
                            d[f"smart_circuit_{n}_mode"] = "Manual" if str(raw_mode) == "1" else "Schedule"
                            if f"smart_circuit_{n}_state" not in d:
                                d[f"smart_circuit_{n}_state"] = "ON" if c.get("is_on") else "OFF"
                            d[f"smart_circuit_{n}_soc_cutoff_enabled"] = c.get("soc_cutoff_enabled", False)
                            d[f"smart_circuit_{n}_soc_cutoff_limit"] = c.get("soc_cutoff_limit", 0)
                            d[f"smart_circuit_{n}_pro_load_type"] = c.get("pro_load_type", 0)
                            d[f"smart_circuit_{n}_time_enabled"] = c.get("time_enabled", [])
                            d[f"smart_circuit_{n}_time_schedules"] = c.get("time_schedules", [])
                            d[f"smart_circuit_{n}_time_set"] = c.get("time_set", [])
                elif "switchs" in s or "switches" in s:
                    sw_list = s.get("switches", s.get("switchs", []))
                    for idx, c in enumerate(sw_list):
                        n = idx + 1
                        if f"smart_circuit_{n}_state" not in d:
                            d[f"smart_circuit_{n}_state"] = "ON" if c.get("state") in (1, "1", "ON", True) else "OFF"
                        d[f"smart_circuit_{n}_mode"] = "Schedule" if str(c.get("workMode")) == "1" else "Manual"
                        if "swName" in c:
                            d[f"smart_circuit_{n}_name"] = c.get("swName")
                else:
                    for n in range(1, 4):
                        if f"Sw{n}Name" in s or d.get(f"smart_circuit_{n}_power") is not None:
                            d[f"smart_circuit_{n}_name"] = s.get(f"Sw{n}Name", f"Circuit {n}")
                            raw_mode = s.get(f"Sw{n}Mode", 0)
                            # Ensure we map API mode 1 to 'Manual' and 0 to 'Schedule' so the frontend buttons highlight properly
                            d[f"smart_circuit_{n}_mode"] = "Manual" if str(raw_mode) == "1" else "Schedule"
                            
                            # Apply CLI abstractions
                            d[f"smart_circuit_{n}_soc_cutoff_enabled"] = s.get(f"Sw{n}AtuoEn", 0) == 1
                            d[f"smart_circuit_{n}_soc_cutoff_limit"] = s.get(f"Sw{n}SocLowSet", 0)
                            d[f"smart_circuit_{n}_pro_load_type"] = s.get(f"Sw{n}ProLoad", 0)
                            
                            # V2 Time Schedules
                            d[f"smart_circuit_{n}_time_enabled"] = s.get(f"Sw{n}TimeEn")
                            d[f"smart_circuit_{n}_time_schedules"] = s.get(f"Sw{n}Time")
                            d[f"smart_circuit_{n}_time_set"] = s.get(f"Sw{n}TimeSet")

                            # Fallback state inference if exact logic state isn't in flat payload
                            if f"smart_circuit_{n}_state" not in d:
                                raw_state = s.get(f"Sw{n}State")
                                if raw_state is not None:
                                    d[f"smart_circuit_{n}_state"] = "ON" if str(raw_state) == "1" else "OFF"
                                else:
                                    # If power is > 0, it's definitely ON.
                                    # Also match CLI logic: if mode is 1 (Always ON), it is ON.
                                    pwr = d.get(f"smart_circuit_{n}_power", 0)
                                    if str(raw_mode) == "1" or pwr > 0:
                                        d[f"smart_circuit_{n}_state"] = "ON"
                                    else:
                                        d[f"smart_circuit_{n}_state"] = "OFF"

                
        if "generator" in results:
            g = self._dataclass_to_dict(results["generator"]) if not isinstance(results["generator"], dict) else results["generator"]
            if isinstance(g, dict):
                d["generator_enabled"] = g.get("enable")
                d["generator_mode"] = g.get("generatorMode")
                d["gen_start_soc"] = g.get("socLowThreshold")
                d["gen_stop_soc"] = g.get("socHighThreshold")
            
        if "power_settings" in results:
            p_obj = self._dataclass_to_dict(results["power_settings"]) if not isinstance(results["power_settings"], dict) else results["power_settings"]
            if isinstance(p_obj, dict):
                p = p_obj.get("result", p_obj)  # handle if result is wrapped
                # FEM pattern (service_engine.py:1291-1298):
                # globalGridChargeMax / globalGridDischargeMax:
                #   -1   = unlimited  → switch ON
                #    0   = disabled/capped at 0 → switch OFF
                #   >0   = explicit kW limit   → switch OFF
                if "globalGridChargeMax" in p:
                    _charge_max = float(p["globalGridChargeMax"])
                    d["grid_import_limit"] = 0 if _charge_max <= 0 else round(_charge_max, 2)
                    d["grid_import_unlimited"] = (_charge_max == -1.0)
                if "globalGridDischargeMax" in p:
                    _discharge_max = float(p["globalGridDischargeMax"])
                    d["grid_export_limit"] = 0 if _discharge_max <= 0 else round(_discharge_max, 2)
                    d["grid_export_unlimited"] = (_discharge_max == -1.0)
                
        # Remove redundant block that I accidentally left
        if "accessories_power" in results:
            ap = self._dataclass_to_dict(results["accessories_power"]) if not isinstance(results["accessories_power"], dict) else results["accessories_power"]
            if isinstance(ap, dict):
                if "SW3Curr" in ap:
                    d["smart_circuit_3_power"] = ap.get("SW3ExpPower", 0)  # Assuming SW3 prefix if FranklinWH adds it soon
                    d["smart_circuit_3_energy"] = ap.get("SW3ExpEnergy", 0)
                if "genpowerGen" in ap:
                    d["generator_power"] = ap.get("genpowerGen", 0)
                if "CarSWCurr" in ap:
                    d["v2l_power"] = ap.get("CarSWPower", 0)
                    d["v2l_state"] = 1 if ap.get("CarSWPower", 0) > 0 else 0
                
        # Phase 16 Full Cloud Parity Restructuring

        # DEF-NETWORK-FIX v2: network_connection field uses franklinwh-cloud NETWORK_TYPES encoding.
        # Confirmed by live raw value 3=WiFi from franklinwh-cli raw get_stats.
        # Correct map: {1:Ethernet 1, 2:Ethernet 2, 3:WiFi, 4:4G Mobile} (matches devices.py)
        _NETWORK_TYPE_MAP = {1: "Ethernet", 2: "Ethernet 2", 3: "WiFi", 4: "4G Mobile"}
        _raw_net = d.get("network_connection")
        _net_label = _NETWORK_TYPE_MAP.get(_raw_net, str(_raw_net) if _raw_net is not None else "Unknown")

        # 1. Agate Diagnostics
        d["agate"] = {
            "wifi_signal": d.get("wifi_signal"),
            "mobile_signal": d.get("mobile_signal"),
            "network_connection": _net_label,
            "ambient_temp": d.get("agate_ambient_temparture"),
        }
        
        # 2. Status & Hardware Definitions
        import datetime as _dt
        # Phase 0: is_three_phase_install extraction
        _curr_stats = d.get("current", {}) if isinstance(d.get("current"), dict) else d
        d["is_three_phase_install"] = _curr_stats.get("isThreePhaseInstall", False)

        # DEF-FIRMWARE/MODEL-FIX: Both firmware and model come from gateway list (seeded at startup).
        # getDeviceInfoV2 has NO deviceModel or firmwareVersion keys.
        # di is assigned here first as it's needed for updateFlag, softwareVersion, etc.
        di = self._dataclass_to_dict(results.get("device_info", {})) if not isinstance(results.get("device_info", {}), dict) else results.get("device_info", {})
        _full_model = self.context.get("device_model_full", "aGate")
        _firmware = self.context.get("firmware_version") or di.get("firmwareVersion")
        # Cloud SW version: seeded at startup from gateway list 'version' field.
        # di.get("softwareVersion") is always None — device_info API has no such key.
        # Context takes priority; di fallback kept for future API changes.
        _cloud_sw = self.context.get("cloud_software_version") or di.get("softwareVersion")

        # Map updateFlag (int/bool) to human label matching FEM
        _update_flag = di.get("updateFlag")
        if _update_flag is None:
            _update_label = self.context.get("update_available")  # use cached value if available
        elif _update_flag in (0, False, "0", "false"):
            _update_label = "Up to date"
        elif _update_flag in (1, True, "1", "true"):
            _update_label = "Update available"
        else:
            _update_label = str(_update_flag)
        if _update_label:
            self.context["update_available"] = _update_label


        d["device"] = {
            "model": _full_model,
            # Phase B: use startup-cached firmware as fallback if device_info not present this poll
            "firmware_version": _firmware,
            "cloud_software_version": _cloud_sw,
            "timezone": di.get("timeZone"),
            # Phase D: full serial from GatewayService.full_serial (always available)
            "serial_number": self.full_serial,
            # Sprint 4b: grid compliance profile from device_info (cached)
            "grid_profile_name": di.get("gridProfileName") or di.get("gridProfile") or self.context.get("grid_profile_name"),
        }
        # Cache grid profile name for persistence across polls
        if d["device"]["grid_profile_name"]:
            self.context["grid_profile_name"] = d["device"]["grid_profile_name"]

        # Sprint 4b: compute startup duration in seconds
        _started_at_str = self.context.get("started_at")
        _startup_s = None
        if _started_at_str:
            try:
                import datetime as _dt2
                _started_dt = _dt2.datetime.fromisoformat(_started_at_str)
                _startup_s = round((_dt.datetime.now(_dt.timezone.utc) - _started_dt).total_seconds(), 1)
            except Exception:
                pass

        d["integration"] = {
            "provider_mode": "Cloud",
            # Phase B: ISO 8601 timestamp — required by HA device_class: timestamp
            "last_update": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "update_available": _update_label or self.context.get("update_available"),
            # Phase D: container start time + first successful poll time
            "last_restart": self.context.get("started_at"),
            "startup_time": self.context.get("first_poll_at"),
            # Sprint 4b: startup duration in seconds (FEM parity: device_class=duration)
            "startup_time_s": _startup_s,
            # Sprint 4b: MQTT publisher status — always online if we're publishing
            "mqtt_status": "online",
        }
        
        # 3. Capacity & Battery Limits
        sn_val = d.get("apower_serial_numbers")
        
        # Circuit 3: FEM explicitly supports this entity for AU/US-V1 sites even when no
        # physical 3rd switch is present (AU hardware calculates it as residual load).
        # Always publish with power=0 if no real reading, per FEM parity.
        if d.get("smart_circuit_3_power") is None:
            d["smart_circuit_3_power"] = 0.0

        if isinstance(sn_val, list):
            apower_sn_list = sn_val
        else:
            apower_sn_list = [x.strip() for x in str(sn_val).split(",") if x.strip()] if sn_val else []
        battery_count = len(apower_sn_list)

        # Automatically hydrate the local DB table using the Cloud's discovered serial numbers natively
        if not getattr(self, "_batteries_upserted", False) and apower_sn_list:
            from src.services.battery_capability import (
                FALLBACK_KW, FALLBACK_KWH, fleet_capability, load_catalog_models,
            )
            from src.services.db import upsert_battery

            # The batteries table stores rated_kw and rated_kwh per unit and was
            # being seeded with aPower X's figures for every unit regardless of
            # model. Resolve each one instead, keyed on peHwVersion.
            _seed_units = d.get("apower_units") or []
            _by_serial = {}
            try:
                if _seed_units:
                    for cap in fleet_capability(_seed_units, load_catalog_models()).units:
                        _by_serial[cap.serial] = cap
            except Exception:
                logger.debug(f"[{self.short_id}] per-unit rating unavailable", exc_info=True)

            for i, sn in enumerate(apower_sn_list):
                if sn:
                    _cap = _by_serial.get(sn)
                    asyncio.create_task(upsert_battery(
                        sn[-8:], sn, self.short_id,
                        _cap.continuous_kw if _cap else FALLBACK_KW,
                        _cap.usable_kwh if _cap else FALLBACK_KWH,
                        i + 1,
                    ))
            self._batteries_upserted = True

        _battery_count = d.get("battery_count") or d.get("apower_count") or battery_count

        # Capability comes from the device registry, per unit and summed —
        # not from a per-unit constant times a count. aPower X is 5 kW/13.6 kWh,
        # aPower 2 is 10 kW/15 kWh and aPower S is 11.5 kW/15 kWh, and an
        # aGate X 1.3 can carry all three at once, so there is no single rating
        # to multiply. The old code applied aPower X's numbers to everything.
        _fleet = None
        try:
            from src.services.battery_capability import fleet_capability, load_catalog_models
            _units = d.get("apower_units") or (self.context.get("profile", {}) or {}).get("apower_units") or []
            if _units:
                _fleet = fleet_capability(_units, load_catalog_models())
        except Exception:
            logger.debug(f"[{self.short_id}] fleet capability unavailable", exc_info=True)

        # A hardware line for Home Assistant's device info: the aGate's SKU,
        # then how many aPowers and which. FEM shows "AGT-R1V1-AU (Hybrid)
        # [default]"; the fleet is the part FEM never had and is what makes the
        # line worth reading on a site with more than one unit.
        #
        # A SKU is only included when it was actually resolved. SOURCE_FALLBACK
        # means nothing identified the unit and aPower X's figures were assumed,
        # and printing an assumed part number as though it were read off the
        # hardware is how "aGate 102" happened.
        try:
            from src.services.battery_capability import SOURCE_FALLBACK

            # "AGT-R1V1-AU \u00b7 1\u00d7 aPower" — the gateway's full SKU and how
            # many aPowers, and nothing else. The aPower part numbers were
            # tried and are not wanted: they push the line to 51 characters
            # with two SKUs and 72 with three, against the 30 of FEM's
            # equivalent, and the count is the part a reader actually uses.
            _hw_bits = []

            # The aGate's SKU lives in device_model_full — "aGate X-01-AU
            # (AGT-R1V1-AU)" — not in profile["sku"], which is routinely empty.
            # Reading the empty key produced a hardware line with the gateway's
            # own part number missing.
            _agate_sku = (self.context.get("profile", {}) or {}).get("sku") or ""
            if not _agate_sku:
                _mf = (self.context.get("device_model_full") or "").strip()
                if "(" in _mf and _mf.endswith(")"):
                    _agate_sku = _mf[_mf.index("(") + 1:-1].strip()
            if _agate_sku:
                _hw_bits.append(_agate_sku)

            _units = _fleet.unit_count if _fleet else 0
            if _units:
                _hw_bits.append(f"{_units}\u00d7 aPower" + ("s" if _units != 1 else ""))

            self.context["hw_summary"] = " \u00b7 ".join(b for b in _hw_bits if b)
        except Exception:
            logger.debug(f"[{self.short_id}] hardware summary unavailable", exc_info=True)

        _rated_kwh_per_unit = 13.6   # only reached when nothing identifies the units
        if _fleet and _fleet.unit_count:
            _total_kwh = d.get("total_capacity") or _fleet.usable_kwh
            _rated_power_per_unit = None          # summed below instead
            _fleet_kw = _fleet.continuous_kw
        else:
            _total_kwh = d.get("total_capacity") or (_battery_count * _rated_kwh_per_unit if _battery_count else 0)
            _rated_power_per_unit = 5.0
            _fleet_kw = (_battery_count * _rated_power_per_unit) if _battery_count else None
        _soc = d.get("battery_soc") or 0

        # P1 Fix: batteries_online — apower_status is a CSV string of "1"/"0" per unit (e.g. "1" or "1,1").
        # If absent (cloud doesn't always publish it), fall back to battery_count when battery is clearly active.
        _apower_status_raw = d.get("apower_status")
        if _apower_status_raw is not None:
            # Parse as list or CSV string
            if isinstance(_apower_status_raw, list):
                _status_list = _apower_status_raw
            else:
                _status_list = [x.strip() for x in str(_apower_status_raw).split(",") if x.strip()]
            _batteries_online = len([x for x in _status_list if str(x) == "1"])
        elif _battery_count and _battery_count > 0:
            # apower_status absent — infer: if any battery count is known, assume all online
            # (the cloud only reports the count when batteries are registered and communicating)
            _batteries_online = int(_battery_count)
        else:
            _batteries_online = 0

        d["capacity"] = {
            "battery_count": _battery_count,
            "batteries_online": _batteries_online,
            "total": _total_kwh,
            "available": round(_total_kwh * (_soc / 100.0), 2) if _total_kwh and _soc else 0,
            # Derived from rated power per unit; overridden by live BMS data if available
            # Charge and discharge share the continuous rating: the registry
            # carries one figure per model, because FranklinWH publishes one.
            # If per-direction limits ever appear they belong in the catalog,
            # not here.
            "max_charge_kw": round(_fleet_kw, 2) if _fleet_kw else None,
            "max_discharge_kw": round(_fleet_kw, 2) if _fleet_kw else None,
            "peak_kw": _fleet.peak_kw if _fleet else None,
            "ac_solar_max_kw": _fleet.ac_solar_kw if _fleet else None,
            "dc_pv_max_kw": _fleet.dc_pv_kw if _fleet else None,
            "mppt_count": _fleet.mppt_count if _fleet else 0,
            "models": _fleet.models if _fleet else [],
            "mixed_fleet": _fleet.mixed if _fleet else False,
            "capability_source": (
                "registry" if _fleet and _fleet.resolved_from_registry else "assumed"
            ),
        }
        
        # 4. Cross-Topology Power Flow & Relays
        if "power" not in d:
            d["power"] = {}
            
        d["power"]["grid_to_battery"] = d.get("grid_charging_battery", 0)
        d["power"]["solar_to_grid"] = d.get("solar_export_to_grid", 0)
        d["power"]["battery_to_grid"] = d.get("battery_export_to_grid", 0)
        d["power"]["solar_to_battery"] = d.get("solar_charging_battery", 0)  # soChBat — real API field

        # Feature (Solar Setup): Hardware Solar Discovery
        d["solar_hardware"] = {
            # MPPT
            "mppt_en_flag": d.get("mppt_en_flag", False),
            "mppt_export_en": d.get("mppt_export_en", 0),
            "mppt_status": d.get("mppt_status"),
            "mppt_all_power": d.get("mppt_all_power"),
            "mppt_active_power": d.get("mppt_active_power"),
            # aPBox Remote Solar
            "remote_solar_mode": d.get("remote_solar_mode", 0),
            "remote_solar_enabled": d.get("remote_solar_enabled", 0),
            "pv_split_ct_en": d.get("pv_split_ct_en", 0),
            "grid_split_ct_en": d.get("grid_split_ct_en", 0),
            "apbox_remote_solar": d.get("apbox_remote_solar"),
            "mpan_pv1_power": d.get("mpan_pv1_power"),
            "mpan_pv2_power": d.get("mpan_pv2_power"),
            "remote_solar_pv1": d.get("remote_solar_pv1", 0),
            "remote_solar_pv2": d.get("remote_solar_pv2", 0),
        }

        # franklinwh-cloud schema (models.py:107): 1=OPEN(connected)=ON, 0=CLOSED(disconnected)=OFF
        # This is the authoritative definition — NO inversion needed.
        def _relay_on(v): return bool(v) if v is not None else None  # 1=ON, 0=OFF
        d["power"]["relays"] = {
            "grid1":      _relay_on(d.get("grid_relay1")),
            "grid2":      _relay_on(d.get("grid_relay2")),
            "generator":  _relay_on(d.get("generator_relay")),
            "solar1":     _relay_on(d.get("solar_relay1")),
            "blackStart": _relay_on(d.get("black_start_relay")),
            "pv2":        _relay_on(d.get("pv_relay2")),
            "apbox":      _relay_on(d.get("bfpv_apbox_relay")),
        }

        # Ensure battery dictionary is intact for the heater sensor
        if "battery" not in d:
            d["battery"] = {}
        # DEF-HEATER-FIX: heatState from BMS slow-poll. Prefer first BMS unit's heat_state
        # (aggregated into bms_units at line ~1175). Fall back to False when BMS not yet polled.
        _bms_units = d.get("bms_units", [])
        _heater_on = any(bool(u.get("heat_state")) for u in _bms_units) if _bms_units else False
        d["battery"]["heater_state"] = _heater_on

        # DEF-BAT-CURRENT-FIX: Battery current derived from power / nominal DC voltage.
        # FEM source: mqtt_publisher.py:1428-1438
        # Cloud API has no DC current — we derive: I = P / V_nominal
        # aPower DC bus ≈ 51.2 V (LFP 16S × 3.2 V nominal cell voltage)
        # Return 0.0 when |power| ≤ 5W (matches FEM sentinel).
        if "current" not in d.get("battery", {}):
            _bat_kw_now = d.get("battery_kw") or d.get("battery_use") or 0.0
            _bat_w = abs(float(_bat_kw_now or 0)) * 1000
            _bat_amps = round(_bat_w / 51.2, 1) if _bat_w > 5 else 0.0
            d["battery"]["current"] = _bat_amps

        # Phase A: status sub-dict from existing root fields
        # Sprint 4b: battery derived state from power flow direction
        _bat_kw = d.get("battery_kw") or d.get("battery_use") or 0.0
        if _bat_kw > 0.05:
            _bat_state = "Discharging"
        elif _bat_kw < -0.05:
            _bat_state = "Charging"
        else:
            _bat_state = "Standby"

        # Set grid_status at top-level of flat dict so Alpine data?.grid_status templates work.
        # Also included in d["status"] below for MQTT entity stat_path resolution.
        _grid_status = (d.get("grid_connection_state") or "Connected")
        if hasattr(_grid_status, "value"):  # GridConnectionState enum — extract .value string
            _grid_status = _grid_status.value
        d["grid_status"] = _grid_status

        d["status"] = {
            # grid_connection_state is a GridConnectionState enum from franklinwh-cloud (DEF-GRID-STATE-ENUM fix).
            # .value gives the display string: 'Connected', 'Outage', 'SimulatedOffGrid', 'NotGridTied'.
            "grid_status": _grid_status,
            "active_dispatch_name": d.get("active_tou_name", ""),
            "active_dispatch_start": d.get("active_tou_start", ""),
            "active_dispatch_end": d.get("active_tou_end", ""),
            "active_dispatch_remaining": d.get("active_tou_remaining", ""),
            "active_dispatch_code": d.get("active_tou_dispatch", ""),
            # DEF-GEN-ENABLED-FIX: generator_enabled may be None when no generator API call
            # was made. Explicitly coerce to bool so binary_sensor publishes ON/OFF not Unknown.
            # Source priority: generator_enabled (from API) → d.generator_enabled → genEn root field.
            "generator_enabled": bool(d.get("generator_enabled") or d.get("genEn") or False),
            # Sprint 4b: new status entities
            "battery_status": _bat_state,
            "control_source": "Cloud",  # FHAI is always cloud-controlled
            "grid_profile": d["device"].get("grid_profile_name") or self.context.get("grid_profile_name", ""),
            "current_dispatch": d.get("active_tou_name", ""),  # same as dispatch name
            "dispatch_remaining": d.get("active_tou_remaining", ""),
            # P1 Fix: dispatch execution status sensor (distinct from dispatch_action control)
            # Derived: if a named dispatch is active, use battery direction; else Idle.
            "dispatch_execution_status": (
                _bat_state if d.get("active_tou_name") else "Idle"
            ),
            # P1 Fix: grid connection state tri-state label for FEM parity
            # Maps GridConnectionState values → FEM-compatible: Disconnected/Connected/Available
            "grid_connection_state_label": (
                "Disconnected" if _grid_status == "Outage"
                else "Available" if _grid_status in ("SimulatedOffGrid", "NotGridTied")
                else "Connected"
            ),
        }

        # Phase B: solar-to-battery fallback derivation (only if soChBat API field is absent/zero)
        if not d["power"].get("solar_to_battery"):
            _solar_kw = d.get("solar_kw") or 0.0
            _solar_to_grid = d.get("grid_export") or 0.0  # solar exported to grid
            _home_kw = d.get("home_kw") or 0.0
            d["power"]["solar_to_battery"] = round(max(0.0, _solar_kw - _solar_to_grid - _home_kw), 3)

        # 5. Global Control States (using the values extracted up above)
        _ha_cache = self.context.get("ha_input_cache", {})
        if "control" not in d:
            d["control"] = {}
        # DEF-GRID-UNLIMITED-FIX: MQTT switch entities require "ON"/"OFF" strings.
        # Storing raw Python bool causes HA to receive incorrect payloads.
        # globalGridDischargeEnabled → grid_export_unlimited, globalGridChargeEnabled → grid_import_unlimited.
        _exp_unlimited_raw = d.get("grid_export_unlimited")
        _imp_unlimited_raw = d.get("grid_import_unlimited")
        d["control"]["_pcs_grid_export_unlimited"] = "ON" if _exp_unlimited_raw else "OFF"
        d["control"]["_pcs_grid_import_unlimited"] = "ON" if _imp_unlimited_raw else "OFF"
        # ── Storm settings: prefer ha_input_cache (user commands), fall back to API seed ──
        # NOTE: Current dataclass has NO storm fields — get_storm_settings() is the only source.
        _storm_raw = _ha_cache.get("storm_hedge", "OFF")
        _storm_strategy_int = int(_ha_cache.get("storm_strategy", 0))
        _storm_lead_min = int(_ha_cache.get("storm_backup_lead", 0))
        d["control"]["_storm_hedge_enabled"] = "Enabled" if _storm_raw in ("ON", "Enabled", True, 1) else "Disabled"
        d["control"]["_storm_decision_strategy"] = ["Disabled", "Auto-Active", "Ask Each Time"][min(_storm_strategy_int, 2)]
        d["control"]["_storm_backup_lead_min"] = _storm_lead_min
        d["storm"] = {
            "enabled": _storm_raw,
            "strategy": d["control"]["_storm_decision_strategy"],
            "backup_lead_min": _storm_lead_min,
        }
        # Phase D1: populate dispatch + circuit + grid-limit keys so controls never publish 'unknown'
        d["control"]["dispatch_power"] = d.get("dispatch_power", 2.0)
        d["control"]["dispatch_duration"] = d.get("dispatch_duration", 60)
        d["control"]["dispatch_target_soc"] = d.get("dispatch_target_soc", 100)
        d["control"]["smart_circuit_1"] = d.get("smart_circuit_1_state", "OFF")
        d["control"]["smart_circuit_2"] = d.get("smart_circuit_2_state", "OFF")
        d["control"]["smart_circuit_3"] = d.get("smart_circuit_3_state", "OFF")
        # Clamp -1 (unlimited) to 0 for slider display parity with FEM
        _raw_export_lim = d.get("grid_export_limit", 0)
        _raw_import_lim = d.get("grid_import_limit", 0)
        d["control"]["grid_export_limit"] = 0 if (_raw_export_lim is None or _raw_export_lim < 0) else _raw_export_lim
        d["control"]["grid_import_limit"] = 0 if (_raw_import_lim is None or _raw_import_lim < 0) else _raw_import_lim
        d["control"]["tou_saved_dispatches"] = d.get("tou_saved_dispatches", "Stop / Restore")
        # off_grid_mode: map to FEM-compatible select option strings
        _off_grid_raw = d.get("off_grid_mode") or d.get("off_grid") or (d.get("mode", {}).get("off_grid") == "ON")
        d["control"]["_off_grid_mode"] = "Off-Grid" if _off_grid_raw in (1, True, "true", "1", "ON", "on", "Off-Grid") else "On-Grid"
        # Phase D3: dispatch sub-dict via write-back cache
        d["dispatch"] = {
            "action": _ha_cache.get("dispatch_action", "Idle"),
        }

        # 6. Current TOU block from abstracted get_stats (Current)
        if d.get("active_tou_name"):
            d["tou_active"] = {
                "name": d.get("active_tou_name", ""),
                "title": d.get("active_tou_name", ""),
                "dispatch_code": d.get("active_tou_dispatch", ""),
                "dispatch_desc": d.get("active_tou_dispatch", ""),
                "wave_type": 0,
                "start": d.get("active_tou_start", ""),
                "end": d.get("active_tou_end", ""),
                "remaining": d.get("active_tou_remaining", ""),
            }
        elif "tou_info" in results:
            ti = self._dataclass_to_dict(results["tou_info"]) if not isinstance(results["tou_info"], dict) else results["tou_info"]
            if isinstance(ti, dict):
                d["tou_active"] = {
                    "name": ti.get("activeTOUname", ""),
                    "title": ti.get("activeTOUtitle", ""),
                    "dispatch_code": ti.get("activeTOUdispatchCode", ""),
                    "dispatch_desc": ti.get("activeTOUdispatchDesc", ""),
                    "wave_type": ti.get("activeWaveType", 0),
                    "start": ti.get("activeStartTime", ""),
                    "end": ti.get("activeEndTime", ""),
                    "remaining": ti.get("activeRemainingTime", ""),
                }
                d["tou_next"] = {
                    "name": ti.get("nextTOUname", ""),
                    "title": ti.get("nextTOUtitle", ""),
                    "dispatch_code": ti.get("nextTOUdispatchCode", ""),
                    "dispatch_desc": ti.get("nextTOUdispatchDesc", ""),
                    "wave_type": ti.get("nextWaveType", -1),  # tariff type for next block
                    "start": ti.get("nextStartTime", ""),
                    "end": ti.get("nextEndTime", ""),
                    "remaining": ti.get("nextRemainingTime", ""),
                }
        elif "tou_active" not in d:
            # Preserve cached value from previous poll if available
            d.setdefault("tou_active", {})
            d.setdefault("tou_next", {})

        # ── Propagate tou_active / tou_next → d["status"] ──────────────────────────────────────
        # d["status"] is built before tou_info is processed; this second pass fills all
        # active/next dispatch fields.  Fields are cleared when NOT in TOU mode (work_mode=1)
        # so HA sensors show unavailable rather than stale tariff data.
        _in_tou = (d.get("mode", {}).get("work_mode") == 1)
        _ta = d.get("tou_active") or {}
        _tn = d.get("tou_next") or {}

        def _dispatch_label(code) -> str:
            try:
                return _DISPATCH_ID_LABELS.get(int(code), f"Dispatch {code}")
            except (ValueError, TypeError):
                return ""

        def _wave_label(wtype) -> str:
            try:
                return _WAVE_TYPE_LABELS.get(int(wtype), "")
            except (ValueError, TypeError):
                return ""

        if _ta and _in_tou:
            d["status"]["active_dispatch_name"]      = _ta.get("name", "")
            d["status"]["active_dispatch_start"]     = _ta.get("start", "")
            d["status"]["active_dispatch_end"]       = _ta.get("end", "")
            d["status"]["active_dispatch_remaining"] = _ta.get("remaining", "")
            d["status"]["active_dispatch_code"]      = str(_ta.get("dispatch_code", ""))
            # TOU Dispatch Now — dispatchId enum label (was: raw name string)
            d["status"]["current_dispatch"]          = _dispatch_label(_ta.get("dispatch_code"))
            d["status"]["dispatch_remaining"]        = _ta.get("remaining", "")
            d["status"]["dispatch_tariff_now"]       = _wave_label(_ta.get("wave_type", -1))
        else:
            for _k in ("active_dispatch_name", "active_dispatch_start", "active_dispatch_end",
                       "active_dispatch_remaining", "active_dispatch_code", "current_dispatch",
                       "dispatch_remaining", "dispatch_tariff_now"):
                d["status"][_k] = ""

        if _tn and _in_tou:
            d["status"]["next_dispatch_name"]   = _dispatch_label(_tn.get("dispatch_code"))
            d["status"]["next_dispatch_start"]  = _tn.get("start", "")
            d["status"]["next_dispatch_end"]    = _tn.get("end", "")
            d["status"]["dispatch_tariff_next"] = _wave_label(_tn.get("wave_type", -1))
        else:
            for _k in ("next_dispatch_name", "next_dispatch_start", "next_dispatch_end",
                       "dispatch_tariff_next"):
                d["status"][_k] = ""

        # Stamp wave_label onto tou_active / tou_next so the dashboard can display
        # the human-readable enum ("Super Off-Peak") without a client-side int→label map.
        for _tou_key in ("tou_active", "tou_next"):
            _tou = d.get(_tou_key)
            if isinstance(_tou, dict):
                _tou["wave_label"] = _wave_label(_tou.get("wave_type", -1))

        # 7. Reserve SOCs
        if "reserves" in results and isinstance(results["reserves"], list):
            for r in results["reserves"]:
                wm = r.get("workMode")
                soc_val = r.get("soc", r.get("socLowThreshold", 20))
                if wm == 1:
                    d["tou_reserve_soc"] = soc_val       # Phase C: TOU reserve floor
                elif wm == 2:
                    d["self_reserve_soc"] = soc_val
                elif wm == 3:
                    d["backup_reserve_soc"] = soc_val

        # Finally, safely round all numeric dict values that aren't timestamps
        # to ensure clean payloads down the line.
            d.setdefault("tou_next", {})

        # 7. BMS telemetry per aPower (from get_bms_info slow-poll)
        if "bms" in results and isinstance(results["bms"], dict):
            bms_list = self._normalise_bms_raw(results["bms"])
            d["bms_units"] = bms_list

            # Phase C: aggregate battery current + capacity limits from BMS units
            total_current = sum(
                (u.get("current") or 0)
                for u in bms_list
                if isinstance(u, dict)
            )
            d["battery"]["current"] = round(total_current, 2)

            # Max charge/discharge power from raw BMS response
            raw_bms_vals = list(results["bms"].values())
            max_chg_w = sum((r.get("maxChargeW") or 0) for r in raw_bms_vals if isinstance(r, dict))
            max_dis_w = sum((r.get("maxDischargeW") or 0) for r in raw_bms_vals if isinstance(r, dict))
            d["capacity"]["max_charge_kw"] = round(max_chg_w / 1000, 2) if max_chg_w else d["capacity"].get("max_charge_kw")
            d["capacity"]["max_discharge_kw"] = round(max_dis_w / 1000, 2) if max_dis_w else d["capacity"].get("max_discharge_kw")
        elif "bms_units" not in d:
            d.setdefault("bms_units", [])

        # ── Zero-collapse detector (Batch I, backlog 2026-07-12) ──────────
        # FranklinWH cloud has a stale-window bug where a successful HTTP
        # 200 poll returns a payload with the configured mode (typically
        # Time-of-Use) + all runtime metrics zeroed (soc=0, battery_kw=0,
        # grid_kw=0, run_status=Standby). Observed rate: 16% of polls
        # over 30h in prod. Key evidence rules out other candidates:
        #   - key_count identical (198) between good and bad rows → not
        #     an async endpoint-response swap
        #   - all mode fields internally consistent (work_mode / effective
        #     _mode / mode.runtime_mode all agree on TOU) → not a partial
        #     merge
        #   - api_last_error='', api_connection_status='ok' → cloud
        #     returned 200 with a valid-looking default payload
        #
        # Signature: soc==0 AND battery_kw==0 AND grid_kw==0 AND
        # run_status in (0, Standby) AND previous good SoC > 5%.
        # The prev-SoC guard prevents dropping legit boot polls on a
        # genuinely empty battery or first-install scenarios.
        #
        # When detected: tag the dict with _fhai_suspect_stale_window and
        # increment self.context["_stale_polls_dropped"]. The poll loop
        # (line ~561) checks the tag and skips both _store_metrics and
        # _on_data — keeping the last-known-good state visible until the
        # next real poll succeeds. Also exposed as MQTT diagnostic sensor
        # (see entities.py entry `stale_polls_dropped`).
        try:
            _soc = d.get("battery_soc")
            _bkw = d.get("battery_kw")
            _gkw = d.get("grid_kw")
            _rst = d.get("run_status")
            _prev_good_soc = float(self.context.get("_last_good_soc", 0.0) or 0.0)
            _prev_good_mode = self.context.get("_last_good_mode", "")
            _cur_mode = d.get("operating_mode") or ""
            _rst_is_standby = _rst in (0, "0", None) or str(_rst).lower() == "standby"
            _all_zeros = (
                _soc == 0.0
                and _bkw == 0.0
                and _gkw == 0.0
                and _rst_is_standby
            )
            # Batch M-2 (2026-07-25): lowered prev-SoC threshold 5.0 → 1.0
            # so low-SoC scenarios (battery below 5%) are still protected.
            # Batch I's original 5% guard was intended to spare genuine
            # empty-battery scenarios but was under-protecting real
            # stale-window rows at low SoC (as evidenced by 2026-07-24
            # 16:26:11 row leaking through with prev soc=4.1%).
            _by_soc_threshold = _all_zeros and _prev_good_soc >= 1.0
            # Batch M-2: mode-flip detection — when the payload's mode
            # jumps to "Time-of-Use" from a non-TOU last-good mode with
            # all-zero runtime metrics, that's the native-mode default
            # leaking through the cloud API. Catches stale-window rows
            # at ANY prev SoC (including < 1%) because the mode-flip
            # signature is independent of SoC.
            _by_mode_flip = (
                _all_zeros
                and _prev_good_mode
                and _cur_mode
                and _prev_good_mode != _cur_mode
                and _cur_mode == "Time-of-Use"
                and _prev_good_mode != "Time-of-Use"
            )
            # Batch I-3 (2026-08-06): "SoC cliff-drop" signature.
            # The two existing signatures require all four runtime fields
            # zero before firing. If ANY one field is non-zero
            # (battery_kw wiggling, grid_kw non-zero from a transient, etc.)
            # the stale row leaks through and last_data gets overwritten
            # with soc=0. The v0.4.13 downstream fix stops the SD engine
            # from firing notifications on this, but the UI SoC widget
            # still blinks to 0% until the next real poll lands.
            #
            # Physical reality check: at aPower's ~5 kW max discharge and
            # 13.6 kWh capacity, dropping 5% (0.68 kWh) requires 8+
            # minutes. A poll interval is 30 s. Therefore any transition
            # from `_last_good_soc >= 5%` to `soc == 0` between two polls
            # is impossible — it's data corruption, not a real reading.
            # Fires INDEPENDENTLY of battery_kw / grid_kw / run_status.
            _by_soc_cliff = (
                _soc == 0.0
                and _prev_good_soc >= 5.0
            )

            # The library's own flag is authoritative and comes first; the
            # heuristics below it remain as a backstop for a cloud glitch the
            # library has not classified.
            _by_library = bool(self.context.get("_last_stats_is_stale"))
            _is_stale_window = _by_library or _by_soc_threshold or _by_mode_flip or _by_soc_cliff
            if _is_stale_window:
                _dropped = int(self.context.get("_stale_polls_dropped", 0)) + 1
                self.context["_stale_polls_dropped"] = _dropped
                if _by_library:
                    _sig = "library-is_stale"
                elif _by_soc_threshold:
                    _sig = "soc-threshold"
                elif _by_mode_flip:
                    _sig = "mode-flip"
                else:
                    _sig = "soc-cliff"
                d["_fhai_suspect_stale_window"] = (
                    f"soc==0 stale sentinel (signature={_sig}, "
                    f"prior soc={_prev_good_soc:.1f}%, prior mode={_prev_good_mode!r} → "
                    f"current mode={_cur_mode!r}, bkw={_bkw}, gkw={_gkw}, rst={_rst!r})"
                )
                _lvl = logger.warning if _dropped % 10 == 1 else logger.info
                _lvl(
                    f"[{self.short_id}] cloud stale-window detected [{_sig}] — "
                    f"dropping poll (soc=0, prior={_prev_good_soc:.1f}%, "
                    f"prior_mode={_prev_good_mode!r}, cur_mode={_cur_mode!r}, "
                    f"total dropped this session={_dropped})"
                )
            else:
                # Update the last-good watermarks ONLY on healthy polls
                if isinstance(_soc, (int, float)) and _soc > 0:
                    self.context["_last_good_soc"] = float(_soc)
                if _cur_mode and _cur_mode != "Unknown":
                    self.context["_last_good_mode"] = _cur_mode
            d["stale_polls_dropped"] = int(self.context.get("_stale_polls_dropped", 0))
        except Exception as _sc_exc:
            logger.debug(f"[{self.short_id}] stale-window detector error — {_sc_exc}")

        return d

    @staticmethod
    def _normalise_bms_raw(bms_dict: dict) -> list:
        """Convert raw get_bms_info() response dict (keyed by serial) into the standard
        bms_units list that both _normalise_stats and the /bms/trigger endpoint return.

        Centralising the mapping here means the trigger endpoint can return fresh,
        normalised data in its response body without waiting for the background _poll_once
        flush — eliminating the race condition that caused missing recording samples.
        """
        bms_list = []
        for serial, raw in bms_dict.items():
            if not isinstance(raw, dict):
                continue
            entry = {
                "serial": serial,
                "soc": raw.get("batSoc"),
                "soh": raw.get("batSoh"),
                "pack_voltage": raw.get("batTotalVolt"),
                "current": raw.get("batCurr"),
                "alarm_level": raw.get("alarmLevel", 0),
                "run_mode": raw.get("runMode"),
                "inverter_status": raw.get("inverterStatus"),
                "dcdc_status": raw.get("DCDCStatus"),
                "bms_state": raw.get("bmsState"),
                "mos_state": raw.get("mosState"),
                "switch_state": raw.get("switchState"),
                "heat_state": raw.get("heatState"),
                "fan_state": raw.get("fanState"),
                "cell_voltages": raw.get("batVolt", []),
                "cell_temps": raw.get("batTemp", []),
                "highest_cell_v": raw.get("singleHighestVolt"),
                "lowest_cell_v": raw.get("singleLowestVolt"),
                "highest_cell_t": raw.get("singleHighestTemp"),
                "lowest_cell_t": raw.get("singleLowestTemp"),
                "dev_temp": raw.get("devTemp"),
                "llc_temp": raw.get("llcTemp"),
                "buckboost_temp": raw.get("buckBoostTemp"),
                "buckboost_current": raw.get("buckboostCurr"),
                "inv_temp": raw.get("invTemp"),
                "grid_volt_l1": raw.get("gridVol1"),
                "grid_volt_l2": raw.get("gridVol2"),
                "grid_freq": raw.get("gridFreq"),
                "grid_line_vol": raw.get("gridLineVol"),
                "inv_volt_1": raw.get("invVolt1"),
                "inv_volt_2": raw.get("invVolt2"),
                "grid_volt_an": raw.get("gridVoltAN"),
                "grid_volt_bn": raw.get("gridVoltBN"),
                "inv_line_vol": raw.get("invLineVol"),
                "solar_volt_an": raw.get("solarVoltAN"),
                "solar_volt_bn": raw.get("solarVoltBN"),
                "pos_bus_volt": raw.get("positiveBusVolt"),
                "neg_bus_volt": raw.get("negativeBusVolt"),
                "mid_bus_volt": raw.get("midBusVolt"),
                "load_curr_1": raw.get("loadCurr1"),
                "load_curr_2": raw.get("loadCurr2"),
                "inv_curr_1": raw.get("invCurr1"),
                "inv_curr_2": raw.get("invCurr2"),
                "out_curr_1": raw.get("outCur1"),
                "out_curr_2": raw.get("outCur2"),
                "act_pwr_1": raw.get("actPwr1"),
                "act_pwr_2": raw.get("actPwr2"),
                "react_pwr_1": raw.get("reactPwr1"),
                "react_pwr_2": raw.get("reactPwr2"),
                "sampled_volt": raw.get("samBatVol"),
                "packet_mode": raw.get("type"),
                "balan_state": raw.get("balanState", 0),
                "max_temp_pos": raw.get("maxTempPos"),
                "min_temp_pos": raw.get("minTempPos"),
                "max_cell_pos": raw.get("maxVolPos"),
                "min_cell_pos": raw.get("minVolPos"),
            }
            bms_list.append(entry)
        return bms_list

    async def _store_metrics(self, data: dict) -> None:
        """Persist this poll snapshot to the rolling metrics table.

        Gated by the `metrics_enabled` DB flag (cached 60s to avoid DB hit every poll).
        Auto-default: enabled for Docker standalone, disabled for HA Add-on (where HA
        Recorder already stores all entity state history).
        """
        # ── Flag cache ──────────────────────────────────────────────────────────
        import time as _time
        _now = _time.monotonic()
        if not hasattr(self, "_metrics_flag_cache"):
            self._metrics_flag_cache = None
            self._metrics_flag_ts = 0.0
        if _now - self._metrics_flag_ts > 60:
            try:
                from src.services.db import get_config_value
                from src.config.environment import detect_environment
                raw = await get_config_value("metrics_enabled", None)
                if raw is None:
                    # First time — set env-aware default, persist it
                    env = detect_environment()
                    default = env != "ha_addon"
                    await get_config_value.__module__  # just import check
                    from src.services.db import set_config_value
                    await set_config_value("metrics_enabled", str(default).lower())
                    self._metrics_flag_cache = default
                    logger.info(f"[{self.short_id}] metrics_enabled auto-set to {default} (env={env})")
                else:
                    self._metrics_flag_cache = str(raw).lower() in ("true", "1")
                self._metrics_flag_ts = _now
            except Exception as _e:
                logger.debug(f"[{self.short_id}] metrics flag check failed: {_e}")
                self._metrics_flag_cache = True  # fail-open
        # ── Guard ───────────────────────────────────────────────────────────────
        if not self._metrics_flag_cache:
            return
        try:
            from src.services.db import insert_metric, insert_api_edge_metrics
            await insert_metric(self.short_id, data)

            # Persistent CloudFront Analytics (Phase 14)
            if self._client:
                await insert_api_edge_metrics(
                    self.short_id,
                    self._client.get_metrics(),
                    self._client.edge_tracker.snapshot()
                )

            # D6: Periodic edge metrics TTL purge — every ~2880 writes (~24h at 30s poll)
            # Fire-and-forget: never blocks the poll loop; errors are silently logged.
            if not hasattr(self, '_edge_purge_counter'):
                self._edge_purge_counter = 0
            self._edge_purge_counter += 1
            if self._edge_purge_counter >= 2880:
                self._edge_purge_counter = 0
                try:
                    from src.services.db import purge_old_edge_metrics
                    deleted = await purge_old_edge_metrics(days=90)
                    if deleted:
                        logger.info(f"[{self.short_id}] Purged {deleted} api_edge_metrics rows older than 90d")
                except Exception as _pe:
                    logger.debug(f"[{self.short_id}] Edge metrics purge skipped: {_pe}")

        except Exception as exc:
            logger.warning(f"[{self.short_id}] Failed to store metrics: {exc}")


    # ------------------------------------------------------------------
    # Control command dispatch
    # ------------------------------------------------------------------

    async def dispatch_command(self, slug: str, value: str) -> dict:
        """
        Execute a control command via the Cloud API.

        :param slug:  Entity slug from MQTT or HTTP (e.g. "operating_mode")
        :param value: Raw string value from MQTT payload or HTTP body
        :returns:     {"ok": bool, "slug": str, "value": ..., "error": str|None}
        """
        try:
            from src.services.db import log_admin_audit
            delta_msg = ""
            result = None  # initialise so final return is always safe

            # --- Fast validation before any cloud API calls ---
            if slug == "battery_backup_reserve":
                try:
                    pct = int(float(value))
                except (ValueError, TypeError):
                    return {"ok": False, "slug": slug, "error": f"Invalid value: expected a number, got {value!r}"}
                if not (0 <= pct <= 100):
                    return {"ok": False, "slug": slug, "error": f"Invalid value: backup reserve must be 0-100, got {value!r}"}
                client = await self._get_or_create_client()
                current_mode = await client.get_mode()
                work_mode = current_mode.work_mode if hasattr(current_mode, "work_mode") else 1
                old_pct = current_mode.soc if hasattr(current_mode, "soc") else "?"
                delta_msg = f"Battery Backup Reserve: {old_pct}% -> {pct}%"
                result = await client.set_mode(requestedOperatingMode=work_mode, requestedSOC=pct)
                # Early return for battery_backup_reserve
                logger.info(f"[{self.short_id}] Command {slug}={value!r} OK")
                if delta_msg:
                    await log_admin_audit(f"Config Change", "GatewayService", "system", f"[{self.short_id}] {delta_msg}")
                return {"ok": True, "slug": slug, "value": value, "result": str(result)}

            if slug == "battery_tou_reserved_soc":
                # Phase C: Lightweight SOC-only update for TOU mode (workMode=1)
                # Uses update_soc() — does NOT switch operating mode, far cheaper than set_mode()
                try:
                    pct = int(float(value))
                except (ValueError, TypeError):
                    return {"ok": False, "slug": slug, "error": f"Invalid value: expected a number, got {value!r}"}
                if not (0 <= pct <= 100):
                    return {"ok": False, "slug": slug, "error": f"TOU Reserved SOC must be 0-100, got {value!r}"}
                client = await self._get_or_create_client()
                delta_msg = f"TOU Reserved SOC → {pct}%"
                result = await client.update_soc(requestedSOC=pct, workMode=1, electricityType=1)
                logger.info(f"[{self.short_id}] Command {slug}={value!r} OK")
                if delta_msg:
                    await log_admin_audit("Config Change", "GatewayService", "system", f"[{self.short_id}] {delta_msg}")
                return {"ok": True, "slug": slug, "value": value, "result": str(result)}

            if slug == "battery_self_consumption_reserve_soc":
                # Self-Consumption mode reserve SOC slider (workMode=2)
                # Published by HA when user moves the number entity slider in Self-Consumption dashboard.
                try:
                    pct = int(float(value))
                except (ValueError, TypeError):
                    return {"ok": False, "slug": slug, "error": f"Invalid value: expected a number, got {value!r}"}
                if not (0 <= pct <= 100):
                    return {"ok": False, "slug": slug, "error": f"Self-Consumption Reserve SOC must be 0-100, got {value!r}"}
                client = await self._get_or_create_client()
                delta_msg = f"Self-Consumption Reserve SOC → {pct}%"
                result = await client.update_soc(requestedSOC=pct, workMode=2, electricityType=1)
                logger.info(f"[{self.short_id}] Command {slug}={value!r} OK")
                if delta_msg:
                    await log_admin_audit("Config Change", "GatewayService", "system", f"[{self.short_id}] {delta_msg}")
                return {"ok": True, "slug": slug, "value": value, "result": str(result)}

            client = await self._get_or_create_client()

            if slug == "operating_mode":
                # Map MQTT string values to integer work modes
                mode_map = {"Time-of-Use": 1, "Self-Consumption": 2, "Emergency Backup": 3}
                work_mode = mode_map.get(value, 0)
                if work_mode == 0:
                    return {"ok": False, "error": f"Invalid operating mode requested: {value}"}
                
                kwargs = {}
                if work_mode == 3:
                    cache = self.context.get("ha_input_cache", {})
                    dur_type = cache.get("emergency_backup_duration_type", "Indefinite")
                    if dur_type == "Indefinite":
                        kwargs["forever"] = 1
                    else:
                        kwargs["forever"] = 2
                        dur_map = {"1 Day": 1440, "2 Days": 2880, "3 Days": 4320}
                        kwargs["duration"] = dur_map.get(dur_type, 1440)
                        
                    resume = cache.get("emergency_backup_resume_mode", "Self-Consumption")
                    resume_map = {"Self-Consumption": 2, "Time-of-Use": 1, "Off Grid": 3}
                    kwargs["next_mode"] = resume_map.get(resume, 2)
                
                # Defer to the advanced wrapper which internally parses constraints
                mode_resp = await self.set_operating_mode(work_mode=work_mode, **kwargs)
                if not mode_resp.get("ok"):
                    raise Exception(mode_resp.get("error", "Upstream API mode transition failure"))
                
                result = mode_resp.get("result", True)
                delta_msg = ""  # The advanced wrapper logs its own delta audit trails.

            elif slug == "storm_hedge_config":
                # Handle complex storm hedge modal payload
                try:
                    payload = json.loads(value)
                    # Corrected Mapping for Storm Hedge Config:
                    # 1. stormEn (bool -> 0/1)
                    # 2. advanceTime (UI hours -> API minutes for setAdvanceBackupTime, 30-300)
                    # 3. setAdvanceBackupTime (UI 1=Auto/0=Ask strategy -> API stormNoticeEn, 0=Auto/1=Ask)
                    
                    en = payload.get("stormEn", 0)
                    lead_hours = payload.get("advanceTime", 2.0)
                    strategy = payload.get("setAdvanceBackupTime", 1)  # 1=Auto, 0=Ask
                    
                    # Audit old state from cache
                    _hic = self.context.get("ha_input_cache", {})
                    old_state = "Enabled" if _hic.get("storm_hedge") in ("ON", True, 1) else "Disabled"
                    new_state = "Enabled" if en else "Disabled"
                    delta_msg = f"Storm Hedge [Modal]: {old_state} -> {new_state} (Advance: {lead_hours}h, Auto: {bool(strategy)})"

                    # Convert lead_hours to minutes for the 'setAdvanceBackupTime' API parameter (30-300)
                    lead_minutes = int(float(lead_hours) * 60)
                    if lead_minutes < 30: lead_minutes = 30
                    if lead_minutes > 300: lead_minutes = 300
                    
                    # Map strategy to stormNoticeEn (API: 0=Auto, 1=Ask)
                    storm_notice_en = 0 if strategy == 1 else 1

                    # The API's setStormNotice endpoint (triggered by stormNoticeEn) 
                    # supports ONLY stormNoticeEn. Appending 'advanceTime' (notification lead)
                    # as previously attempted causes a 400 Parameter Error.
                    result = await client.set_storm_settings(
                        stormEn=en,
                        setAdvanceBackupTime=lead_minutes,
                        stormNoticeEn=storm_notice_en
                    )
                    # Write-back cache for all three storm fields
                    _hic2 = self.context.setdefault("ha_input_cache", {})
                    _hic2["storm_hedge"] = "ON" if en else "OFF"
                    _hic2["storm_strategy"] = int(strategy)
                    # lead_hours is in decimal hours, but cache stores minutes for consistency with startup poll
                    _hic2["storm_backup_lead"] = int(float(lead_hours) * 60)
                except Exception as e:
                    logger.error(f"Failed to parse or apply storm hedge config: {e}")
                    raise Exception(f"Invalid storm hedge payload: {e}")

            elif slug in ("storm_hedge", "storm_hedge_enabled"):
                en = 1 if _str_to_bool(value) else 0

                # Audit old state from cache (Current dataclass has no storm fields)
                _hic = self.context.get("ha_input_cache", {})
                old_state = "Enabled" if _hic.get("storm_hedge") in ("ON", True, 1) else "Disabled"
                new_state = "Enabled" if en else "Disabled"
                delta_msg = f"Storm Hedge: {old_state} -> {new_state}"

                result = await client.set_storm_settings(stormEn=en)
                # Write-back cache — persist state since Current dataclass has no storm field
                self.context.setdefault("ha_input_cache", {})["storm_hedge"] = "ON" if en else "OFF"

            elif slug == "storm_decision_strategy":
                # Map string option to API int: Disabled=0, Auto-Active=1, Ask Each Time=2
                _strat_map = {"Disabled": 0, "Auto-Active": 1, "Ask Each Time": 2}
                new_strat_int = _strat_map.get(str(value).strip(), 0)
                _hic = self.context.get("ha_input_cache", {})
                old_strat_int = int(_hic.get("storm_strategy", 0))
                _strat_labels = ["Disabled", "Auto-Active", "Ask Each Time"]
                delta_msg = f"Storm Decision Strategy: {_strat_labels[min(old_strat_int,2)]} -> {_strat_labels[min(new_strat_int,2)]}"

                # Map strategy to stormNoticeEn (API: 0=Auto, 1=Ask)
                # UI strategy: 1=Auto, 0=Ask
                storm_notice_en = 0 if new_strat_int == 1 else 1
                # Must provide ONLY stormNoticeEn; 'advanceTime' is unsupported.
                result = await client.set_storm_settings(stormNoticeEn=storm_notice_en)
                self.context.setdefault("ha_input_cache", {})["storm_strategy"] = new_strat_int

                
            elif slug in ("grid_import_unlimited", "grid_export_unlimited", "grid_export_limit", "grid_import_limit"):
                # Use cached locking to prevent Cloud API staleness from snapping back the UI
                now = time.time()
                if not self._pcs_cache or (now - self._pcs_cache_time) > 120:
                    pcs = await client.get_power_control_settings()
                    self._pcs_cache = {
                        "chargeMax": pcs["result"]["globalGridChargeMax"],
                        "dischargeMax": pcs["result"]["globalGridDischargeMax"],
                        "importEnabled": pcs["result"].get("globalGridChargeEnabled", False),
                        "exportEnabled": pcs["result"].get("globalGridDischargeEnabled", False)
                    }
                
                chargeMax = self._pcs_cache["chargeMax"]
                dischargeMax = self._pcs_cache["dischargeMax"]
                
                if slug == "grid_import_unlimited":
                    chargeMax = -1 if _str_to_bool(value) else 0
                    old_val = "Enabled" if self._pcs_cache["importEnabled"] else "Disabled"
                    new_val = "Enabled" if chargeMax == -1 else "Disabled"
                    delta_msg = f"Grid Import Unlimited: {old_val} -> {new_val}"
                elif slug == "grid_export_unlimited":
                    dischargeMax = -1 if _str_to_bool(value) else 0
                    old_val = "Enabled" if self._pcs_cache["exportEnabled"] else "Disabled"
                    new_val = "Enabled" if dischargeMax == -1 else "Disabled"
                    delta_msg = f"Grid Export Unlimited: {old_val} -> {new_val}"
                elif slug == "grid_export_limit":
                    dischargeMax = float(value)
                    old_kw = round(float(self._pcs_cache["dischargeMax"]), 2)
                    delta_msg = f"Grid Export Limit: {old_kw} kW -> {dischargeMax} kW"
                elif slug == "grid_import_limit":
                    chargeMax = float(value)
                    old_kw = round(float(self._pcs_cache["chargeMax"]), 2)
                    delta_msg = f"Grid Import Limit: {old_kw} kW -> {chargeMax} kW"

                result = await client.set_power_control_settings(
                    globalGridChargeMax=chargeMax,
                    globalGridDischargeMax=dischargeMax
                )
                
                self._pcs_cache["chargeMax"] = chargeMax
                self._pcs_cache["dischargeMax"] = dischargeMax
                self._pcs_cache["importEnabled"] = chargeMax == -1
                self._pcs_cache["exportEnabled"] = dischargeMax == -1
                self._pcs_cache_time = time.time()

            elif slug == "off_grid_mode":
                state = 1 if _str_to_bool(value) else 0
                # Audit old state from cached control data (Current dataclass has no off_grid field)
                _last_data = self.status.last_data or {}
                _old_off_grid = _last_data.get("control", {}).get("_off_grid_mode", "On-Grid")
                old_state = "Off Grid" if _old_off_grid == "Off-Grid" else "On Grid"
                new_state = "Off Grid" if state == 1 else "On Grid"
                delta_msg = f"Grid Connect Mode: {old_state} -> {new_state}"

                result = await client.set_grid_status(status=state)

                # Optimistic MQTT publish: immediately reflect command in HA without waiting for next poll
                try:
                    from src.main import get_app_state
                    _pub = get_app_state().get("publisher")
                    if _pub:
                        _pfx = _pub.topic_prefix
                        _sid = self.short_id
                        _pub.enqueue(f"{_pfx}/{_sid}/control/off_grid_mode", "Off-Grid" if state == 1 else "On-Grid", qos=1)
                        if state == 1:
                            _pub.enqueue(f"{_pfx}/{_sid}/status/status_grid_status", "SimulatedOffGrid", qos=1)
                        logger.debug(f"[{_sid}] Optimistic MQTT publish after off_grid_mode command")
                except Exception as _pub_exc:
                    logger.warning(f"[{self.short_id}] optimistic MQTT publish failed: {_pub_exc}")


            # Phase 107: Transient Payload Caching for Composite Dispatches
            # tou_saved_dispatches is intentionally excluded — it has its own active handler below
            elif slug in (
                "dispatch_duration", "dispatch_target_soc", "dispatch_power", "dispatch_method",
                "dispatch_stop_soc",
                "emergency_backup_duration", "emergency_backup_duration_type", "emergency_backup_resume_mode"
            ):
                if "ha_input_cache" not in self.context:
                    self.context["ha_input_cache"] = {}
                self.context["ha_input_cache"][slug] = value
                logger.info(f"[{self.short_id}] Cached transient HA payload array: {slug}={value!r}")
                return {"ok": True, "slug": slug, "value": value, "result": "cached_in_memory"}

            elif slug in ("min_discharge_soc", "max_charge_soc"):
                # Phase C: Battery Status SOC limits — persist to DB + update in-memory context
                try:
                    soc_val = int(float(value))
                except (TypeError, ValueError):
                    return {"ok": False, "slug": slug, "error": f"Invalid value: {value!r}"}

                if slug == "min_discharge_soc" and not (0 <= soc_val <= 95):
                    return {"ok": False, "slug": slug, "error": f"min_discharge_soc {soc_val}% out of range (0–95%)"}
                if slug == "max_charge_soc" and not (20 <= soc_val <= 100):
                    return {"ok": False, "slug": slug, "error": f"max_charge_soc {soc_val}% out of range (20–100%)"}

                from src.services.db import set_config_value as _set_cfg
                db_key = f"gw_{self.short_id}_{slug}"
                await _set_cfg(db_key, soc_val)
                # Also update in-memory context so next gwObj read sees it immediately
                self.context[slug] = soc_val
                logger.info(f"[{self.short_id}] {slug} persisted → {soc_val}% (DB key: {db_key!r})")
                return {"ok": True, "slug": slug, "value": soc_val, "result": "persisted"}

            elif slug == "tou_saved_dispatches":
                preset_name = (value or "").strip()
                logger.info(f"[{self.short_id}] TOU Saved Dispatch selected: {preset_name!r}")

                if preset_name == "Stop / Restore":
                    res = await self.cloud_dispatch.stop()
                    # Report the schedule restore, not just the stop. This said
                    # "dispatch stopped" whatever happened, so a restore that
                    # found no backup was indistinguishable from one that
                    # worked — and the TOU schedule silently stayed overwritten.
                    _tou = res.get("tou_restore", "unknown")
                    delta_msg = (
                        f"TOU Saved Dispatch: Stop / Restore — dispatch stopped, "
                        f"mode restored to {res.get('restored_mode')!r}, "
                        f"TOU schedule restore: {_tou}"
                    )
                    if _tou in ("no_backup_file", "unknown"):
                        logger.warning(
                            f"[{self.short_id}] {delta_msg} — the TOU schedule was NOT "
                            "restored; no backup existed to restore from"
                        )
                    else:
                        logger.info(f"[{self.short_id}] {delta_msg}")
                    ok = res.get("success", res.get("ok", False))
                else:
                    from src.main import get_app_state as _gas
                    _presets_mgr = _gas().get("schedule_presets")
                    if _presets_mgr is None:
                        logger.error(f"[{self.short_id}] TOU Saved Dispatch: schedule_presets not initialised")
                        return {"ok": False, "error": "Schedule presets not initialised"}

                    preset_result = _presets_mgr.load_preset(preset_name)
                    if not preset_result.get("success"):
                        err = preset_result.get("error", f"Preset '{preset_name}' not found")
                        logger.error(f"[{self.short_id}] TOU Saved Dispatch: {err}")
                        return {"ok": False, "error": err}

                    schedule_data = preset_result.get("schedule", [])

                    # Back up first. Applying a preset overwrites the live TOU
                    # schedule, and this path does not go through
                    # cloud_dispatch, which is the only thing that was taking
                    # backups. So "Export to Grid (Always)" replaced the real
                    # schedule with a single all-export tariff and saved
                    # nothing, and the later "Stop / Restore" had nothing to
                    # restore. The 300s guard inside means a second preset
                    # applied straight after does not overwrite the backup with
                    # the already-overwritten schedule.
                    try:
                        await self.cloud_dispatch.backup_tou_schedule()
                    except Exception:
                        logger.warning(
                            f"[{self.short_id}] TOU backup before preset failed — "
                            "restore may not be possible", exc_info=True,
                        )

                    res = await self.set_tou_schedule({"schedule": schedule_data, "operation": 0})
                    ok = res.get("ok", False)
                    delta_msg = f"TOU Saved Dispatch: Applied preset '{preset_name}' ({len(schedule_data)} blocks) — ok={ok}"
                    logger.info(f"[{self.short_id}] {delta_msg}")

                # Persist selection in transient cache so polls reflect current choice
                self.context.setdefault("ha_input_cache", {})["tou_saved_dispatches"] = preset_name

                # Optimistic MQTT publish — update HA immediately rather than waiting for next poll
                try:
                    from src.main import get_app_state as _gas2
                    _pub2 = _gas2().get("publisher")
                    if _pub2:
                        _pfx2 = _pub2.topic_prefix
                        _pub2.enqueue(
                            f"{_pfx2}/{self.short_id}/control/tou_saved_dispatches",
                            preset_name, qos=1
                        )
                        logger.debug(f"[{self.short_id}] Optimistic MQTT publish: tou_saved_dispatches={preset_name!r}")
                except Exception as _pub_exc2:
                    logger.warning(f"[{self.short_id}] Optimistic tou_saved_dispatches publish failed: {_pub_exc2}")

                if not ok:
                    return {"ok": False, "slug": slug, "error": f"TOU dispatch '{preset_name}' failed — see logs"}

                result = {"ok": True}

            elif slug == "dispatch_action":
                action = value.strip().title()
                cache = self.context.get("ha_input_cache", {})
                
                if action in ("Idle", "Stop"):
                    res = await self.cloud_dispatch.stop()
                    delta_msg = f"Dispatch Action: Stopped via Cloud API"
                    result = res
                else:
                    power_kw = float(cache.get("dispatch_power", 2.0))
                    duration_min = int(float(cache.get("dispatch_duration", 60)))
                    target_soc = int(float(cache.get("dispatch_target_soc", 100)))
                    stop_soc = int(float(cache.get("dispatch_stop_soc", 30)))

                    if action == "Charge":
                        res = await self.cloud_dispatch.charge(power_kw=power_kw, max_soc=target_soc, duration_min=duration_min)
                        delta_msg = f"Dispatch Action: Charge {power_kw}kW to {target_soc}% for {duration_min}m"
                        result = res
                    elif action == "Discharge":
                        res = await self.cloud_dispatch.discharge(power_kw=power_kw, duration_min=duration_min, min_soc=stop_soc)
                        delta_msg = f"Dispatch Action: Discharge {power_kw}kW (floor {stop_soc}%) for {duration_min}m"
                        result = res
                    else:
                        return {"ok": False, "error": f"Invalid dispatch action: {action}"}

                # Phase D3: write-back cache — persist dispatch action for next poll cycle
                self.context.setdefault("ha_input_cache", {})["dispatch_action"] = action

                if not result.get("success"):
                    return {"ok": False, "slug": slug, "error": result.get("error", "Unknown error")}

                # Optimistically update dynamic status cache
                if not self.status.last_data:
                    self.status.last_data = {}
                self.status.last_data["dispatch_action"] = action
                if action in ("Charge", "Discharge"):
                    if "mode" not in self.status.last_data or not isinstance(self.status.last_data["mode"], dict):
                        self.status.last_data["mode"] = {}
                    self.status.last_data["mode"]["runtime_mode"] = f"Manual {action}"
                elif action in ("Idle", "Stop"):
                    if "mode" in self.status.last_data and isinstance(self.status.last_data["mode"], dict):
                        self.status.last_data["mode"]["runtime_mode"] = self.status.last_data["mode"].get("work_mode_desc", "Unknown")

                # Force-trigger optimistic MQTT publish to broadcast the updated state instantly
                if self._on_data:
                    try:
                        await self._on_data(self.full_serial, self.status.last_data)
                        logger.debug(f"[{self.short_id}] Optimistic MQTT publish: dispatch_action={action!r}")
                    except Exception as _pub_exc:
                        logger.warning(f"[{self.short_id}] Optimistic dispatch_action publish failed: {_pub_exc}")

            elif slug.endswith("_mode") and slug.startswith("smart_circuit_"):
                # e.g. "smart_circuit_1_mode" -> String: "Manual" or "Schedule"
                idx = int(slug.split("_")[2]) - 1
                circuit = idx + 1
                
                # We need to map "Manual" back to its current ON/OFF state because "Manual" just means exiting "Schedule"
                # If switching TO "Schedule", we just pass "SCHEDULE" to the upstream API.
                # If switching TO "Manual", we must send either "ON" or "OFF" to clear schedule mode.
                if value.lower() == "schedule":
                    state_arg = "SCHEDULE"
                else:
                    # Look up current state to revert to manual ON/OFF
                    sc_info = await client.get_smart_circuits_info()
                    sw_list = getattr(sc_info, "switches", None) or (sc_info.get("switches") if isinstance(sc_info, dict) else []) or sc_info.get("switchs", [])
                    target_switch = sw_list[idx] if idx < len(sw_list) else {}
                    current_state = target_switch.state if hasattr(target_switch, "state") else target_switch.get("state", 0)
                    state_arg = "ON" if current_state == 1 else "OFF"
                
                delta_msg = f"Smart Circuit {circuit} Mode: -> {value}"
                
                result = await client.set_smart_switch_state(
                    circuit=circuit,
                    state=state_arg
                )

            elif slug.startswith("smart_circuit_"):
                # e.g. "smart_circuit_1"
                idx = int(slug.split("_")[2]) - 1
                circuit = idx + 1
                state_arg = "ON" if _str_to_bool(value) else "OFF"
                delta_msg = f"Smart Circuit {circuit}: -> {state_arg}"
                
                # Call the validated upstream method explicitly matching the developer package signature
                result = await client.set_smart_switch_state(
                    circuit=circuit,
                    state=state_arg
                )

            elif slug == "generator_mode":
                # Manual vs Auto Schedule abstraction
                gen_info = await client.get_generator_info()
                old_raw = gen_info.get("generatorMode", 0)
                old_mode = "Auto Schedule" if old_raw == 1 else "Manual"
                
                # If switching from Manual to Auto -> API Mode 1
                # If switching from Auto to Manual -> API Mode 0 (Off) by default to prevent unintended run
                new_raw = 1 if value.lower() == "auto schedule" else 0
                delta_msg = f"Generator Mode: {old_mode} -> {value}"

                # Refused, not attempted. `set_generator_mode()` is misnamed:
                # it posts `manuSw`, a manual start/stop acting on generator
                # STATE, while the mode lives in a separate `mode` field
                # (DEF-GEN-MODE-WRITES-MANUSW — 35 captured writes correlated).
                # The library exposes no setter for `mode` at all.
                #
                # So this control could never change the mode. Worse, it would
                # send an unrecognised manual command to a generator: the
                # value→effect mapping for `manuSw` is explicitly ASSUMED
                # upstream, from a single sample. Issuing an unknown start/stop
                # because someone picked a schedule is not a failure mode worth
                # having on a machine that burns fuel.
                return {
                    "ok": False,
                    "slug": slug,
                    "error": (
                        "Generator mode cannot be set. The cloud API method for "
                        "it writes the manual start/stop field, not the mode, "
                        "and no setter for the mode field is available. Change "
                        "it in the FranklinWH app."
                    ),
                }

            elif slug == "generator_run":
                # Only explicitly maps to ON (2) or OFF (0). Implicitly sets mode into Manual
                state = 2 if _str_to_bool(value) else 0
                
                # ENFORCING HARDWARE RULES: Manual start ONLY permitted when off-grid
                if state == 2:
                    stats = await client.get_stats()
                    if not getattr(stats.current, "off_grid", False):
                        return {"ok": False, "slug": slug, "error": "Hardware constraint: Generator can only be manually started while disconnected from the external grid."}
                        
                gen_info = await client.get_generator_info()
                old_raw = gen_info.get("generatorMode", 0)
                old_state = "On" if old_raw == 2 else "Off"
                new_state = "On" if state == 2 else "Off"
                delta_msg = f"Generator Relay: {old_state} -> {new_state}"
                
                # set_generator_mode() posts `manuSw`, which IS the right
                # field for a manual start/stop — the method is misnamed, not
                # wrong, for this use.
                #
                # The value mapping is not established upstream: the one clean
                # sample shows `manuSw: 2` preceding Running -> Cooldown, which
                # reads as a STOP, while this passes 2 for ON. Logged so a
                # surprise here is traceable rather than mysterious.
                logger.warning(
                    f"[{self.short_id}] generator_run -> manuSw={state}. The "
                    "value mapping is unverified upstream "
                    "(DEF-GEN-MODE-WRITES-MANUSW); confirm the generator did "
                    "what you asked."
                )
                result = await client.set_generator_mode(mode=state)
                
            elif slug == "generator_start_soc":
                soc_val = int(float(value))
                gen_info = await client.get_generator_info()
                stop_soc = gen_info.get("socHighThreshold", 80)
                
                old_soc = gen_info.get("socLowThreshold", 0)
                delta_msg = f"Generator Start SOC: {old_soc}% -> {soc_val}%"
                
                # set_generator_schedule() does not exist on the cloud client and
                # never has — the library offers no setter for the generator SoC
                # thresholds at all. The call raised AttributeError into a broad
                # except, so the control reported a cloud failure rather than a
                # missing capability. Same shape as Client.raw().
                return {
                    "ok": False,
                    "slug": slug,
                    "error": (
                        "Generator start SoC cannot be set from here — the cloud "
                        "API provides no setter for it. Change it in the "
                        "FranklinWH app."
                    ),
                }
                
            elif slug == "generator_stop_soc":
                soc_val = int(float(value))
                gen_info = await client.get_generator_info()
                start_soc = gen_info.get("socLowThreshold", 20)
                
                old_soc = gen_info.get("socHighThreshold", 0)
                delta_msg = f"Generator Stop SOC: {old_soc}% -> {soc_val}%"
                
                # No setter exists — see generator_start_soc above.
                return {
                    "ok": False,
                    "slug": slug,
                    "error": (
                        "Generator stop SoC cannot be set from here — the cloud "
                        "API provides no setter for it. Change it in the "
                        "FranklinWH app."
                    ),
                }

            else:
                return {"ok": False, "slug": slug, "error": f"Unknown or untranslated command slug: {slug!r}"}

            logger.info(f"[{self.short_id}] Command {slug}={value!r} OK")
            
            if delta_msg:
                await log_admin_audit(f"Config Change", "GatewayService", "system", f"[{self.short_id}] {delta_msg}")
                
            return {"ok": True, "slug": slug, "value": value, "result": str(result)}

        except Exception as exc:
            err_str = str(exc)
            if "Expecting value: line 1" in err_str:
                err_str = "Hardware Error: The requested Smart Circuit relay module did not acknowledge the command. Ensure the circuit hardware actually exists (e.g. attempting to control uninstalled Circuit 3 may timeout the RS485 bus)."
                
            logger.error(f"[{self.short_id}] Command {slug} failed: {err_str}")
            self._handle_token_expiration(exc)
            return {"ok": False, "slug": slug, "error": err_str}

    # ------------------------------------------------------------------
    # Advanced Live Controls (Operating Mode)
    # ------------------------------------------------------------------

    async def get_operating_mode(self) -> dict:
        """Fetch current operating mode and scheduling details via the Cloud API."""
        try:
            client = await self._get_or_create_client()
            mode = await client.get_mode()
            return {"ok": True, "mode": mode}
        except Exception as exc:
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def get_tou_schedule(self) -> dict:
        """Fetch the granular multi-season/day-type TOU detail from the Cloud API."""
        try:
            client = await self._get_or_create_client()
            res = await client.get_tou_dispatch_detail()
            return {"ok": True, "detail": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] get_tou_schedule failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def get_live_tou_schedule(self) -> dict:
        """Fetch the active "live" TOU schedule (today's active day/season blocks) from the Cloud API."""
        try:
            client = await self._get_or_create_client()
            res = await client.get_tou_info(2)
            return {"ok": True, "schedule": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] get_live_tou_schedule failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def get_mode_soc_reserves(self) -> dict:
        """Fetch all mode reserve SOC limits via the Cloud API."""
        try:
            client = await self._get_or_create_client()
            reserves = await client.get_all_mode_soc()
            return {"ok": True, "reserves": reserves}
        except Exception as exc:
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def get_tou_raw(self) -> dict:
        """Return the complete raw getGatewayTouListV2 Cloud API response.

        Unlike get_mode_soc_reserves() which returns a simplified subset,
        this returns every field from the vendor API including id, oldIndex,
        dischargeDepthSoc, multiSOCFlag, energyIncentivesType, etc.

        Used exclusively by the Operating Mode diagnostics modal to show
        the full unfiltered API payload alongside the enum-normalised view.
        """
        try:
            client = await self._get_or_create_client()
            raw = await client.get_gateway_tou_list()
            return {"ok": True, "result": raw}
        except Exception as exc:
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_tou_schedule(self, payload: dict) -> dict:
        """Saves a custom TOU schedule via the Cloud API."""
        try:
            client = await self._get_or_create_client()
            schedule = payload.get('schedule', [])
            operation = payload.get('operation', 0)
            default_mode = payload.get('default_mode', 'SELF')
            default_tariff = payload.get('default_tariff', 'OFF_PEAK')
            
            res = await client.set_tou_schedule(
                touMode="CUSTOM",
                touSchedule=schedule,
                operation=operation,
                default_mode=default_mode,
                default_tariff=default_tariff
            )
            return {"ok": True, "detail": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_tou_schedule failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_tou_schedule_multi(self, strategy_list: list) -> dict:
        """Save a multi-season / multi-day-type TOU schedule via the Cloud API.

        Must be used whenever the existing schedule has:
          - more than one season (strategyList length > 1), OR
          - a weekday/weekend day-type split (dayType=1 or 2, not just dayType=3).

        Calling the flat set_tou_schedule() in those cases silently destroys the
        multi-season structure. This method safely round-trips the full strategyList.
        """
        try:
            client = await self._get_or_create_client()
            res = await client.set_tou_schedule_multi(strategy_list)
            return {"ok": True, "detail": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_tou_schedule_multi failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def apply_tariff_template(self, tariff_id: int, name: str = "") -> dict:
        """Apply a FranklinWH tariff template to this gateway (Workflow A — Phase 3).

        This is a WRITE operation: it calls apply_tariff_template (saveTouDispatchUseTemplate)
        which replaces the active TOU schedule with the server-side utility tariff template.

        Must NOT be called without explicit user confirmation — it overwrites any custom schedule.
        """
        try:
            client = await self._get_or_create_client()
            # template_id, not tariff_id. The library's parameter has always
            # been template_id; passing tariff_id raised TypeError into the
            # except below, which logged it as "apply_tariff_template failed"
            # and returned a cloud error for a local mistake.
            res = await client.apply_tariff_template(template_id=tariff_id, name=name)
            return {"ok": True, "detail": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] apply_tariff_template failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_operating_mode(self, work_mode: int, soc: int = None, forever: int = None, next_mode: int = None, duration: int = None, caller: str = None) -> dict:
        """Set the operating mode with optional advanced scheduling (Emergency Backup).

        Args:
            work_mode:  1=Time-of-Use  2=Self-Consumption  3=Emergency Backup
            soc:        Optional reserve SOC % to set simultaneously.
            forever:    Emergency Backup indefinite flag.
            next_mode:  Mode to resume after Emergency Backup expires.
            duration:   Emergency Backup duration in minutes.
            caller:     Optional human-readable string identifying the caller
                        (e.g. 'AB:Rule:Not TOU Mode:7037b97f', 'TOU:reset_step1').
                        Included in the Audit Trail so mode changes are traceable.
        """
        try:
            from src.services.db import log_admin_audit
            client = await self._get_or_create_client()

            old_mode_dict = await client.get_mode()
            old_mode_int = old_mode_dict.get("workMode", "Unknown")

            # The upstream library safely scrubs None values to prevent Cloud API 400 Bad Requests
            result = await client.set_mode(
                requestedOperatingMode=work_mode,
                requestedSOC=soc,
                reqbackupForeverFlag=forever,
                reqnextWorkMode=next_mode,
                reqdurationMinutes=duration
            )

            if result is False:
                logger.error(f"[{self.short_id}] upstream client rejected mode transition for {work_mode}")
                return {"ok": False, "error": f"Upstream Cloud API structurally rejected the mode transition payload for operating mode {work_mode}."}

            # If standard mode switch was successful AND custom Reserve SOC requested,
            # we invoke the explicit Reserve SOC mutation.
            if soc is not None:
                await client.update_soc(requestedSOC=soc, workMode=work_mode)

            MODE_LABELS = {1: "Time-of-Use", 2: "Self-Consumption", 3: "Emergency Backup"}
            old_label = MODE_LABELS.get(old_mode_int, str(old_mode_int))
            new_label = MODE_LABELS.get(work_mode, str(work_mode))
            caller_str = f" | requested by: {caller}" if caller else ""
            audit_detail = (
                f"[{self.short_id}] Operating Mode: {old_label} ({old_mode_int}) → {new_label} ({work_mode}){caller_str}"
            )
            logger.info(f"[{self.short_id}] set_operating_mode({work_mode}/{new_label}){caller_str}")
            await log_admin_audit("Mode Change", "GatewayService", "system", audit_detail)
            
            # Affirmatively update the local cache so the UI reflects the change instantly
            self.context["last_work_mode"] = work_mode
            self.context["last_work_mode_desc"] = new_label
            
            # Optimistically update dynamic status cache
            if not self.status.last_data:
                self.status.last_data = {}
            if "mode" not in self.status.last_data or not isinstance(self.status.last_data["mode"], dict):
                self.status.last_data["mode"] = {}
            self.status.last_data["mode"]["work_mode"] = work_mode
            self.status.last_data["mode"]["work_mode_desc"] = new_label
            self.status.last_data["mode"]["runtime_mode"] = new_label
            self.status.last_data["operating_mode_id"] = work_mode
            self.status.last_data["operating_mode"] = new_label
            if soc is not None:
                self.status.last_data["backup_reserve_soc"] = soc
                
            # Force-trigger optimistic MQTT publish to broadcast the updated state instantly
            if self._on_data:
                try:
                    await self._on_data(self.full_serial, self.status.last_data)
                    logger.debug(f"[{self.short_id}] Optimistic MQTT publish: operating_mode={new_label!r}")
                except Exception as _pub_exc:
                    logger.warning(f"[{self.short_id}] Optimistic operating_mode publish failed: {_pub_exc}")
            
            # Phase 108: Invalidate client cache if supported to force next poll to be fresh
            if hasattr(client, "invalidate_cache"):
                client.invalidate_cache("get_mode")
                client.invalidate_cache("get_stats")
            
            return {"ok": True, "result": result}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_operating_mode failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def update_reserve_soc(self, soc: int, work_mode: int) -> dict:
        """Update the reserve SOC limit for a specific mode."""
        try:
            from src.services.db import log_admin_audit
            client = await self._get_or_create_client()
            
            old_reserves = await client.get_all_mode_soc()
            old_soc = next((m.get("socLowThreshold", "Unknown") for m in old_reserves if m.get("workMode") == work_mode), "Unknown")
            
            result = await client.update_soc(requestedSOC=soc, workMode=work_mode)
            logger.info(f"[{self.short_id}] update_reserve_soc({soc}%, mode={work_mode}) -> {result}")
            
            await log_admin_audit("Reserve Change", "GatewayService", "system", f"[{self.short_id}] Reserve SOC (Mode {work_mode}): {old_soc}% -> {soc}%")
            
            return {"ok": True, "result": result}
        except Exception as exc:
            logger.error(f"[{self.short_id}] update_reserve_soc failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}
    async def set_offgrid(self, enabled: bool) -> dict:
        """Toggle the gateway Off-Grid mode (islanding)."""
        try:
            from src.services.db import log_admin_audit
            from franklinwh_cloud.models import GridStatus
            client = await self._get_or_create_client()
            
            status = GridStatus.OFF if enabled else GridStatus.NORMAL
            result = await client.set_grid_status(status=status)
            
            logger.info(f"[{self.short_id}] set_offgrid({enabled}) -> {result}")
            await log_admin_audit("Offgrid Change", "GatewayService", "system", f"[{self.short_id}] Off-Grid: {enabled}")

            # Optimistic MQTT publish: immediately reflect command in HA without waiting for next poll
            try:
                from src.main import get_app_state
                _pub = get_app_state().get("publisher")
                if _pub:
                    _pfx = _pub.topic_prefix
                    _sid = self.short_id
                    _pub.enqueue(f"{_pfx}/{_sid}/control/off_grid_mode", "Off-Grid" if enabled else "On-Grid", qos=1)
                    if enabled:
                        _pub.enqueue(f"{_pfx}/{_sid}/status/status_grid_status", "SimulatedOffGrid", qos=1)
                    logger.debug(f"[{_sid}] Optimistic MQTT publish after set_offgrid({enabled})")
            except Exception as _pub_exc:
                logger.warning(f"[{self.short_id}] optimistic MQTT publish failed: {_pub_exc}")

            return {"ok": True, "result": result}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_offgrid failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_storm_edge(self, enabled: bool) -> dict:
        """Toggle the gateway Storm Guard feature."""
        try:
            from src.services.db import log_admin_audit
            client = await self._get_or_create_client()
            
            storm_val = 1 if enabled else 0
            result = await client.set_storm_settings(stormEn=storm_val)
            
            logger.info(f"[{self.short_id}] set_storm_edge({enabled}) -> {result}")
            await log_admin_audit("Storm Guard", "GatewayService", "system", f"[{self.short_id}] Storm Guard: {enabled}")
            return {"ok": True, "result": result}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_storm_edge failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_apower_led(self, enabled: bool) -> dict:
        """Toggle the aPower LED strip across all connected batteries."""
        try:
            from src.services.db import log_admin_audit
            client = await self._get_or_create_client()
            
            apowers_info = await client.get_apower_info()
            batteries = apowers_info.get("result", [])
            success_count = 0
            res_history = []
            
            for ap in batteries:
                sn = ap.get("fhpSn")
                if not sn: continue
                
                # Experimental payload structure based on typical FranklinWH MQTT models
                payload = {
                    "fhpSn": sn,
                    "opt": 1,
                    # Provide multiple common keys attempting to toggle the strip
                    "workStatus": 1 if enabled else 0,
                    "enable": 1 if enabled else 0,
                    "ledEn": 1 if enabled else 0
                }
                
                try:
                    res = await client.led_light_settings(mode="2", dataArea=payload)
                    res_history.append(res)
                    success_count += 1
                except Exception as inner_e:
                    logger.warning(f"[{self.short_id}] set_apower_led failed for {sn}: {inner_e}")
                
            logger.info(f"[{self.short_id}] set_apower_led({enabled}) triggered on {success_count} batteries.")
            await log_admin_audit("aPower LED", "GatewayService", "system", f"[{self.short_id}] aPower LED Strip: {enabled}")
            return {"ok": True, "success_count": success_count, "results": res_history}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_apower_led failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}

    async def set_smart_circuit(self, circuit_id: int, state: str) -> dict:
        """Toggle a specific Smart Circuit."""
        try:
            from src.services.db import log_admin_audit
            client = await self._get_or_create_client()
            turn_on = str(state).lower() in ("1", "true", "on", "yes")
            
            logger.info(f"[{self.short_id}] Triggering set_smart_switch_state: {circuit_id} -> {'ON' if turn_on else 'OFF'}")
            res = await client.set_smart_switch_state(circuit=circuit_id, state="ON" if turn_on else "OFF")
            await log_admin_audit("Smart Circuit", "GatewayService", "system", f"[{self.short_id}] Circuit {circuit_id} set to {'ON' if turn_on else 'OFF'} by Edge Engine")
            return {"ok": True, "result": res}
        except Exception as exc:
            logger.error(f"[{self.short_id}] set_smart_circuit failed: {exc}")
            self._handle_token_expiration(exc)
            return {"ok": False, "error": str(exc)}
    # Client factory
    # ------------------------------------------------------------------
    async def schedule_bms_recording(self, battery_sn: str, count: int, interval: int) -> None:
        """Daemon task: Poll BMS info organically, accumulating directly into SQLite."""
        if battery_sn == "ALL" or not battery_sn:
            # 1. Discover all connected battery serials
            apowers = self.status.last_data.get("apower_serial_numbers", [])
            if isinstance(apowers, str):
                serials = [x.strip() for x in apowers.split(",") if x.strip()]
            else:
                serials = list(apowers)
                
            if not serials:
                # Fallback to discover online battery serials
                try:
                    client = await self._get_or_create_client()
                    apowers_info = await client.get_apower_info()
                    batteries = apowers_info.get("result", [])
                    serials = [ap.get("fhpSn") for ap in batteries if ap.get("fhpSn")]
                except Exception as e:
                    logger.error(f"Cannot discover battery serials for ALL BMS recording: {e}")
                    return
            
            if not serials:
                logger.warning(f"No batteries found for ALL BMS recording on gateway {self.short_id}")
                return
                
            logger.info(f"Scheduling parallel staggered BMS recording for {len(serials)} batteries on gateway {self.short_id}: {serials}")
            
            # Start a separate schedule_bms_recording task for each battery, staggered by 1.0 seconds
            for idx, sn in enumerate(serials):
                async def run_staggered(target_sn=sn, delay=idx * 1.0):
                    await asyncio.sleep(delay)
                    await self._schedule_bms_recording_single(target_sn, count, interval)
                asyncio.create_task(run_staggered())
        else:
            await self._schedule_bms_recording_single(battery_sn, count, interval)

    async def _schedule_bms_recording_single(self, battery_sn: str, count: int, interval: int) -> None:
        """Execution task: Poll a single battery BMS info, accumulating directly into SQLite."""
        from datetime import datetime
        from src.services.db import save_bms_session
        
        logger.info(f"Starting single BMS recording loop: {battery_sn} ({count} sweeps @ {interval}s)")
        session_buffer = {"times": [], "voltages": [], "temps": []}
        start_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            client = await self._get_or_create_client()
        except Exception as e:
            logger.error(f"Cannot start BMS recording task for {battery_sn}; target offline: {e}")
            return
            
        for i in range(count):
            try:
                b_info = await client.get_bms_info(battery_sn)
                now_str = datetime.now().strftime("%H:%M:%S")
                # Bind directly to our detached session buffer
                session_buffer["times"].append(now_str)
                session_buffer["voltages"].append(b_info.get("batVolt", []))
                session_buffer["temps"].append(b_info.get("batTemp", []))
                logger.debug(f"[Daemonic BMS] Tick {i+1}/{count} locked for {battery_sn}")
            except Exception as e:
                logger.warning(f"BMS Record Daemon failed on tick {i} for {battery_sn}: {e}")
                
            if i < count - 1:
                await asyncio.sleep(interval)
                
        # Hard commit to persistence layer
        if session_buffer["times"]:
            name = f"Background Recording {start_label}"
            try:
                await save_bms_session(self.short_id, battery_sn, name, json.dumps(session_buffer))
                logger.info(f"Detached BMS recording session securely committed for {battery_sn}: {name}")
                # Push completion notification — automations tab polls and toasts
                try:
                    from src.routes.api_system import push_notification
                    push_notification("record_bms", session_name=name, gateway_id=self.short_id, battery_sn=battery_sn, samples=len(session_buffer["times"]))
                except Exception as _notif_exc:
                    logger.debug(f"BMS notification push skipped: {_notif_exc}")
            except Exception as ex:
                logger.error(f"SQLite finalization aborted for BMS background sweep on {battery_sn}: {ex}")


    @staticmethod
    async def _default_client_factory(email: str, password: str, gateway_serial: str = ""):
        """Create and authenticate a franklinwh-cloud Client.
        
        Uses TokenFetcher → get_token() → Client(fetcher, serial).
        gateway_serial is the full aGate serial number required by Client.
        """
        try:
            from franklinwh_cloud.client import Client, TokenFetcher
            from franklinwh_cloud.metrics import RateLimiter
            from franklinwh_cloud import DEFAULT_CACHE
        except ImportError:
            raise RuntimeError(
                "franklinwh-cloud library not installed. "
                "Run: pip install franklinwh-cloud"
            )
        fetcher = TokenFetcher(email, password)
        await fetcher.get_token()

        # Load persisted rate-limit overrides (set via SysAdmin panel in MQTT Admin).
        # Increased defaults: caching (DEFAULT_CACHE + TTL extensions) reduces actual
        # footprint to ~2 calls/cycle at 30s → ~240/hr. Guard exists to catch rogue code.
        # Prev defaults: 60/500/5000. New: 120/1500/15000.
        from src.services import db as _db
        _rl_per_min  = int(await _db.get_config_value("rate_limit_per_minute",  120))
        _rl_per_hour = int(await _db.get_config_value("rate_limit_per_hour",   1500))
        _rl_daily    = int(await _db.get_config_value("rate_limit_daily_budget", 15000))

        return Client(
            fetcher,
            gateway=gateway_serial,
            rate_limiter=RateLimiter(
                calls_per_minute=_rl_per_min,
                calls_per_hour=_rl_per_hour,
                daily_budget=_rl_daily,
            ),
            tolerate_stale_data=True,
            stale_cache_ttl=300,
            cache={
                **DEFAULT_CACHE,
                # get_tou_info is NOT in DEFAULT_CACHE so it was previously uncached —
                # called every poll cycle (~30s). TOU blocks change on 30-min schedule
                # boundaries: 60s TTL eliminates redundant calls with no data loss.
                "get_tou_info": 60,
                # get_accessories_power_info already in DEFAULT_CACHE at 120s.
                # Extend to 180s — circuit loads change slowly, get_stats() has
                # switch_1_load/switch_2_load as a fallback anyway.
                "get_accessories_power_info": 180,
            },
            client_headers={
                "User-Agent": "franklinwh-ha-integrator/v0.1.16",
                "X-FranklinWH-Client": "franklinwh-ha-integrator"
            },
            track_python_methods=True,  # populate calls_by_python_method in metrics snapshot
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _str_to_bool(v: str) -> bool:
    """Convert MQTT payload string to bool. '1'/'true'/'on' → True, else False."""
    return str(v).lower() in ("1", "true", "on", "yes")


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses / nested objects to plain dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, Enum):
        return obj.value
    return obj
