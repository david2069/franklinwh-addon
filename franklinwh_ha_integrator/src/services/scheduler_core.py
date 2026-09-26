import asyncio
import time
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

logger = logging.getLogger(__name__)

LEGACY_MAP = {
    "grid.status": "grid_status",
    "battery.soc": "battery_soc",
    # battery.status → run_status_desc (human label: "Charging" / "Discharging" / "Standby")
    # Use battery.run_status_int if you need the raw RUN_STATUS integer (0/1/2/...)
    "battery.status": "run_status_desc",
    "battery.run_status_int": "run_status",      # raw int for history graphs
    "battery.mode": "operating_mode",            # Migrated from work_mode
    "home.load": "home_kw",                # Migrated from home_load
    "grid.power": "grid_kw",               # Migrated from grid_use
    "solar.power": "solar_kw",             # Migrated from solar_production
    "generator.status": "generator_status",
    "relay.generator": "power.relays.generator",
    "relay.grid1": "power.relays.grid1",
    "relay.grid2": "power.relays.grid2",
    "relay.solar_1": "power.relays.solar1",
    "relay.solar_2": "power.relays.pv2",
    "relay.blackstart": "power.relays.blackStart",
    "relay.smart_circuit_1": "smart_circuit_1_state",
    "relay.smart_circuit_2": "smart_circuit_2_state",
    "relay.smart_circuit_3": "smart_circuit_3_state",
    "storm_hedge.enabled": "storm.enabled",
    "grid.offgrid_status": "grid_status",
    # Ambient temperature — forward-compat alias
    "agate.temperature": "agate.ambient_temp",
}

def get_nested(d: dict, path: str, extra_context: dict = None) -> Any:
    # First check extra_context if provided (for pricing.* and dispatch.* overrides)
    if extra_context:
        keys = path.split('.')
        ref = extra_context
        found_in_extra = True
        for k in keys:
            if isinstance(ref, dict) and k in ref:
                ref = ref[k]
            else:
                found_in_extra = False
                break
        if found_in_extra:
            return ref

    mapped_path = LEGACY_MAP.get(path, path)
    keys = mapped_path.split('.')
    ref = d
    for k in keys:
        if isinstance(ref, dict) and k in ref:
            ref = ref[k]
        else:
            return None
    return ref

import string

class SafeFormatter(string.Formatter):
    def __init__(self, context):
        super().__init__()
        self.context = context

    def get_field(self, field_name, args, kwargs):
        if field_name in self.context:
            return self.context[field_name], field_name
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError, IndexError):
            return "{" + field_name + "}", field_name

def expand_placeholders(val: Any, context: dict[str, Any]) -> Any:
    if isinstance(val, str):
        try:
            return SafeFormatter(context).format(val)
        except Exception:
            return val
    elif isinstance(val, dict):
        return {k: expand_placeholders(v, context) for k, v in val.items()}
    elif isinstance(val, list):
        return [expand_placeholders(v, context) for v in val]
    return val


def _flatten_to_dotted(d: dict, prefix: str, out: dict) -> None:
    """
    Recursively flatten a nested dict into `out` using dotted keys, so the
    SafeFormatter's flat-key lookup can resolve any depth.

    Example: {"home_load": {"global": {"hvac": {"controllability": "control_only"}}}}
    becomes  {"home_load.global.hvac.controllability": "control_only", ...}.

    Only leaf (non-dict) values are recorded; intermediate dict values are
    skipped because str.format cannot render them usefully.
    """
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            _flatten_to_dotted(v, key, out)
        else:
            out[key] = v

def evaluate_conditions(live_data: dict, condition_logic: str, conditions: list, extra_context: dict = None) -> tuple[bool, list, list]:
    if not conditions:
        return True, [], []
        
    failed_reasons = []
    passed_reasons = []
    condition_results = []
    
    def get_nested_local(d, path):
        return get_nested(d, path, extra_context)

    for idx, cond in enumerate(conditions):
        # Skip conditions explicitly disabled in the rule builder
        if not cond.get("enabled", True):
            continue
        metric = cond.get("metric")
        op = cond.get("operator")
        req_val = cond.get("value")

        # ── RHS metric reference (value_ref) ─────────────────────────────────
        # If the condition was built with a metric reference on the right side
        # (e.g. value_ref="dispatch.min_soc_limit") resolve it from live_data /
        # extra_context rather than using the literal cond.value string.
        value_ref = cond.get("value_ref", "")
        if value_ref:
            resolved = get_nested_local(live_data, value_ref)
            if resolved is not None:
                req_val = resolved
            # else: fall through to literal cond.value (safe degradation)

        if metric == "system.time":
            actual_val = datetime.now().strftime("%H:%M:%S")
        else:
            actual_val = get_nested_local(live_data, metric)
            if actual_val is None:
                failed_reasons.append(f"Metric '{metric}' is currently offline/unavailable")
                condition_results.append(False)
                continue

        av = actual_val
        rv = req_val
        
        # Literal macros
        if op == "is_true":
            if metric in ("grid.status", "grid.offgrid_status", "grid_status"):
                c_pass = str(actual_val).lower().strip() == "connected"
            else:
                c_pass = str(actual_val).lower().strip() in ('true', '1', 'on', 'yes')
        elif op == "is_false":
            if metric in ("grid.status", "grid.offgrid_status", "grid_status"):
                c_pass = str(actual_val).lower().strip() != "connected"
            else:
                c_pass = str(actual_val).lower().strip() in ('false', '0', 'off', 'no', 'none')
        elif op == "is_empty":
            c_pass = str(actual_val).strip() == ""
        else:
            try:
                # Handle backwards compatibility for grid.status boolean comparison
                if metric in ("grid.status", "grid.offgrid_status", "grid_status") and str(req_val).lower().strip() in ('true', 'false', '1', '0', 'on', 'off', 'yes', 'no'):
                    av = "true" if str(actual_val).lower().strip() == "connected" else "false"
                    rv = "true" if str(req_val).lower().strip() in ('true', '1', 'on', 'yes') else "false"
                else:
                    rv = float(req_val)
                    av = float(actual_val)
            except (ValueError, TypeError):
                rv = str(req_val).lower().strip()
                av = str(actual_val).lower().strip()
                
            c_pass = False
            if op == "==": c_pass = av == rv
            elif op == "!=": c_pass = av != rv
            elif op == ">=": c_pass = av >= rv
            elif op == "<=": c_pass = av <= rv
            elif op == ">": c_pass = av > rv
            elif op == "<": c_pass = av < rv
        
        if not c_pass:
            failed_reasons.append(f"Step {idx+1}: [{metric}] {av} {op} {rv} → False")
        else:
            passed_reasons.append(f"Step {idx+1}: [{metric}] {av} {op} {rv} → True")
        condition_results.append(c_pass)
        
    if condition_logic == "OR":
        matched = any(condition_results)
    else:
        matched = all(condition_results)
        
    return matched, failed_reasons, passed_reasons


def _site_tz():
    """The site's timezone for trigger construction, or None to accept the default.

    A trigger built explicitly — CronTrigger(hour=3) — takes the PROCESS local
    timezone at construction, not the scheduler's. Only the string form
    (add_job("cron", hour=3)) inherits from the scheduler. The container runs
    UTC, so every job registered here as "site-local" was in fact firing on UTC:
    the macro planner's 03:00 was 13:00 in Sydney, and the meso planner's
    00:05/06:05/12:05/18:05 were 10:05/16:05/22:05/04:05.

    TOU blocks, demand windows and export windows are local wall-clock, so this
    is the difference between a plan that matches the tariff and one that does
    not.
    """
    try:
        from zoneinfo import ZoneInfo
        from src.services.supervisor_settings import site_timezone

        zone = site_timezone()
        return ZoneInfo(zone) if zone else None
    except Exception:
        return None


async def get_solar_forecast_metrics(gateway_short_id: str, timezone_name: Optional[str] = None) -> dict:
    """
    Fetch the solar forecast and aggregate today_kwh, remaining_kwh, tomorrow_kwh.
    """
    from src.services.db import get_solar_forecast_config as _sc
    from src.services.solar import SolarForecastManager
    from datetime import timezone, timedelta
    import zoneinfo
    
    # Defaults
    metrics = {
        "today_kwh": 0.0,
        "remaining_kwh": 0.0,
        "tomorrow_kwh": 0.0,
    }
    
    try:
        cfg = await _sc()
        if not cfg or not cfg.get("enabled"):
            return metrics
            
        # Get forecast slots
        from src.main import get_app_state
        state = get_app_state()
        data_dir = state.get("data_dir")
        mgr = SolarForecastManager(config=cfg, data_dir=data_dir)
        slots = mgr.get_forecast()
        if not slots:
            return metrics
            
        # Resolve local timezone
        tz = timezone.utc
        if timezone_name:
            try:
                tz = zoneinfo.ZoneInfo(timezone_name)
            except Exception:
                pass
                
        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")
        tomorrow_str = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
        
        today_total = 0.0
        remaining_total = 0.0
        tomorrow_total = 0.0
        
        for slot in slots:
            ts_str = slot.get("timestamp")
            if not ts_str:
                continue
            try:
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                slot_dt = datetime.fromisoformat(ts_str)
                slot_local = slot_dt.astimezone(tz)
            except Exception:
                continue
                
            pv_kw = float(slot.get("pv_kw", 0.0) or 0.0)
            period_mins = int(slot.get("period_mins", 30))
            pv_kwh = pv_kw * (period_mins / 60.0)
            
            slot_date_str = slot_local.strftime("%Y-%m-%d")
            if slot_date_str == today_str:
                today_total += pv_kwh
                if slot_local > now_local:
                    remaining_total += pv_kwh
            elif slot_date_str == tomorrow_str:
                tomorrow_total += pv_kwh
                
        metrics["today_kwh"] = round(today_total, 2)
        metrics["remaining_kwh"] = round(remaining_total, 2)
        metrics["tomorrow_kwh"] = round(tomorrow_total, 2)
        
    except Exception as e:
        logger.warning(f"Failed to calculate solar forecast metrics for {gateway_short_id}: {e}")
        
    return metrics

class AutomationEngine:
    """
    Central rule engine that evaluates active user automations and manages APScheduler lifecycles.
    """
    def __init__(self, db_path: str):
        # SQLAlchemyJobStore manages the 'apscheduler_jobs' table automatically
        # sqlite:/// is the synchronous connection string format for SQLAlchemy.
        # Attach a `PRAGMA busy_timeout=5000` listener so SQLite waits up to
        # 5 s for a competing writer to release the lock instead of raising
        # "database is locked" immediately. Root fix for the 2026-08-06
        # 02:01:32 crash where the AsyncIOScheduler.wakeup callback died on
        # a transient contention with our own SD writes and stalled every
        # scheduled job silently for 5 h. Belt-and-braces with the liveness
        # monitor at src/services/scheduler_liveness.py.
        from sqlalchemy import create_engine, event
        engine = create_engine(f'sqlite:///{db_path}')
        @event.listens_for(engine, "connect")
        def _sqlite_busy_timeout(dbapi_conn, _):
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
            except Exception:
                pass
        jobstores = {
            'default': SQLAlchemyJobStore(engine=engine)
        }
        # The scheduler gets the SITE's timezone; the process keeps UTC.
        # TOU blocks, demand windows and export windows are local wall-clock, so
        # the jobs must fire on local time — but stored timestamps must not move
        # underneath the database to achieve it. APScheduler takes its own
        # timezone precisely so these can differ.
        scheduler_kwargs = {"jobstores": jobstores}
        try:
            from zoneinfo import ZoneInfo
            from src.services.supervisor_settings import site_timezone

            zone = site_timezone()
            if zone:
                scheduler_kwargs["timezone"] = ZoneInfo(zone)
                logger.info(f"Scheduler timezone: {zone} (site-local); storage remains UTC")
        except Exception as exc:
            logger.warning(f"Scheduler timezone falling back to process default: {exc!r}")

        self.scheduler = AsyncIOScheduler(**scheduler_kwargs)
        self.registry = None  # Injected via main.py to allow gateway object routing
        self._modbus_lock = asyncio.Lock()  # Serializes parallel smart circuit & dispatch requests
        
    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info(f"APScheduler Automation Engine started (Bound to SQLAlchemy backend)")
        
    async def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("APScheduler Automation Engine cleanly shut down")

    async def stop_graceful(self):
        """Shut down, letting in-flight jobs finish first (GH #36).

        shutdown(wait=True) blocks, so it runs in a worker thread to keep the
        event loop free. Callers should bound it with asyncio.wait_for and fall
        back to stop() — a genuinely wedged scheduler may never drain, which is
        the failure mode scheduler_liveness exists to recover from.
        """
        if not self.scheduler.running:
            return
        await asyncio.to_thread(self.scheduler.shutdown, True)
        logger.info("APScheduler Automation Engine drained and shut down")

    def register_sd_jobs(self) -> None:
        """Register SmartDispatch temporal-loop jobs on the scheduler.

        Phase 2 of the SD revamp introduces the Macro/Meso/Micro loops as
        first-class APScheduler jobs so their cadence is decoupled from
        `PricingService.tick()`. Called from `main.py` after `.start()` if
        setup is complete.

        Idempotent — uses `replace_existing=True` so re-invocation on a
        warm scheduler (e.g. after live-edit or setup-wizard completion)
        just refreshes the trigger. Job IDs are namespaced under `sd:*`.
        """
        from apscheduler.triggers.cron import CronTrigger

        # Phase 2.A (2026-08-05) — Macro Discovery daily @03:00 site-local.
        # Site-local timezone is deferred until we handle multi-gateway
        # sites in different TZs; for a single-site deployment the machine
        # timezone already matches. When multi-TZ support lands, this will
        # need to switch to per-gateway jobs.
        from src.services.smart_dispatch.macro import MacroDiscovery
        self.scheduler.add_job(
            MacroDiscovery.run_all,
            trigger=CronTrigger(hour=3, minute=0, timezone=_site_tz()),
            id="sd:macro:daily",
            name="SmartDispatch Macro Discovery (daily)",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        logger.info("SmartDispatch: registered scheduler job sd:macro:daily (@ 03:00 site-local)")

        # Phase 2.C (2026-08-05) — Meso planner cron @ 00:05 / 06:05 /
        # 12:05 / 18:05 site-local. Guarantees a fresh 24h dispatch plan
        # gets computed at each 6h boundary regardless of price refresh
        # cadence. `force=True` inside MesoScheduler bypasses the greedy
        # heuristic's own cadence controller so the scheduled fire always
        # re-optimizes. Written to `sd_forecast_history` by the greedy
        # body (existing behaviour).
        from src.services.smart_dispatch.meso import MesoScheduler
        self.scheduler.add_job(
            MesoScheduler.run_all,
            trigger=CronTrigger(hour="0,6,12,18", minute=5, timezone=_site_tz()),
            id="sd:meso:cron",
            name="SmartDispatch Meso Planner (6h cron)",
            replace_existing=True,
            misfire_grace_time=1800,
            coalesce=True,
            max_instances=1,
        )
        logger.info("SmartDispatch: registered scheduler job sd:meso:cron (@ 00:05/06:05/12:05/18:05 site-local)")

        # Phase 2.D (2026-08-05) — Micro Ticker @30s interval. Only acts
        # on gateways with `smart_dispatch_config.sd_use_micro_ticker=1`
        # (default 0). When enabled per gateway, `PricingService.tick`
        # skips SD for that gateway and MicroTicker drives it instead.
        # Instantly reversible via config; no restart required.
        from apscheduler.triggers.interval import IntervalTrigger
        from src.services.smart_dispatch.micro import MicroTicker
        self.scheduler.add_job(
            MicroTicker.run_all,
            trigger=IntervalTrigger(seconds=30, timezone=_site_tz()),
            id="sd:micro:tick",
            name="SmartDispatch Micro Ticker (30s)",
            replace_existing=True,
            misfire_grace_time=15,
            coalesce=True,
            max_instances=1,
        )
        logger.info("SmartDispatch: registered scheduler job sd:micro:tick (every 30s, opt-in per gateway)")

        # Persona (v0.6.0, GH #11) — weekly re-detection so gateway/tariff
        # changes get picked up without user intervention. Silent + non-
        # fatal; the initial detection happens at startup via the
        # persona_first_run task in main.py. Must reference a module-level
        # function (not a closure) so SQLAlchemyJobStore can serialise it —
        # see run_weekly_redetect docstring for why.
        from src.services.persona_detector import run_weekly_redetect
        self.scheduler.add_job(
            run_weekly_redetect,
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=30, timezone=_site_tz()),
            id="persona:weekly",
            name="Persona weekly re-detection",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Persona: registered scheduler job persona:weekly (Sun 03:30 site-local)")

        # ── TLS certificate expiry watch ────────────────────────────────────
        # Renewal happens on the host (LaunchAgent / cron), somewhere FHAI
        # cannot observe. Watching days_remaining catches a renewal that has
        # stopped whatever the cause, without needing to know the mechanism.
        # Module-level function, as the jobstore must serialise the reference.
        from src.services.tls_monitor import run_cert_expiry_check
        self.scheduler.add_job(
            run_cert_expiry_check,
            trigger=CronTrigger(hour=9, minute=7, timezone=_site_tz()),
            id="tls:cert:check",
            name="TLS certificate expiry check (daily)",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        logger.info("TLS: registered scheduler job tls:cert:check (daily @ 09:07 site-local)")

        # ── Local usage telemetry rollup ────────────────────────────────────
        # Collection only — materialises the day's counters into one outbox row
        # and transmits nothing (GH #40). Scheduled in UTC so the run hour does
        # not encode the site's timezone. No-ops unless the user opted in.
        from src.services.telemetry import run_daily_rollup
        self.scheduler.add_job(
            run_daily_rollup,
            trigger=CronTrigger(hour=1, minute=23, timezone="UTC"),
            id="telemetry:rollup",
            name="Local usage telemetry rollup (daily)",
            replace_existing=True,
            misfire_grace_time=7200,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Telemetry: registered scheduler job telemetry:rollup (daily @ 01:23 UTC, local only)")

    async def _evaluate_macro(self, macro_id: str, live_data: dict, extra_ctx: dict, evaluating_macros: set) -> any:
        if macro_id in evaluating_macros:
            logger.warning(f"Circular dependency detected in macro evaluation for rule {macro_id}. Returning False.")
            return False
            
        evaluating_macros.add(macro_id)
        try:
            if not self.scheduler:
                return False
                
            job = self.scheduler.get_job(macro_id)
            if not job:
                for j in self.scheduler.get_jobs():
                    if j.kwargs.get("rule_id") == macro_id:
                        job = j
                        break
                        
            if not job:
                return False
                
            kw = job.kwargs
            cond_logic = kw.get("condition_logic", "AND")
            conds = kw.get("conditions", [])
            actions = kw.get("actions", [])
            
            m_extra_ctx = extra_ctx.copy()
            m_extra_ctx["base"] = {}  # Empty base context to avoid circularity during evaluation
            
            matched, _, _ = evaluate_conditions(live_data, cond_logic, conds, extra_context=m_extra_ctx)
            
            payload = actions[0].get("payload", {}) if actions else {}
            ret_true = payload.get("return_value_true")
            ret_false = payload.get("return_value_false")
            
            if matched:
                val = ret_true if ret_true is not None and str(ret_true).strip() != "" else True
            else:
                val = ret_false if ret_false is not None and str(ret_false).strip() != "" else False
                
            if isinstance(val, str):
                lower_val = val.lower().strip()
                if lower_val == 'true':
                    val = True
                elif lower_val == 'false':
                    val = False
                else:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
            return val
        except Exception as e:
            logger.error(f"Failed to evaluate macro {macro_id}: {e}")
            return False
        finally:
            evaluating_macros.remove(macro_id)

    async def _execute_action(self, gateway, action: dict, action_context: dict = None, trace_id: str = None, hist_id: int = None) -> tuple[str, str]:
        """Perform the actual Modbus mutation inside the concurrency lock. Returns (status, detail)."""
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "base_condition":
            # Logical Base Condition Macro evaluations have zero physical side effects
            return "success", "Base Condition logical macro evaluated successfully"

        # ── SD mode aliases: normalise to set_mode before dispatch ──────────
        if action_type == "smart_dispatch.solar_mode":
            action_type = "set_mode"
            payload = {"mode": "solar"}
        elif action_type == "smart_dispatch.home_mode":
            action_type = "set_mode"
            payload = {"mode": "home"}
        
        async with self._modbus_lock:
            if action_type == "set_smart_circuit":
                cid = payload.get("id")
                state = payload.get("state")
                logger.info(f"Executing [set_smart_circuit]: {cid} -> {state}")
                await gateway.set_smart_circuit(cid, state)
                return "success", f"Triggered circuit {cid} to {state}"
            
            elif action_type == "set_mode":
                raw_mode = payload.get("mode")
                if raw_mode is None:
                    return "failed", "Missing 'mode' key in action payload"
                    
                mode_map = {
                    "time of use": 1, "time_of_use": 1, "1": 1, 1: 1,
                    "home": 1,          # alias: HOME mode = TOU (dispatchId 1)
                    "self consumption": 2, "self_consumption": 2, "2": 2, 2: 2,
                    "solar": 3,         # SOLAR mode = self-consumption+solar priority (dispatchId 3)
                    "backup priority": 4, "backup standby": 4, "4": 4, 4: 4
                }
                
                map_key = str(raw_mode).lower().strip()
                if map_key in mode_map:
                    resolved_mode = mode_map[map_key]
                else:
                    return "failed", f"Invalid execution mode specified: '{raw_mode}'"
                    
                logger.info(f"Executing [set_mode]: {resolved_mode} (from {raw_mode})")
                res = await gateway.set_operating_mode(
                    resolved_mode,
                    caller=action.get("_caller", "AutomationBuilder:set_mode")
                )
                if res.get("ok"):
                    return "success", f"Triggered operational mode {resolved_mode}"
                else:
                    return "failed", f"Failed to set mode {resolved_mode}: {res.get('error')}"
                
            elif action_type in ["force_charge", "force_discharge", "force_standby"] or action_type.startswith("smart_dispatch."):
                base_action = action_type.replace("smart_dispatch.", "")
                
                if base_action == "solar_mode":
                    logger.info("Executing [smart_dispatch.solar_mode] -> Set mode to 2 (Self-Consumption)")
                    res = await gateway.set_operating_mode(2, caller="AutomationBuilder:solar_mode")
                    return ("success" if res.get("ok") else "failed"), f"Set SOLAR Mode: {res.get('error', 'OK')}"
                    
                elif base_action == "home_mode":
                    logger.info("Executing [smart_dispatch.home_mode] -> Set mode to 1 (TOU)")
                    res = await gateway.set_operating_mode(1, caller="AutomationBuilder:home_mode")
                    return ("success" if res.get("ok") else "failed"), f"Set HOME Mode: {res.get('error', 'OK')}"

                elif base_action == "curtail_solar":
                    logger.info("Executing [smart_dispatch.curtail_solar] -> Set Off-Grid mode to True")
                    res = await gateway.set_offgrid(True)
                    return ("success" if res.get("ok") else "failed"), f"Curtail Solar (Off-Grid Islanding): {res.get('error', 'OK')}"

                elif base_action in ["force_charge", "force_discharge", "force_standby"]:
                    action_type = base_action
                    logger.info(f"Executing [{action_type}] via TOU Custom Sandbox")
                    dispatch_map = {
                        "force_charge": 8,     # GRID_CHARGE
                        "force_discharge": 7,  # GRID_SELL
                        "force_standby": 6     # SELF_CONSUMPTION (0 threshold)
                    }
                # ── Resolve Lookup references in payload ──────────────────────
                # If the UI sent a Lookup key (target_soc_ref / power_limit_ref)
                # resolve it against the SD config now, at execution time.
                resolved_target_soc = payload.get("target_soc")
                resolved_power_limit = payload.get("power_limit_kw")

                target_soc_ref = payload.get("target_soc_ref", "")
                power_limit_ref = payload.get("power_limit_ref", "")

                if target_soc_ref or power_limit_ref:
                    try:
                        from src.services.db import get_smart_dispatch_config as _get_sd_cfg
                        _sd_cfg = await _get_sd_cfg(gateway.short_id)
                        _dispatch_ctx = {
                            "min_soc_limit":       float(_sd_cfg.get("min_soc", 20)),
                            "max_soc_limit":       float(_sd_cfg.get("max_soc", 90)),
                            "min_peak_window_soc": float(_sd_cfg.get("min_peak_window_soc", 60)),
                            "max_peak_window_soc": float(_sd_cfg.get("max_peak_window_soc", 90)),
                        }
                        if action_context:
                            _dispatch_ctx.update(action_context)
                            
                        if target_soc_ref and target_soc_ref in _dispatch_ctx:
                            resolved_target_soc = _dispatch_ctx[target_soc_ref]
                            logger.info(f"[{action_type}] target_soc resolved via Lookup '{target_soc_ref}' = {resolved_target_soc}")
                        if power_limit_ref and power_limit_ref in _dispatch_ctx:
                            resolved_power_limit = _dispatch_ctx[power_limit_ref]
                            logger.info(f"[{action_type}] power_limit_kw resolved via Lookup '{power_limit_ref}' = {resolved_power_limit}")
                    except Exception as _ref_ex:
                        logger.warning(f"[{action_type}] Lookup ref resolution failed: {_ref_ex} — using literal values")

                # We enforce this forced state continuously utilizing a 24h block until disabled or replaced.
                # Field names MUST match the franklinwh_cloud tou_json_schema:
                #   required: name, startHourTime, endHourTime, waveType, dispatchId
                
                # Resolve duration
                duration = payload.get("duration_mins") or payload.get("duration")
                if not duration:
                    try:
                        from src.services import db as _db
                        sd_sig_payload = await _db.get_sd_signal_payload(gateway.short_id)
                        duration = sd_sig_payload.get("duration_mins")
                    except Exception:
                        pass
                if not duration:
                    duration = 120 # Default to 2 hours
                
                import datetime as _dt
                now_local = _dt.datetime.now()
                start_mins = now_local.hour * 60 + now_local.minute
                end_mins = start_mins + duration
                if end_mins > 1440:
                    end_mins = 1440
                
                start_time_str = f"{now_local.hour:02d}:{now_local.minute:02d}"
                end_time_str = f"{end_mins // 60:02d}:{end_mins % 60:02d}"

                from src.services import db as _db
                sd_cfg = await _db.get_smart_dispatch_config(gateway.short_id)
                default_pref = sd_cfg.get("default_operating_mode", "gateway_default")
                
                padding_dispatch_id = 6 # Default to Self-Consumption (6)
                if default_pref in ("Standby", "Backup", "Emergency Backup"):
                    padding_dispatch_id = 2

                # Pre-override baseline snapshot saving
                existing_snapshot = sd_cfg.get("baseline_tou_snapshot")
                if not existing_snapshot:
                    logger.info(f"[{gateway.short_id}] Pre-override snapshot: capturing current TOU schedule from gateway")
                    tou_res = await gateway.get_tou_schedule()
                    if tou_res.get("ok"):
                        detail_obj = tou_res.get("detail") or {}
                        res_data = detail_obj.get("result") or {}
                        strategy_list = res_data.get("strategyList") or []
                        if strategy_list:
                            import json as _json
                            snapshot_json = _json.dumps(strategy_list)
                            import uuid
                            override_uuid = str(uuid.uuid4())
                            
                            now_utc = _dt.datetime.now(_dt.timezone.utc)
                            expires_at_iso = (now_utc + _dt.timedelta(minutes=duration)).isoformat()
                            
                            await _db.upsert_smart_dispatch_config(
                                gateway.short_id,
                                baseline_tou_snapshot=snapshot_json,
                                active_override_uuid=override_uuid,
                                active_override_expires_at=expires_at_iso
                            )
                            logger.info(f"[{gateway.short_id}] Pre-override snapshot successfully saved to DB. Expires at {expires_at_iso}.")
                        else:
                            logger.warning(f"[{gateway.short_id}] Pre-override snapshot: gateway returned empty strategyList — snapshot skipped.")
                    else:
                        logger.error(f"[{gateway.short_id}] Pre-override snapshot failed: {tou_res.get('error')}")

                _dispatch_labels = {
                    "force_charge":    "Grid Charge",
                    "force_discharge": "Grid Discharge",
                    "force_standby":   "Standby",
                }

                schedule_block = {
                    "name":          _dispatch_labels.get(action_type, "SD Dispatch"),
                    "startHourTime": start_time_str,
                    "endHourTime":   end_time_str,
                    "dispatchId":    dispatch_map[action_type],
                    "waveType":      0,  # Off-peak classification
                    "briefDescribe": "HEMS_OVERRIDE_ACTIVE",
                }

                # Support advanced properties & manual limits
                schedule_block["rampTime"] = int(sd_cfg.get("rampTime") or sd_cfg.get("ramp_time") or 99)

                if sd_cfg.get("maxChargeSoc") is not None:
                    schedule_block["maxChargeSoc"] = int(sd_cfg["maxChargeSoc"])
                elif resolved_target_soc is not None and action_type == "force_charge":
                    schedule_block["maxChargeSoc"] = int(resolved_target_soc)

                if sd_cfg.get("minDischargeSoc") is not None:
                    schedule_block["minDischargeSoc"] = int(sd_cfg["minDischargeSoc"])
                elif resolved_target_soc is not None and action_type == "force_discharge":
                    schedule_block["minDischargeSoc"] = int(resolved_target_soc)

                if sd_cfg.get("chargePower") is not None:
                    schedule_block["chargePower"] = int(sd_cfg["chargePower"])
                    schedule_block["gridChargeMax"] = int(sd_cfg["chargePower"])
                elif resolved_power_limit is not None and action_type == "force_charge":
                    schedule_block["chargePower"] = int(resolved_power_limit * 1000)
                    schedule_block["gridChargeMax"] = int(resolved_power_limit * 1000)

                if sd_cfg.get("dischargePower") is not None:
                    schedule_block["dischargePower"] = int(sd_cfg["dischargePower"])
                    schedule_block["gridDischargeMax"] = int(sd_cfg["dischargePower"])
                elif resolved_power_limit is not None and action_type == "force_discharge":
                    schedule_block["dischargePower"] = int(resolved_power_limit * 1000)
                    schedule_block["gridDischargeMax"] = int(resolved_power_limit * 1000)

                schedule_list = []
                if start_time_str != "00:00":
                    schedule_list.append({
                        "name":          "HEMS Default Padding",
                        "startHourTime": "00:00",
                        "endHourTime":   start_time_str,
                        "dispatchId":    padding_dispatch_id,
                        "waveType":      0,
                    })

                schedule_list.append(schedule_block)

                if end_time_str != "24:00":
                    schedule_list.append({
                        "name":          "HEMS Default Padding",
                        "startHourTime": end_time_str,
                        "endHourTime":   "24:00",
                        "dispatchId":    padding_dispatch_id,
                        "waveType":      0,
                    })

                payload_tou = {
                    "schedule":       schedule_list,
                    "operation":      0,
                    "default_mode":   "SELF",
                    "default_tariff": "OFF_PEAK"
                }

                # Step 1: Ensure system is inside TOU Mode to evaluate our dispatch blocks
                await gateway.set_operating_mode(
                    1,  # 1 = Time-of-Use
                    caller=f"AutomationBuilder:{action_type}"
                )

                # Step 2: Inject the custom dispatch block
                res = await gateway.set_tou_schedule(payload_tou)

                if res.get("ok"):
                    soc_info = f" → target SOC {resolved_target_soc}%" if resolved_target_soc is not None else ""
                    pw_info  = f", power limit {resolved_power_limit}kW" if resolved_power_limit is not None else ""
                    return "success", f"Force-applied {action_type} (dispatchType {dispatch_map[action_type]}) via TOU block{soc_info}{pw_info}"
                else:
                    return "failed", f"Failed to inject TOU Cloud payload: {res.get('error')}"

            elif action_type in ("stop_dispatch", "stop"):
                logger.info(f"[{gateway.short_id}] Executing [{action_type}] — clearing cloud TOU overrides and checking baseline snapshot")
                try:
                    from src.services import db as _db
                    sd_cfg = await _db.get_smart_dispatch_config(gateway.short_id)
                    snapshot_json = sd_cfg.get("baseline_tou_snapshot")
                    
                    res = {"ok": True, "success": True}
                    if snapshot_json:
                        logger.info(f"[{gateway.short_id}] Found cached baseline snapshot. Restoring baseline schedule...")
                        import json as _json
                        baseline_list = _json.loads(snapshot_json)
                        res = await gateway.set_tou_schedule_multi(baseline_list)
                        if res.get("ok"):
                            logger.info(f"[{gateway.short_id}] Baseline schedule successfully restored on stop command.")
                            await _db.upsert_smart_dispatch_config(
                                gateway.short_id,
                                baseline_tou_snapshot=None,
                                active_override_uuid=None,
                                active_override_expires_at=None
                            )
                            # ── Revert to user preferred operating mode if configured ──
                            default_mode = sd_cfg.get("default_operating_mode", "gateway_default")
                            if default_mode != "gateway_default":
                                mode_map = {
                                    "Time-of-Use": 1,
                                    "Self-Consumption": 2,
                                    "Backup": 3,
                                    "Standby": 3,
                                    "Emergency Backup": 3
                                }
                                target_mode = mode_map.get(default_mode)
                                if target_mode is not None:
                                    await gateway.set_operating_mode(target_mode, caller="SmartDispatch:RevertToDefault")
                        else:
                            logger.error(f"[{gateway.short_id}] Failed to restore baseline schedule on stop: {res.get('error')}")
                            res = {"ok": False, "success": False, "reason": res.get("error")}
                    else:
                        # Fallback: stop via cloud dispatch manager
                        res = await gateway.cloud_dispatch.stop()
                        
                        # ── Revert to user preferred operating mode if configured ──
                        default_mode = sd_cfg.get("default_operating_mode", "gateway_default")
                        if default_mode != "gateway_default":
                            mode_map = {
                                "Time-of-Use": 1,
                                "Self-Consumption": 2,
                                "Backup": 3,
                                "Standby": 3,
                                "Emergency Backup": 3
                            }
                            target_mode = mode_map.get(default_mode)
                            if target_mode is not None:
                                await gateway.set_operating_mode(target_mode, caller="SmartDispatch:RevertToDefault")
                                
                    return ("success" if res.get("success") or res.get("ok") else "failed"), f"Cleared overrides"
                except Exception as exc:
                    return "error", f"Stop dispatch failed: {exc}"

            elif action_type == "set_offgrid":
                enabled = str(payload.get("enabled", False)).lower() in ("true", "1", "on", "yes")
                logger.info(f"Executing [set_offgrid]: {enabled}")
                res = await gateway.set_offgrid(enabled)
                if res.get("ok"):
                    return "success", f"Triggered Off-Grid: {enabled}"
                else:
                    return "failed", f"Failed to set Off-Grid {enabled}: {res.get('error')}"

            elif action_type == "set_storm_edge":
                enabled = str(payload.get("enabled", False)).lower() in ("true", "1", "on", "yes")
                logger.info(f"Executing [set_storm_edge]: {enabled}")
                res = await gateway.set_storm_edge(enabled)
                if res.get("ok"):
                    return "success", f"Triggered Storm Edge: {enabled}"
                else:
                    return "failed", f"Failed to set Storm Edge {enabled}: {res.get('error')}"

            elif action_type == "set_apower_led":
                enabled = str(payload.get("enabled", False)).lower() in ("true", "1", "on", "yes")
                logger.info(f"Executing [set_apower_led]: {enabled}")
                res = await gateway.set_apower_led(enabled)
                if res.get("ok"):
                    return "success", f"Set aPower LED: {enabled} across {res.get('success_count')} batteries"
                else:
                    return "failed", f"Failed to set aPower LED {enabled}: {res.get('error')}"

            elif action_type == "record_bms":
                samples = int(payload.get("samples") or 5)
                interval_secs = int(payload.get("interval_secs") or 30)
                session_name = (payload.get("session_name") or "").strip()

                # Resolve battery SN: use payload if provided, else fall back to DB batteries table.
                # NOTE: bms_units in last_data is only populated after an explicit BMS trigger
                # (UI refresh or previous automation). Using DB as the authoritative source
                # so this action works reliably from first boot without a prior BMS poll.
                battery_sn = (payload.get("battery_sn") or "").strip()
                if not battery_sn:
                    try:
                        from src.services.db import get_batteries_for_gateway
                        db_batteries = await get_batteries_for_gateway(gateway.short_id)
                        if db_batteries:
                            battery_sn = db_batteries[0].get("full_serial") or db_batteries[0].get("short_id", "")
                            logger.info(f"[record_bms] Resolved battery_sn from DB: {battery_sn[-6:] if battery_sn else 'N/A'}")
                    except Exception as _db_ex:
                        logger.warning(f"[record_bms] DB battery lookup failed: {_db_ex}")

                if not battery_sn:
                    logger.warning(f"[record_bms] No battery SN resolvable for {gateway.short_id} — action skipped")
                    return "failed", "Could not resolve a battery serial number. Set battery_sn in the action payload or register a battery in the Gateways tab first."

                if not session_name:
                    from datetime import datetime
                    session_name = f"Automation: {gateway.short_id} @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"

                logger.info(f"[record_bms] Dispatching background BMS recording: {battery_sn} × {samples} @ {interval_secs}s → '{session_name}'")
                asyncio.create_task(gateway.schedule_bms_recording(battery_sn, samples, interval_secs))
                return "dispatched", f"BMS background recording started: {battery_sn} × {samples} samples @ {interval_secs}s — '{session_name}'"


            elif action_type == "start_generator":
                # "1=auto-schedule, 2=manual" came from the library's old
                # docstring and is now disproven: set_generator_mode() posts
                # `manuSw`, a manual start/stop acting on generator STATE, not
                # the mode (DEF-GEN-MODE-WRITES-MANUSW). The value→effect
                # mapping is explicitly ASSUMED upstream from one sample, in
                # which `manuSw: 2` preceded Running -> Cooldown.
                #
                # Left working rather than refused: this fires only from a rule
                # somebody wrote deliberately, and silently breaking their
                # automation would be worse than running it. But the log must
                # not keep repeating a claim known to be false.
                mode = int(payload.get("mode", 1))
                logger.warning(
                    f"Executing [start_generator]: manuSw={mode}. This writes the "
                    "manual start/stop field, not the operating mode, and the "
                    "value mapping is unverified upstream."
                )
                try:
                    res = await gateway.client.set_generator_mode(mode)
                    return "success", (
                        f"Sent manuSw={mode} to the generator (manual start/stop; "
                        f"this is not the operating mode). API result: {res}"
                    )
                except AttributeError:
                    return "failed", "gateway.client.set_generator_mode() not available — ensure franklinwh-cloud library is updated."
                except Exception as exc:
                    return "failed", f"Generator mode set failed: {exc}"

            elif action_type == "start_v2l":
                v2l_state = str(payload.get("mode", "on")).lower()
                logger.warning(
                    f"[start_v2l] ⚠ Speculative API — V2L control endpoint is unverified. "
                    f"Attempting state={v2l_state} for gateway {gateway.short_id}. "
                    f"This may have no effect if the API endpoint differs."
                )
                try:
                    # Attempt via cloud client (endpoint TBD — update docs/GENERATOR_V2L_API.md when confirmed)
                    res = await gateway.client.set_v2l_mode(v2l_state)
                    return "success", f"V2L output set to {v2l_state} (speculative API). Result: {res}"
                except AttributeError:
                    return "skipped", (
                        "gateway.client.set_v2l_mode() is not yet implemented in franklinwh-cloud. "
                        "V2L control API endpoint requires verification with physical hardware. "
                        "See docs/GENERATOR_V2L_API.md for research status."
                    )
                except Exception as exc:
                    return "failed", f"V2L mode set failed (speculative API): {exc}"

            elif action_type == "stop_all_smart_circuits":
                logger.info(f"Executing [stop_all_smart_circuits]: turning OFF circuits 1, 2, 3")
                results = []
                for cid in (1, 2, 3):
                    try:
                        await gateway.set_smart_circuit(cid, "off")
                        results.append(f"SC{cid}:OK")
                    except Exception as exc:
                        results.append(f"SC{cid}:FAIL({exc})")
                summary = " | ".join(results)
                all_ok = all("OK" in r for r in results)
                return ("success" if all_ok else "partial"), f"Stop all smart circuits: {summary}"
                
            elif action_type == "send_notification":
                from src.services.notification_sender import send_ha_notification
                title = payload.get("title", "Automation Builder")
                message = payload.get("message", "A rule was triggered.")
                
                # Expand extra variables from action_context
                fmt_ctx = {}
                if action_context:
                    fmt_ctx.update(action_context)
                    
                ctx = {"title": title, "message": message, "action": action.get("_caller", "rule")}
                ctx.update(fmt_ctx)
                
                res = await send_ha_notification("rule_triggered", ctx)
                if res.get("sent"):
                    return "success", f"Notification sent: {title}"
                else:
                    return "failed", f"Failed to send notification: {res.get('error')}"
                    
            elif action_type == "send_actionable_notification":
                from src.services.notification_sender import send_ha_notification
                from src.services.db import set_pending_approval
                import uuid
                import time
                
                title = payload.get("title", "Action Required")
                message = payload.get("message", "Please respond to this notification.")
                timeout_mins = int(payload.get("timeout_mins", 1))
                no_reply_action = payload.get("no_reply_action", "skip")
                response_type = payload.get("response_type", "yes_no")
                
                fmt_ctx = {}
                if action_context:
                    fmt_ctx.update(action_context)
                    
                # Reconciliation UUID — prefer the trace_id from the runner
                request_id = trace_id or str(uuid.uuid4())
                ctx = {
                    "title": title, 
                    "message": message, 
                    "action": f"FWH_ACTIONABLE_{request_id[:8]}",
                    "response_type": response_type
                }
                ctx.update(fmt_ctx)
                
                logger.info(f"[{gateway.short_id}] Sending actionable notification (timeout={timeout_mins}m, id={request_id}): {title}")
                
                # Store pending approval
                await set_pending_approval(
                    gateway.short_id,
                    request_id=request_id,
                    rule_id=action.get("id"),
                    rule_name=action.get("_caller", "Actionable Notification"),
                    action=ctx["action"],
                    dispatch_summary=message,
                    ttl_secs=timeout_mins * 60,
                    no_reply_action=no_reply_action,
                    action_context=action_context
                )
                
                res = await send_ha_notification("actionable_step", ctx, force_actionable=True, request_id=request_id)
                if not res.get("sent"):
                    return "failed", f"Failed to dispatch actionable notification: {res.get('error')}"
                    
                # Update status to 'waiting' for the trace modal
                if hist_id:
                    await update_automation_history(hist_id, "waiting", f"Waiting for user response (timeout {timeout_mins}m)...")

                # Wait for response (poll pending_approval every 5 seconds)
                logger.info(f"[{gateway.short_id}] Waiting up to {timeout_mins}m for HA webhook callback (request_id={request_id})...")
                start_time = time.time()
                from src.services.db import get_pending_approval
                
                while (time.time() - start_time) < (timeout_mins * 60):
                    pending = await get_pending_approval(request_id=request_id)
                    if pending and pending.get("response") is not None:
                        resp_val = pending.get("response")
                        logger.info(f"[{gateway.short_id}] Received actionable response for {request_id}: {resp_val}")
                        
                        # Set into context
                        if action_context is not None:
                            action_context["notification.response_value"] = resp_val
                            
                        return "success", f"Actionable response received: {resp_val}"
                    
                    # Periodically refresh the 'waiting' status to show the engine is alive
                    if hist_id and int(time.time() - start_time) % 30 < 5:
                        elapsed = int(time.time() - start_time)
                        rem = max(0, (timeout_mins * 60) - elapsed)
                        await update_automation_history(hist_id, "waiting", f"Waiting for response... {rem}s remaining")

                    await asyncio.sleep(5)
                
                # Timeout
                if no_reply_action == "skip":
                    return "skipped", "No response received from user (timed out). Action skipped."
                else:
                    # execute anyway
                    if payload.get("default_reply_value"):
                        action_context["notification.response_value"] = payload["default_reply_value"]
                    return "success", "No response received (timed out), falling back to execute."

            else:
                logger.warning(f"Unknown APScheduler action: {action_type}")
                return "failed", f"Unknown action type: {action_type}"

    # ── Smart Dispatch signal → action_type mapping ───────────────────────────
    # Maps SD signal names to the scheduler action dicts accepted by _execute_action().
    # CURTAIL_SOLAR / TOPUP_NEEDED are advisory signals — native AB rules execute them;
    # the scheduler skips them (returns "skipped") so they don't abort the chain.
    _SD_SIGNAL_ACTION_MAP: dict = {
        "GRID_CHARGE":    {"type": "force_charge",   "payload": {}},
        "GRID_EXPORT":    {"type": "force_discharge", "payload": {}},
        # STANDBY is the canonical no-discharge signal.
        "STANDBY":        {"type": "force_standby",   "payload": {}},
        # HOLD is a deprecated alias for STANDBY — kept as no-op advisory until next release
        "HOLD":           None,  # advisory no-op — native mode continues, no hardware command
        "RESUME_NATIVE":  {"type": "stop_dispatch",   "payload": {}},
        # New executable signals: set gateway operating mode
        "SOLAR":          {"type": "set_mode",        "payload": {"mode": "solar"}},
        "HOME":           {"type": "set_mode",        "payload": {"mode": "home"}},
        # Advisory-only signals — resolved by native AB rules, not by direct dispatch
        "CURTAIL_SOLAR":  None,
        "TOPUP_NEEDED":   None,
    }

    async def execute_sd_signal_list(
        self,
        gateway_serial: str,
        signals: list,
        dispatch_guid: str,
    ) -> dict:
        """
        Execute an ordered list of Smart Dispatch signals against the specified gateway.

        Execution is sequential (signals sorted by 'order' field). On first failure
        the remaining signals are aborted. Every step — success, skip, failure, and
        the abort event itself — is logged to automation_history so the Automations >
        History panel distinguishes SD-originated actions from native user rules.

        Tags in action_payload: ["Smart Dispatch", "<GUID>", "<SIGNAL>", "<action_type>"]

        Returns: {ok, executed_count, abort_signal, abort_reason, gateway_serial}
        """
        from src.services.db import insert_automation_history

        if not self.registry:
            logger.error(
                f"[SD:{dispatch_guid}] AutomationEngine has no registry — cannot execute signals"
            )
            return {
                "ok": False, "executed_count": 0,
                "abort_signal": "INIT", "abort_reason": "No gateway registry",
                "gateway_serial": gateway_serial,
            }

        # ── Resolve gateway object (Phase E: target-specific, not ALL) ─────────
        gw = None
        if gateway_serial and gateway_serial != "ALL":
            gw = self.registry.get_gateway(gateway_serial)
            if not gw:
                for g in self.registry._services.values():
                    if (getattr(g, "full_serial", None) == gateway_serial
                            or getattr(g, "short_id", None) == gateway_serial):
                        gw = g
                        break
        if not gw:
            msg = f"Gateway '{gateway_serial}' not found in registry — all signals aborted"
            logger.error(f"[SD:{dispatch_guid}] {msg}")
            await insert_automation_history(
                f"SD:{dispatch_guid}:INIT",
                f"Smart Dispatch ABORT [{dispatch_guid}]",
                gateway_serial,
                "SD_ABORT",
                {"dispatch_guid": dispatch_guid, "tags": ["Smart Dispatch", dispatch_guid, "INIT"]},
                "failed",
                msg,
            )
            return {
                "ok": False, "executed_count": 0,
                "abort_signal": "INIT", "abort_reason": msg,
                "gateway_serial": gateway_serial,
            }

        # ── Execute signals in order ──────────────────────────────────────────
        ordered = sorted(signals, key=lambda s: s.get("order", 99))
        executed_count = 0

        for sig in ordered:
            signal_action = sig.get("action", "")
            signal_order  = sig.get("order", "?")
            rule_name = f"Smart Dispatch: {signal_action} [{dispatch_guid}]"
            tags = ["Smart Dispatch", dispatch_guid, signal_action]

            # 1. Try dynamic Actuator Map (Phase 3)
            from src.services.db import get_sd_actuators
            actuators = await get_sd_actuators()
            dynamic_map = next((a for a in actuators if a["signal_key"] == signal_action), None)
            
            action_entry = None
            if dynamic_map:
                action_entry = {
                    "type": dynamic_map["actuator_type"],
                    "target": dynamic_map["target"],
                    "payload": {} # TODO: resolve from row metadata or result payload
                }
                # Adapt naming for _execute_action compatibility
                if action_entry["type"] == "fwh_cloud":
                    # Map 'grid_charge' target to legacy 'force_charge' type, etc.
                    # This bridges Phase 3 mappings to existing _execute_action logic.
                    target = action_entry["target"]
                    if target == "grid_charge": action_entry["type"] = "force_charge"
                    elif target == "grid_export": action_entry["type"] = "force_discharge"
                    elif target == "solar_relay": action_entry["type"] = "curtail_solar"
                    elif target == "standby": action_entry["type"] = "force_standby"
                    elif target == "tou": action_entry["type"] = "set_mode"; action_entry["payload"] = {"mode": "tou"}
                    elif target == "self_consumption": action_entry["type"] = "set_mode"; action_entry["payload"] = {"mode": "self_consumption"}

            # 2. Fallback to hardcoded legacy map
            if not action_entry:
                action_entry = self._SD_SIGNAL_ACTION_MAP.get(signal_action)

            # Advisory-only signal (None entry) — skip without aborting
            if signal_action in self._SD_SIGNAL_ACTION_MAP and action_entry is None:
                logger.info(
                    f"[SD:{dispatch_guid}] [{gateway_serial}] Signal {signal_order} "
                    f"({signal_action}): advisory-only — skipped (AB rules handle this)"
                )
                await insert_automation_history(
                    f"SD:{dispatch_guid}:{signal_order}",
                    rule_name,
                    gateway_serial,
                    signal_action,
                    {"dispatch_guid": dispatch_guid, "tags": tags, "sd_signal": signal_action},
                    "skipped",
                    f"Advisory-only SD signal '{signal_action}' — native AB rules are responsible for execution.",
                )
                executed_count += 1
                continue

            if action_entry is None:
                logger.warning(
                    f"[SD:{dispatch_guid}] Unknown SD signal '{signal_action}' at "
                    f"order={signal_order} — skipping (not an abort)"
                )
                await insert_automation_history(
                    f"SD:{dispatch_guid}:{signal_order}",
                    rule_name,
                    gateway_serial,
                    signal_action,
                    {"dispatch_guid": dispatch_guid, "tags": tags},
                    "skipped",
                    f"No action mapping for SD signal '{signal_action}'.",
                )
                continue

            logger.info(
                f"[SD:{dispatch_guid}] [{gateway_serial}] Executing signal {signal_order}: "
                f"{signal_action} → {action_entry['type']} "
                f"reason='{sig.get('reason', '')}'"
            )
            try:
                status, detail = await self._execute_action(gw, action_entry)
            except Exception as exc:
                status, detail = "error", str(exc)

            await insert_automation_history(
                f"SD:{dispatch_guid}:{signal_order}",
                rule_name,
                gateway_serial,
                action_entry["type"],
                {
                    **action_entry.get("payload", {}),
                    "dispatch_guid": dispatch_guid,
                    "sd_signal": signal_action,
                    "tags": tags,
                },
                status,
                detail,
            )

            if status not in ("success", "skipped"):
                abort_reason = (
                    f"Signal {signal_order} ({signal_action}) failed: {detail}"
                )
                remaining = len(ordered) - executed_count - 1
                logger.error(
                    f"[SD:{dispatch_guid}] [{gateway_serial}] ABORT — {abort_reason}. "
                    f"{remaining} remaining signal(s) cancelled."
                )
                await insert_automation_history(
                    f"SD:{dispatch_guid}:ABORT",
                    f"Smart Dispatch ABORT [{dispatch_guid}]",
                    gateway_serial,
                    "SD_ABORT",
                    {
                        "dispatch_guid": dispatch_guid,
                        "abort_at_order": signal_order,
                        "abort_signal": signal_action,
                        "tags": tags,
                    },
                    "aborted",
                    abort_reason,
                )
                return {
                    "ok": False,
                    "executed_count": executed_count,
                    "abort_signal": signal_action,
                    "abort_reason": abort_reason,
                    "gateway_serial": gateway_serial,
                }

            executed_count += 1
            logger.info(
                f"[SD:{dispatch_guid}] Signal {signal_order} ({signal_action}): "
                f"{status} — {detail}"
            )

        return {
            "ok": True,
            "executed_count": executed_count,
            "abort_signal": None,
            "abort_reason": None,
            "gateway_serial": gateway_serial,
        }


    async def run_rule(self, rule_id: str, rule_name: str, gateway_serial: str, condition_logic: str, conditions: list[dict], actions: list[dict], owner: str = "user", tags: list[str] = None, cron: str = None, run_at: str = None, created_at: str = None, updated_at: str = None, duration_secs: int = 0, **kwargs):

        if not self.registry:
            return

        from src.services.db import insert_automation_history, update_automation_history, log_admin_audit, get_automation_state, upsert_automation_state, clear_automation_state, get_pending_approval
        import asyncio
        import time
        
        # Phase 7: trace_id for real-time UI tracking (can be passed in from /execute)
        trace_id = kwargs.get("trace_id") or str(int(time.time() * 1000))
        
        target_gws = []
        if gateway_serial == "ALL":
            target_gws = list(self.registry._services.values())
        else:
            serials = [s.strip() for s in gateway_serial.split(",") if s.strip()]
            for s in serials:
                gw = self.registry.get_gateway(s)
                if not gw:
                    # Fallback to full_serial object indexing natively
                    for g in self.registry._services.values():
                        if getattr(g, "full_serial", None) == s or getattr(g, "short_id", None) == s:
                            gw = g
                            break
                if gw and gw not in target_gws:
                    target_gws.append(gw)

        if not target_gws:
            logger.warning(f"[Rule {rule_id}] Target gateways offline/unmatched.")
            await insert_automation_history(
                rule_id, rule_name, gateway_serial, "target_discovery", {}, "skipped", f"Target gateways ('{gateway_serial}') offline or unmatched in local registry."
            )
            return

        # Enforce single-gateway execution limit on BMS recording actions
        has_bms_action = any(
            act.get("type") == "record_bms" and act.get("enabled", True) for act in actions
        )
        if has_bms_action and len(target_gws) > 1:
            logger.info(f"[Rule {rule_id}] Truncating target gateways from {len(target_gws)} to 1 for single-gateway BMS recording limit.")
            target_gws = [target_gws[0]]

        if gateway_serial == "ALL":
            asyncio.create_task(log_admin_audit("Automation Trigger", "Scheduler", owner, f"Schedule triggered rule '{rule_name}' against ALL aGate systems ({len(target_gws)} targets)."))
        else:
            asyncio.create_task(log_admin_audit("Automation Trigger", "Scheduler", owner, f"Schedule triggered rule '{rule_name}' against aGate {gateway_serial}."))

        for gateway in target_gws:
            live_data = gateway.status.last_data
            
            # Fetch pricing context for 'pricing.*' metrics
            extra_ctx = {}
            from src.main import get_app_state
            pricing_reg = get_app_state().get("pricing_registry")
            if pricing_reg:
                pricing_svc = pricing_reg.get_primary_service()
                if pricing_svc:
                    snap = pricing_svc.get_snapshot()
                    if snap:
                        # Fetch earnings tracking
                        from src.services.earnings_tracker import get_daily_earnings, get_monthly_earnings
                        try:
                            daily_earnings = await get_daily_earnings(gateway.short_id)
                            monthly_earnings, _ = await get_monthly_earnings(gateway.short_id)
                        except Exception:
                            daily_earnings = 0.0
                            monthly_earnings = 0.0
                            
                        # Phase 0+1: import DB helpers and fetch SD config (used by dispatch.* ctx + sd_signal)
                        from src.services import db as _db
                        try:
                            sd_cfg = await _db.get_smart_dispatch_config(gateway.short_id)
                        except Exception:
                            sd_cfg = {}
                            
                        # ── Site Season & Forecast Loads ──
                        # Defaults so the home_loads namespace is always present in
                        # extra_ctx even when the lookups below fail.
                        home_loads_ctx = {"home_load": {}, "home_loads": {}}
                        try:
                            lat_val = await _db.get_config_value(f"lat_{gateway.short_id}")
                            from datetime import datetime
                            from src.services.smart_dispatch import (
                                get_site_season,
                                get_active_forecast_load_kw,
                                build_home_loads_context,
                            )
                            site_season = ""
                            if lat_val:
                                site_season = get_site_season(float(lat_val), datetime.now().month)

                            fl_loads = await _db.get_all_forecast_loads()

                            ha_states = {}
                            from src.routes.api_ha import get_ha_state
                            for fl in fl_loads:
                                if fl.get("enabled", 1) and fl.get("measurement_type") == "now":
                                    for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                                        ent_id = fl.get(key)
                                        if ent_id:
                                            st = await get_ha_state(ent_id)
                                            if st and "state" in st:
                                                ha_states[ent_id] = st["state"]

                            fl_kw = get_active_forecast_load_kw(
                                fl_loads,
                                datetime.now(),
                                site_season,
                                is_current_slot=True,
                                ha_states=ha_states
                            )

                            # Home Loads FHAI-native context for AB.
                            # See docs/automation_builder_home_loads_exposure_plan.md.
                            # Rule eval is per-gateway here (the outer loop iterates
                            # target_gws even for ALL-scoped rules), so the scope is
                            # always this gateway plus global-scoped loads.
                            home_loads_ctx = build_home_loads_context(
                                loads=fl_loads,
                                target_time=datetime.now(),
                                seasons_by_gateway={
                                    gateway.short_id: site_season,
                                    "global":         site_season,
                                },
                                rule_gateway_scope=gateway.short_id,
                            )
                        except Exception:
                            site_season = ""
                            fl_kw = 0.0

                        extra_ctx["pricing"] = {
                            "import_price_c_kwh": snap.import_c_kwh,
                            "export_price_c_kwh": snap.export_c_kwh,
                            "demand_active": 1 if snap.demand_window else 0,
                            # Phase 0: fix key name — UI condition uses demand_window_active
                            "demand_window_active": snap.demand_window,
                            "spike_status": snap.spike_status,
                            "tariff_type": snap.tariff_type,
                            "renewables_pct": snap.renewables_pct,
                            "daily_earnings": daily_earnings,
                            "monthly_earnings": monthly_earnings,
                            # Phase 0: price descriptor (extremelyLow/negative/low/neutral/high/spike)
                            "descriptor": getattr(snap, "descriptor", "") or "",
                            # Phase 0: tariff period (solarSponge/offPeak/shoulder/peak)
                            "tariff_period": getattr(snap, "tariff_period", "") or "",
                            # Phase 0: solar sponge boolean alias
                            "solar_sponge": (getattr(snap, "tariff_period", "") or "").lower() == "solarsponge",
                            # Phase 1+2: SD engine decision signal + enriched payload
                            **{"sd_signal": "", "sd_signal_power_kw": None, "sd_signal_duration_mins": None, "sd_signal_target_soc": None},
                            **({"sd_signal": _sd_sig_payload.get("signal", ""),
                                "sd_signal_power_kw": _sd_sig_payload.get("power_kw"),
                                "sd_signal_duration_mins": _sd_sig_payload.get("duration_mins"),
                                "sd_signal_target_soc": _sd_sig_payload.get("target_soc"),
                                "sd_signal_calc_basis": _sd_sig_payload.get("calc_basis", ""),
                               }
                               if (_sd_sig_payload := await _db.get_sd_signal_payload(gateway.short_id))
                               else {}),
                        }
                        # Phase 0+1: SD Engine Parameters + live dispatch context for dispatch.* conditions
                        extra_ctx["dispatch"] = {
                            "min_soc_limit": float(sd_cfg.get("min_soc", 20)),
                            "max_soc_limit": float(sd_cfg.get("max_soc", 90)),
                            # Peak window SOC guard rails
                            "min_peak_window_soc": float(sd_cfg.get("min_peak_window_soc", 60)),
                            "max_peak_window_soc": float(sd_cfg.get("max_peak_window_soc", 90)),
                            # Phase 2: expose min_export_price + max_charge_price for RHS value_ref resolution
                            "min_export_price": float(sd_cfg.get("min_export_price", 0.0)),
                            "max_charge_price": float(sd_cfg.get("max_charge_price", 15.0)),
                            # solar_pv_active: true if solar is currently generating (> 100W threshold)
                            "solar_pv_active": float(live_data.get("solar_kw", 0) or 0) > 0.1 if live_data else False,
                            # Phase 6: Expose Site Season and Forecast Loads
                            "site_season": site_season,
                            "forecast_load_kw": round(fl_kw, 2),
                            "forecast_load_active": fl_kw > 0.0,
                        }
                        # Home Loads namespaces (FHAI-native, scope-aware)
                        extra_ctx["home_load"]  = home_loads_ctx["home_load"]
                        extra_ctx["home_loads"] = home_loads_ctx["home_loads"]

                        # Phase 0: gateway context booleans for gateway.* conditions
                        extra_ctx["gateway"] = {
                            "has_solar": bool(live_data.get("has_solar", False)) if live_data else False,
                        }
                        
                        # Fetch and inject Solar Forecast context
                        tz_name = live_data.get("device", {}).get("timezone") if live_data else None
                        sf_metrics = await get_solar_forecast_metrics(gateway.short_id, tz_name)
                        extra_ctx["solar_forecast"] = sf_metrics

                        # Fetch and inject Weather context
                        extra_ctx["weather"] = {
                            "temp": float(live_data.get("weather_temp", 0.0) or 0.0) if live_data else 0.0,
                            "humidity": int(live_data.get("weather_humidity", 0) or 0) if live_data else 0,
                            "condition": str(live_data.get("weather_condition", "Unknown")) if live_data else "Unknown",
                            "icon": str(live_data.get("weather_icon", "")) if live_data else "",
                            "pressure": int(live_data.get("weather_pressure", 1013) or 1013) if live_data else 1013,
                            "active_storm_count": int(live_data.get("active_storm_count", 0) or 0) if live_data else 0,
                            "active_storm_warning": int(live_data.get("active_storm_warning", 0) or 0) if live_data else 0,
                        }
                        
                        # Phase 8: Reusable Macros / Base Conditions evaluation
                        extra_ctx["base"] = {}
                        if self.scheduler:
                            evaluating_macros = set()
                            for jb in self.scheduler.get_jobs():
                                kw = jb.kwargs
                                acts = kw.get("actions", [])
                                if acts and acts[0].get("type") == "base_condition":
                                    m_id = kw.get("rule_id")
                                    m_name = kw.get("rule_name", "")
                                    m_slug = m_name.lower().replace(" ", "_")
                                    
                                    val = await self._evaluate_macro(m_id, live_data, extra_ctx, evaluating_macros)
                                    extra_ctx["base"][m_slug] = val
                                    extra_ctx["base"][m_id] = val
                        
                        # Phase 3: Site Orchestrator — Virtual Site Meter
                        if self.registry:
                            site_snap = self.registry.get_site_snapshot()
                            extra_ctx["site"] = {
                                "p_fhp":         site_snap.get("p_fhp", 0.0),
                                "p_uti":         site_snap.get("p_uti", 0.0),
                                "p_sun":         site_snap.get("p_sun", 0.0),
                                "p_ld":          site_snap.get("p_ld", 0.0),
                                "soc_avg":       site_snap.get("soc_avg", 0.0),
                                "soc_min":       site_snap.get("soc_min", 0.0),
                                "soc_max":       site_snap.get("soc_max", 0.0),
                                "is_off_grid":   1 if site_snap.get("is_off_grid") else 0,
                                "is_three_phase": 1 if site_snap.get("is_three_phase") else 0,
                                "gw_count":      site_snap.get("count", 0),
                            }
            
            matched, failed_reasons, passed_reasons = evaluate_conditions(live_data, condition_logic, conditions, extra_context=extra_ctx)
            
            if matched:
                now_ts = int(time.time())
                if duration_secs > 0:
                    state = await get_automation_state(rule_id, gateway.short_id)
                    last_true_ts = state.get("last_true_ts") if state else None
                    if last_true_ts is None:
                        # First time evaluating to True, start timer
                        await upsert_automation_state(rule_id, gateway.short_id, now_ts, duration_secs)
                        logger.info(f"[{rule_name}] Conditions met. Starting timer for {duration_secs}s against {gateway.short_id}...")
                        continue
                        
                    if last_true_ts == -1:
                        # Timer already elapsed and action fired. Waiting for condition to reset to False.
                        continue
                        
                    elapsed = now_ts - last_true_ts
                    if elapsed < duration_secs:
                        logger.debug(f"[{rule_name}] Conditions met. Timer elapsed: {elapsed}s / {duration_secs}s")
                        continue
                    
                    # Timer fully elapsed! Mark state as executed (-1) and fire!
                    await upsert_automation_state(rule_id, gateway.short_id, -1, duration_secs)
                    logger.info(f"[{rule_name}] Duration timer of {duration_secs}s elapsed! Triggering action once.")
                else:
                    # No duration requirement, clear state just in case it was toggled on the fly
                    await clear_automation_state(rule_id, gateway.short_id)

                # ── Noise Reduction ──
                # Check if this rule is already waiting for an actionable response from the user.
                # If it is, skip re-triggering to prevent 'evaluation storms' and duplicate notifications.
                pending = await get_pending_approval(gateway_serial=gateway.short_id, rule_id=rule_id)
                if pending:
                    logger.debug(f"[{rule_name}] Skip trigger: Approval still pending for this rule (id={pending['request_id']})")
                    continue

                logger.info(f"[Rule {rule_id}] Triggered for target [{gateway.short_id}]! Firing {len(actions)} actions.")
                action_context = {
                    "gateway_serial": gateway.short_id,
                    "gateway_id": gateway.short_id,
                    "rule_id": rule_id,
                    "rule_name": rule_name,
                }
                # Add namespace values from extra_ctx
                # Two-level namespaces (pricing.*, dispatch.*, etc.) get both
                # the dotted form and a flat shortcut. Deeper nested namespaces
                # (home_load.<gw>.<slug>.<field>, home_loads.by_gateway.<gw>.*)
                # are fully flattened to any depth so str.format can resolve
                # them — without this, {home_load.global.hvac.controllability}
                # would silently fall back to literal text.
                if extra_ctx:
                    for ns, sub_dict in extra_ctx.items():
                        if not isinstance(sub_dict, dict):
                            continue
                        for k, v in sub_dict.items():
                            if isinstance(v, dict):
                                # Recurse: home_load -> global -> hvac -> {fields}
                                _flatten_to_dotted(v, f"{ns}.{k}", action_context)
                            else:
                                action_context[f"{ns}.{k}"] = v
                                # Legacy: flat shortcut for top-level keys when no collision
                                if k not in action_context:
                                    action_context[k] = v

                # Add live telemetry metrics
                if live_data:
                    for k, v in live_data.items():
                        action_context[k] = v
                    
                    # Expose legacy condition metric paths (e.g. battery.soc -> battery_soc value)
                    for path, mapped_key in LEGACY_MAP.items():
                        val = get_nested(live_data, path, extra_context=extra_ctx)
                        if val is not None:
                            action_context[path] = val
                            # Ensure the mapped key is also set
                            if mapped_key not in action_context:
                                action_key = mapped_key
                                action_context[action_key] = val

                    # Robust fallback for run_status_desc and battery.status
                    if not action_context.get("run_status_desc"):
                        run_status = live_data.get("run_status")
                        if run_status is not None:
                            run_status_map = {
                                0: "Standby",
                                1: "Charging",
                                2: "Discharging",
                                9: "VPP mode"
                            }
                            try:
                                action_context["run_status_desc"] = run_status_map.get(int(run_status), "Standby")
                            except (ValueError, TypeError):
                                action_context["run_status_desc"] = "Standby"
                        else:
                            # Try from status.battery_status nested path
                            bat_status = live_data.get("status", {}).get("battery_status")
                            if bat_status:
                                action_context["run_status_desc"] = bat_status
                            else:
                                action_context["run_status_desc"] = "Standby"
                    
                    action_context["battery.status"] = action_context["run_status_desc"]
                    action_context["battery_status"] = action_context["run_status_desc"]
                    action_context["status_battery_status"] = action_context["run_status_desc"]

                for action in actions:
                    # Skip actions explicitly disabled in the rule builder
                    if not action.get("enabled", True):
                        logger.debug(f"[Rule {rule_id}] Action {action.get('type')} is disabled — skipping")
                        continue
                    # Create a copy of the action to avoid mutating the cached/scheduled rule definition
                    action_copy = dict(action)
                    # Inject caller context so hardware commands (set_mode, etc.) can identify the source rule
                    action_copy["_caller"] = f"AB:Rule:{rule_name}:{rule_id[:8]}"
                    action_type_str = action_copy.get("type", "unknown")
                    
                    if "payload" in action_copy:
                        action_copy["payload"] = expand_placeholders(action_copy["payload"], action_context)

                    try:
                        # Insert 'running' entry for the trace modal
                        hist_id = await insert_automation_history(
                            rule_id, rule_name, gateway.short_id, action_type_str, action_copy.get("payload", {}), "running", "Executing action...", request_id=trace_id
                        )
                        
                        status, detail = await self._execute_action(gateway, action_copy, action_context=action_context, trace_id=trace_id, hist_id=hist_id)
                        await update_automation_history(hist_id, status, detail)
                        # ── Mirror to Audit Trail (admin_audit_log) ──
                        # This makes AB rule executions visible in Logs > Audit Trail
                        # alongside Mode Change and other GatewayService events.
                        _audit_icon = "✅" if status == "success" else ("⚠" if status in ("skipped", "dispatched") else "❌")
                        log_msg = f"{_audit_icon} [{status.upper()}] Rule '{rule_name}' → {action_type_str} on {gateway.short_id}: {detail}"
                        logger.info(log_msg)
                        asyncio.create_task(log_admin_audit("Automation Trigger", "AutomationBuilder", owner, log_msg))
                        
                        # Phase 0: If an action explicitly failed (not skipped/success), stop the chain
                        if status not in ("success", "skipped", "dispatched"):
                            logger.warning(f"[Rule {rule_id}] Action {action_type_str} returned status '{status}' — aborting subsequent actions in this rule.")
                            break

                    except Exception as e:
                        logger.error(f"[Rule {rule_id}] Action failed against {gateway.short_id}: {e}")
                        await insert_automation_history(
                            rule_id, rule_name, gateway.short_id, action_type_str, action_copy.get("payload", {}), "error", str(e), request_id=trace_id
                        )
                        asyncio.create_task(log_admin_audit(
                            "Automation Trigger",
                            "AutomationBuilder",
                            owner,
                            f"❌ [ERROR] Rule '{rule_name}' → {action_type_str} on {gateway.short_id}: {e}"
                        ))
                        # Phase 0: ABORT on exception — do not continue to next action in the rule
                        break
            else:
                if duration_secs > 0:
                    state = await get_automation_state(rule_id, gateway.short_id)
                    if state and state.get("last_true_ts") is not None:
                        # Conditions failed. Break the timer and reset the state.
                        await clear_automation_state(rule_id, gateway.short_id)
                        logger.debug(f"[{rule_name}] Condition failed. Resetting duration timer for {gateway.short_id}.")
                    
                logger.info(f"[{rule_name}] Conditions not met for {gateway.short_id}.")
                trace = []
                if passed_reasons:
                    trace.append("✅ Passed:\n" + "\n".join(f"  • {r}" for r in passed_reasons))
                if failed_reasons:
                    trace.append("❌ Failed:\n" + "\n".join(f"  • {r}" for r in failed_reasons))
                
                reason_str = "\n\n".join(trace) if trace else "Rule execution skipped because sensor conditions evaluated to False."
                await insert_automation_history(
                    rule_id, rule_name, gateway.short_id, "condition_check", conditions, "skipped", reason_str, request_id=trace_id
                )

# Global singleton
automation_engine = None 

async def run_automation_job(rule_id: str, rule_name: str, gateway_serial: str, condition_logic: str, conditions: List[dict], actions: List[dict], owner: str = "user", tags: Optional[List[str]] = None, cron: Optional[str] = None, run_at: Optional[str] = None, created_at: Optional[str] = None, updated_at: Optional[str] = None, duration_secs: int = 0, priority: int = 25, **kwargs):
    """APScheduler job callable. priority is stored in kwargs for UI display but not used during execution."""
    global automation_engine
    if automation_engine:
        # Parallel execution enabled (lock removed)
        await automation_engine.run_rule(rule_id, rule_name, gateway_serial, condition_logic, conditions, actions, owner, tags, cron, run_at, created_at, updated_at, duration_secs, **kwargs)

def init_engine(db_path: str, registry) -> AutomationEngine:
    global automation_engine
    automation_engine = AutomationEngine(db_path)
    automation_engine.registry = registry
    return automation_engine
