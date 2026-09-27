"""
SmartDispatchEngine — B1 Automation Rule Evaluator.

Evaluates the active automation rulebook against the current PriceSnapshot
and live gateway SoC. Returns a RuleResult describing what action would be
taken (signal-only in B1). Auto-execution of APPLY_PRESET is gated on the
"Amber Default" baseline preset existing; without it the engine serves
recommendations only and warns in the signal response.

Condition DSL (stored as JSON in automation_rules.condition_json):
  {}                              → always true (catch-all)
  {"operator": "AND", "conditions": [
    {"field": "descriptor",   "op": "IN",  "value": ["negative", "extremelyLow"]},
    {"field": "spike_status", "op": "EQ",  "value": "none"},
    {"field": "soc_pct",      "op": "LT",  "value": 90}
  ]}

Supported fields:
  import_c_kwh, export_c_kwh, descriptor, spike_status, tariff_type,
  tariff_period, demand_window, renewables_pct, soc_pct

Supported operators:
  EQ, NEQ, LT, GT, LTE, GTE, IN, NOT_IN

Reserved preset names (Amber namespace):
  "Amber Default"         — mandatory baseline; RESUME always applies this
  "Amber Force Charge"    — aggressive grid charge (negative / extremelyLow price)
  "Amber Peak Discharge"  — export maximised during PEAK / SPIKE
  "Amber Solar Sponge"    — export maximised during solarSponge window
  "Amber Spike Hold"      — no import/export during confirmed or developing spike
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.services import db
import httpx
from src.services.notification_sender import _get_ha_credentials
from src.services.pricing.base import PriceSnapshot
from src.services.intent_lock import manager as intent_manager

# Re-exports (Phase 1 Stage A, 2026-08-05) — RuleResult + EvalDecision
# live in `smart_dispatch.models` now. This import both wires them for
# in-file use and exposes them under `from src.services.smart_dispatch
# import RuleResult, EvalDecision` for the 15 external import sites.
from src.services.smart_dispatch.models import RuleResult, EvalDecision

# Re-exports (Phase 1 Stage B, 2026-08-05) — site season + Home Loads
# context helpers moved to `smart_dispatch.loads`. Re-exported so
# `from src.services.smart_dispatch import get_site_season, ...` keeps
# working (mqtt_publisher, api_smart_dispatch, scheduler_core).
from src.services.smart_dispatch.loads import (
    get_site_season,
    resolve_site_season_for_gateway,
    build_home_loads_context,
    is_schedule_active,
    is_heating_cooling_load,
    get_active_forecast_load_kw,
)

# Re-exports (Phase 1 Stage C, 2026-08-05) — context builder + evaluated-
# params helper moved to `smart_dispatch.context`. Both keep their
# leading-underscore names because they are internal helpers, but they
# ARE imported by name from routes/api_smart_dispatch.py etc., so the
# re-export is required.
from src.services.smart_dispatch.context import _build_context, _build_evaluated_params

# Re-exports (Phase 1 Stage D, 2026-08-05) — condition DSL, all 8
# _eval_* helpers, HA service caller, dispatch payload calculator, and
# StrategyMixer moved to `smart_dispatch.evaluator`. `StrategyMixer` is
# re-exported for tests (tests/test_dispatch_strategy.py imports it).
# The engine class below uses the leading-underscore helpers directly.
from src.services.smart_dispatch.evaluator import (
    StrategyMixer,
    evaluate_condition,
    _eval_time_schedules,
    _eval_demand_charge,
    _eval_negative_export,
    _eval_price_spike,
    _eval_export_bonus,
    _eval_force_export,
    _eval_earnings_target,
    _eval_force_charge,
    _call_ha_service,
    _calc_dispatch_payload,
)

logger = logging.getLogger(__name__)

# Max age of gateway telemetry before SD refuses to dispatch on it (GH #35).
# Default poll interval is 30s, so this is ~6 missed cycles — loose enough to
# ride out ordinary cloud flakiness, tight enough that a real degradation
# stops the engine acting on a frozen SoC.
#
# The gate also reads a per-gateway `max_telemetry_age_s` from
# smart_dispatch_config. That column does NOT exist yet, so the read is
# forward-compatible and inert — this constant governs in practice. Adding
# the column needs a schema bump and is deliberately out of scope here.
_DEFAULT_MAX_TELEMETRY_AGE_S = 180

# ── Reserved preset names ────────────────────────────────────────────────────
AMBER_BASELINE_PRESET   = "Smart Default"
SD_RESERVED_PRESETS  = {
    "Smart Default",
    "Smart Force Charge",
    "Smart Peak Discharge",
    "Smart Solar Sponge",
    "Smart Spike Hold",
}


class ActuatorBridge:
    """
    Maps SD Signals to Physical Actuators (HA Entities, Services, or FWH Cloud).
    """

    def __init__(self, registry=None):
        self._registry = registry

    async def execute(self, gateway_serial: str, result: EvalDecision) -> bool:
        """
        Execute the physical action associated with the given decision.
        """
        action = result.action
        if action in ("NONE", "HOLD"):
            return True

        # 1. Fetch Actuator Map
        actuators = await db.get_sd_actuators()
        actuator = next((a for a in actuators if a["signal_key"] == action), None)
        
        if not actuator:
            logger.warning(f"ActuatorBridge: no actuator mapped for signal '{action}'")
            return False

        # 2. Execute based on type
        a_type = actuator["actuator_type"]
        target = actuator["target"]
        
        logger.info(f"ActuatorBridge [{gateway_serial}]: executing {action} via {a_type} -> {target}")
        
        if a_type == "fwh_cloud":
            # Direct cloud call via GatewayRegistry
            if not self._registry:
                logger.error("ActuatorBridge: gateway registry not available")
                return False
            
            gw = self._registry.get_gateway(gateway_serial)
            if not gw:
                logger.error(f"ActuatorBridge: gateway {gateway_serial} not found in registry")
                return False

            # Implementation of cloud actions
            res = False
            if target == "grid_charge":
                res = await gw.set_grid_charge(True)
            elif target == "grid_export":
                res = await gw.set_grid_export(True)
            elif target == "solar_relay":
                res = await gw.set_solar_relay(False)
            elif target == "standby":
                res = await gw.set_mode(4) # Emergency Backup
                
            return res
        
        elif a_type == "ha_service":
            # Delegate to HomeAssistantBridge (to be implemented)
            try:
                from src.services.ha_bridge import manager as ha_bridge
                return await ha_bridge.call_service(target, actuator.get("params_json", "{}"))
            except Exception as e:
                logger.error(f"ActuatorBridge: HA service call failed: {e}")
                return False
        
        return False


# ── Notification event-key map (schema v47 cooldown feature) ─────────────────
# Maps trigger_category → (ev_key, per-gateway toggle_key). ev_key is the
# canonical key used by:
#   - src/services/notification_sender.py (event routing)
#   - notification_cooldown_rules DB table (per-rule cooldown config)
#   - notification_cooldown DB table (active suppression state)
# Extracted to module scope so both the actionable path (line ~3396) and the
# info-notification hook (line ~3437) can consult the same mapping.
NOTIFICATION_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # ── 7 canonical categories (seeded v47) ───────────────────────────────
    "demand_charge":            ("demand_charge",      "notify_on_demand_charge"),
    "negative_export":          ("negative_export",    "notify_on_negative_export"),
    "price_spike":              ("spike",              "notify_on_spike"),
    "export_bonus":             ("export_bonus",       "notify_on_export_bonus"),
    "earnings_target":          ("earnings_target",    "notify_on_earnings"),
    "force_charge":             ("force_charge",       "notify_on_force_charge"),
    "force_export":             ("force_export",       "notify_on_force_export"),
    # ── v48: gap-fill for categories present in live rule matrix ──────────
    # Shared buckets (same signal type as an existing canonical):
    "negative_export_advisory": ("negative_export",    "notify_on_negative_export"),
    "pricing":                  ("negative_export",    "notify_on_negative_export"),
    # New dedicated buckets (seeded v48 in notification_cooldown_rules):
    "time_schedule":            ("time_schedule",      "notify_on_signal_change"),
    "solar_optimization":       ("solar_optimization", "notify_on_signal_change"),
    "custom":                   ("custom",             "notify_on_signal_change"),
    # `fallback` (default on EvalDecision.trigger_category) intentionally
    # unmapped — falls through to the safe default ("signal_change", "")
    # which is NOT a seeded cooldown rule, so is_active() returns None and
    # legitimate diverse fallback signals aren't collapsed under one bucket.
}


class SmartDispatchEngine:
    """
    Rule evaluator — 6-rule priority chain + legacy DSL rulebook.

    evaluate_rules()     → EvalDecision   6-category chain (NEW), no side-effects
    evaluate()           → RuleResult     legacy DSL rulebook (unchanged, backward compat)
    evaluate_and_log()   → EvalDecision   evaluate_rules() + write to pricing_eval_log + automation_history

    Rule priority order (first match wins):
      1. demand_charge     — demand window + SOC below min
      2. negative_export   — export price < 0 (solar disable or off-grid)
      3. price_spike       — spike/potential (hold or discharge)
      4. export_bonus      — export price > threshold or solarSponge
      5. earnings_target   — daily/monthly gap (billing cycle must be configured)
      6. force_charge      — negative/extremelyLow import descriptor
      7. legacy_rules      — user-defined DSL rulebook (if any)
      8. fallback          — Self-Consumption (explicit default_mode=SELF)
    """

    def __init__(self, presets_mgr=None):
        """
        presets_mgr: optional SchedulePresets instance for baseline-check.
        If None, can_execute will always be False.
        """
        self._presets_mgr = presets_mgr
        self._gateway_registry = None              # set via set_gateway_registry() at startup
        self._last_action: dict[str, str] = {}    # gateway_serial → last action key
        self._last_action_at: dict[str, float] = {}      # gateway_serial → first time this action seen
        self._last_winner_rule_id: dict[str, str] = {}   # gateway_serial → last winning rule_id
        self.MIN_ACTION_DURATION = 300  # 5 minutes minimum for an action unless overridden by higher priority

        self._last_pushed: dict[str, str] = {}    # gateway_serial → last pushed preset name

        # ── T-30min lookahead dedup state ─────────────────────────────────────
        # Persisted in `sd_lookahead_dedup` (schema v51, Batch S, 2026-08-02).
        # Previously two process-local dicts here — a container restart wiped
        # them and the first post-startup tick re-fired lookahead notifications
        # for any future hour-bucket still matching thresholds. Now backed by
        # SQLite via db.get_lookahead_sent_at / mark_lookahead_sent /
        # prune_lookahead_sent so the dedup survives restarts. See
        # `_maybe_send_lookahead_notifications` for the read/write sites.

    def set_presets_manager(self, mgr) -> None:
        self._presets_mgr = mgr

    def set_gateway_registry(self, registry) -> None:
        """Inject the GatewayRegistry so active mode can push presets to gateways."""
        self._gateway_registry = registry

    # ── Baseline check ───────────────────────────────────────────────────────

    def _baseline_exists(self) -> bool:
        """True if the 'Smart Default' baseline preset is present."""
        if self._presets_mgr is None:
            return False
        try:
            result = self._presets_mgr.load_preset(AMBER_BASELINE_PRESET)
            return bool(result.get("success"))
        except Exception:
            return False

    async def synthesize_and_optimize(
        self,
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
        force: bool = False
    ) -> tuple[str, float]:
        """
        Phase 2:
        1. Cadence Controller: Only run full optimization at 12am/6am/12pm/6pm hours (or if force=True / first run).
           Otherwise, load cached plan, trim elapsed slots, and tweak PV/Load/SOC trajectories in the lookahead window.
        2. Dynamic Home & Live Load Synthesis: Use True Current Household Load or Fallback formulas.
        3. Extreme Weather Load Scaling: Scale active HVAC loads by 1.25x in extreme temperatures.
        """
        import os
        import json as _json
        from datetime import datetime, timezone, timedelta
        from pathlib import Path
        from src.services.solar import SolarForecastManager
        from src.services.db import get_solar_forecast_config, get_config_value, get_all_forecast_loads, get_gateway_solar_kwp_total

        # Set default values if not configured
        min_soc = cfg.get("min_soc") if cfg.get("min_soc") is not None else 20.0
        max_soc = cfg.get("max_soc") if cfg.get("max_soc") is not None else 90.0
        
        # Max battery capacity and power
        battery_kwh = 13.6
        try:
            reg = self._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    cap = gw.status.last_data.get("battery_capacity_kwh")
                    if cap and float(cap) > 0:
                        battery_kwh = float(cap)
        except Exception:
            pass

        max_power_kw = 5.0 # default ~5kW
        eff = 0.95 # standard charge/discharge efficiency

        # Get solar configuration and forecast slots
        solar_slots = []
        try:
            solar_cfg = await get_solar_forecast_config()
            _source = solar_cfg.get("source") or ""
            _lat    = solar_cfg.get("lat")
            _lng    = solar_cfg.get("lng")

            if _source and _source != "none" and _lat and _lng:
                ha_url   = await get_config_value("ha_host",  "") or os.environ.get("HA_HOST",  "")
                ha_token = await get_config_value("ha_token", "") or os.environ.get("HA_TOKEN", "") \
                           or os.environ.get("SUPERVISOR_TOKEN", "")
                data_dir = Path(os.environ.get("DATA_DIR", "./data"))
                solar_cfg["ha"] = {
                    "ha_url":   ha_url,
                    "ha_token": ha_token,
                    "solar_actual_entity":   solar_cfg.get("ha_solar_actual_entity") or "",
                    "solar_forecast_entity": solar_cfg.get("ha_solar_forecast_entity") or "",
                }
                solar_cfg["installation"] = {
                    "lat":     _lat,
                    "lng":     _lng,
                    "azimuth": solar_cfg.get("azimuth", 180.0),
                    "tilt":    solar_cfg.get("tilt", 22.5),
                    "kwp":     solar_cfg.get("kwp", 5.0),
                }
                solar_cfg["providers"] = {
                    "forecast_solar": {
                        "api_key": solar_cfg.get("forecast_solar_api_key") or "",
                    },
                    "solcast": {
                        "api_key":  solar_cfg.get("solcast_api_key") or "",
                        "site_id":  solar_cfg.get("solcast_site_id") or "",
                        "_data_dir": str(data_dir),
                    },
                }
                mgr = SolarForecastManager(config=solar_cfg, data_dir=data_dir)
                solar_slots = mgr.get_forecast()
        except Exception as err:
            logger.debug(f"synthesize_and_optimize: Solar forecast unavailable: {err}")

        # Baseline home load
        base_home_load_kw = 0.5
        try:
            solar_cfg = await get_solar_forecast_config()
            base_home_load_kw = float(solar_cfg.get("home_load_assumption_kw") or 0.5)
        except Exception:
            pass

        # Load forecasts
        try:
            forecast_loads = await get_all_forecast_loads()
        except Exception:
            forecast_loads = []

        # Site season and HA states
        site_season = await resolve_site_season_for_gateway(gateway_id, self._gateway_registry)
        ha_states = {}

        try:
            from src.routes.api_ha import get_ha_state
            for fl in forecast_loads:
                if fl.get("enabled", 1) and fl.get("measurement_type") == "now":
                    for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                        ent_id = fl.get(key)
                        if ent_id:
                            st = await get_ha_state(ent_id)
                            if st and "state" in st:
                                ha_states[ent_id] = st["state"]
                                break
        except Exception:
            pass

        # Live solar fallback
        live_solar_kw = 0.0
        try:
            reg = self._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    live_solar_kw = float(gw.status.last_data.get("solar_kw", 0.0))
        except Exception:
            pass

        # ── Cadence Controller & Caching logic ────────────────────────────────
        now_local = datetime.now().astimezone()
        last_full_gen = cfg.get("last_full_generation_time")

        def is_fixed_generation_time(now_local, last_gen_str) -> bool:
            if not last_gen_str:
                return True
            try:
                last_gen = datetime.fromisoformat(last_gen_str).astimezone()
            except Exception:
                return True
            current_block = now_local.hour // 6
            last_block = last_gen.hour // 6
            if current_block != last_block or now_local.date() != last_gen.date():
                return True
            return False

        need_full_opt = force or not last_full_gen or is_fixed_generation_time(now_local, last_full_gen)

        cached_plan = None
        if not need_full_opt:
            try:
                hist = await db.get_latest_forecast_history()
                if hist and hist.get("plan_json"):
                    cached_plan = _json.loads(hist["plan_json"])
            except Exception as e:
                logger.debug(f"Failed to read cached forecast history: {e}")

        # If cached plan exists, tweak it!
        if cached_plan and isinstance(cached_plan, list) and len(cached_plan) > 0:
            active_plan = []
            for slot in cached_plan:
                try:
                    slot_end = datetime.fromisoformat(slot["end"])
                    if not slot_end.tzinfo:
                        slot_end = slot_end.replace(tzinfo=timezone.utc)
                    if slot_end > now_local:
                        active_plan.append(slot)
                except Exception:
                    pass

            # If remaining plan is reasonably long, run the tweaked lookahead path
            if len(active_plan) >= 6:
                logger.info(f"SmartDispatch [{gateway_id}]: Tweaking cached forecast plan for lookahead window")
                lookahead_mins = cfg.get("lookahead_minutes", 0) or 0
                lookahead_until = now_local + timedelta(minutes=lookahead_mins)
                weather_impact = bool(cfg.get("weather_extreme_impact", 0))

                is_extreme = False
                try:
                    from src.routes.api_weather import get_weather_forecast
                    wf = await get_weather_forecast(gateway_id)
                    if wf and wf.get("ok"):
                        today_str = now_local.strftime("%Y-%m-%d")
                        for day in wf.get("forecast", []):
                            if day.get("date") == today_str:
                                temp_high = float(day.get("temp_high", 20.0))
                                temp_low = float(day.get("temp_low", 15.0))
                                is_extreme = temp_high > 38.0 or temp_low < 5.0
                                break
                except Exception:
                    pass

                live_p_ld_kw = 0.0
                try:
                    reg = self._gateway_registry
                    if reg:
                        gw = reg.get_gateway(gateway_id)
                        if gw and gw.status and gw.status.last_data:
                            live_p_ld_kw = float(gw.status.last_data.get("load_kw", 0.0))
                except Exception:
                    pass

                tweaked_slots = []
                rolling_soc = soc_pct if soc_pct is not None else 50.0

                for idx, slot in enumerate(active_plan):
                    slot_start = datetime.fromisoformat(slot["start"])
                    if not slot_start.tzinfo:
                        slot_start = slot_start.replace(tzinfo=timezone.utc)

                    in_lookahead = (idx == 0) or (slot_start <= lookahead_until)

                    if in_lookahead:
                        if idx == 0:
                            # Slot 0: True Household Load = site.p_ld + sum(monitored loads with is_behind_meter = 0)
                            unmonitored_p_ld_kw = 0.0
                            fallback_monitored_kw = 0.0
                            for load in forecast_loads:
                                if not load.get("enabled", 1):
                                    continue
                                is_behind = int(load.get("is_behind_meter", 1))
                                m_type = load.get("measurement_type", "forecast")
                                avg_kw = float(load.get("avg_kw", 0.0))
                                if weather_impact and is_extreme and is_heating_cooling_load(load.get("name", "")):
                                    avg_kw *= 1.25

                                is_active_now = False
                                if m_type == "now":
                                    resolved = False
                                    for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                                        ent_id = load.get(key)
                                        if ent_id and ha_states.get(ent_id) in ("on", "true", "running", "active", "charging"):
                                            is_active_now = True
                                            break
                                else:
                                    schedule_json = load.get("schedule_json", "[]")
                                    try:
                                        import json
                                        periods = json.loads(schedule_json)
                                    except Exception:
                                        periods = []
                                    for p in periods:
                                        if is_schedule_active(p, slot_start, site_season):
                                            is_active_now = True
                                            break

                                if is_active_now:
                                    fallback_monitored_kw += avg_kw
                                    if is_behind == 0:
                                        unmonitored_p_ld_kw += avg_kw

                            if live_p_ld_kw <= 0.05:
                                home_load_kw = base_home_load_kw + fallback_monitored_kw
                            else:
                                home_load_kw = live_p_ld_kw + unmonitored_p_ld_kw
                        else:
                            # Slot 1 to N: Projected Load = Baseline + sum(forecast/monitored loads active at t)
                            active_load_kw = get_active_forecast_load_kw(
                                forecast_loads,
                                slot_start,
                                site_season,
                                is_current_slot=False,
                                ha_states=ha_states,
                                gateway_id=gateway_id,
                                weather_extreme_impact=weather_impact,
                                is_extreme_temp=is_extreme
                            )
                            home_load_kw = base_home_load_kw + active_load_kw

                        pv_kw = 0.0
                        if solar_slots:
                            best_match = None
                            best_diff = float('inf')
                            for ss in solar_slots:
                                try:
                                    ss_time = datetime.fromisoformat(ss.get("timestamp"))
                                    if not ss_time.tzinfo:
                                        ss_time = ss_time.replace(tzinfo=timezone.utc)
                                    diff = abs((slot_start - ss_time).total_seconds())
                                    if diff < best_diff and diff <= 3600:
                                        best_diff = diff
                                        best_match = ss
                                except Exception:
                                    pass
                            if best_match:
                                pv_kw = float(best_match.get("pv_kw", 0.0))

                        if idx == 0 and pv_kw == 0.0 and live_solar_kw > 0.1:
                            pv_kw = live_solar_kw

                        slot["pv_kw"] = round(pv_kw, 2)
                        slot["home_load_kw"] = round(home_load_kw, 2)

                    slot_pv = float(slot["pv_kw"]) * 0.5
                    slot_load = float(slot["home_load_kw"]) * 0.5
                    action = slot["action"]

                    charge_kw = 0.0
                    discharge_kw = 0.0

                    if action == "GRID_CHARGE":
                        capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                        charge_kw = min(max_power_kw, capacity_needed_kwh / 0.5)
                    elif action == "GRID_EXPORT":
                        capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                        discharge_kw = min(max_power_kw, capacity_available_kwh / 0.5)
                    else:
                        net_kw = slot_pv - slot_load
                        if net_kw > 0:
                            capacity_needed_kwh = battery_kwh * max(0.0, (max_soc - rolling_soc) / 100.0)
                            charge_kw = min(net_kw / 0.5, max_power_kw, capacity_needed_kwh / 0.5)
                        else:
                            capacity_available_kwh = battery_kwh * max(0.0, (rolling_soc - min_soc) / 100.0)
                            discharge_kw = min(abs(net_kw / 0.5), max_power_kw, capacity_available_kwh / 0.5)

                    net_battery_kw = charge_kw - discharge_kw
                    delta_pct = ((net_battery_kw * 0.5) / battery_kwh) * 100.0
                    rolling_soc = max(0.0, min(100.0, rolling_soc + delta_pct))

                    slot["projected_soc"] = round(rolling_soc, 1)
                    slot["charge_kw"] = round(charge_kw, 2)
                    slot["discharge_kw"] = round(discharge_kw, 2)

                    tweaked_slots.append(slot)

                await db.save_forecast_history(
                    generated_at=now_local.isoformat(),
                    start=tweaked_slots[0]["start"],
                    end=tweaked_slots[-1]["end"],
                    plan_json=_json.dumps(tweaked_slots)
                )
                await db.prune_forecast_history(max_keep=48)
                return tweaked_slots[0]["action"], tweaked_slots[0]["projected_soc"]

        # ── Fallback to full Greedy water-filling optimization ────────────────
        logger.info(f"SmartDispatch [{gateway_id}]: Running full greedy forecast optimization")
        await db.upsert_smart_dispatch_config(gateway_id, last_full_generation_time=now_local.isoformat())

        N = min(48, len(snap.forecast))
        if N == 0:
            return "HOLD", soc_pct or 50.0

        ImportPrice = []
        ExportPrice = []
        PV = []
        L = []
        Periods = []

        weather_impact = bool(cfg.get("weather_extreme_impact", 0))
        is_extreme = False
        try:
            from src.routes.api_weather import get_weather_forecast
            wf = await get_weather_forecast(gateway_id)
            if wf and wf.get("ok"):
                today_str = now_local.strftime("%Y-%m-%d")
                for day in wf.get("forecast", []):
                    if day.get("date") == today_str:
                        temp_high = float(day.get("temp_high", 20.0))
                        temp_low = float(day.get("temp_low", 15.0))
                        is_extreme = temp_high > 38.0 or temp_low < 5.0
                        break
        except Exception:
            pass

        for idx in range(N):
            period = snap.forecast[idx]
            Periods.append(period)

            # Resolve PV temporal matching
            pv_kw = 0.0
            if solar_slots:
                p_start = period.start
                if not p_start.tzinfo:
                    p_start = p_start.replace(tzinfo=timezone.utc)
                best_match = None
                best_diff = float('inf')
                for ss in solar_slots:
                    try:
                        ss_time = datetime.fromisoformat(ss.get("timestamp"))
                        if not ss_time.tzinfo:
                            ss_time = ss_time.replace(tzinfo=timezone.utc)
                        diff = abs((p_start - ss_time).total_seconds())
                        if diff < best_diff and diff <= 3600:
                            best_diff = diff
                            best_match = ss
                    except Exception:
                        pass
                if best_match:
                    pv_kw = float(best_match.get("pv_kw", 0.0))

            is_current_slot = idx == 0
            if is_current_slot and pv_kw == 0.0 and live_solar_kw > 0.1:
                pv_kw = live_solar_kw

            # Home Load synthesis
            active_load_kw = get_active_forecast_load_kw(
                forecast_loads,
                period.start,
                site_season,
                is_current_slot=is_current_slot,
                ha_states=ha_states,
                gateway_id=gateway_id,
                weather_extreme_impact=weather_impact,
                is_extreme_temp=is_extreme
            )
            home_load_kw = base_home_load_kw + active_load_kw

            # Convert to Dollars ($) and round fractional cents (i.e. round to 2 decimal places)
            buy_price = round(period.import_c_kwh) / 100.0
            sell_price = round(period.export_c_kwh) / 100.0 if period.export_c_kwh is not None else 0.0

            ImportPrice.append(buy_price)
            ExportPrice.append(sell_price)
            PV.append(pv_kw * 0.5)      # kWh in 30 mins
            L.append(home_load_kw * 0.5) # kWh in 30 mins

        # ── Greedy Water-filling Arbitrage Solver ─────────────────────────────
        E_init = ((soc_pct if soc_pct is not None else 50.0) / 100.0) * battery_kwh
        E_min_val = (min_soc / 100.0) * battery_kwh
        E_max_val = (max_soc / 100.0) * battery_kwh

        E = [0.0] * N
        grid_charge = [0.0] * N
        grid_discharge = [0.0] * N

        # 1. Passive baseline simulation
        prev_E = E_init
        for t in range(N):
            net = PV[t] - L[t]
            if net > 0:
                pass_charge = min(net, max_power_kw * 0.5, E_max_val - prev_E)
                E[t] = prev_E + pass_charge
            else:
                pass_discharge = min(abs(net), max_power_kw * 0.5, prev_E - E_min_val)
                E[t] = prev_E - pass_discharge
            prev_E = E[t]

        # 2. Optimization loops
        while True:
            best_pair = None
            best_profit = 0.0
            best_dE = 0.0

            for tc in range(N):
                for td in range(N):
                    if tc == td:
                        continue

                    p_buy = ImportPrice[tc]
                    p_sell = ExportPrice[td]
                    if p_sell < 0.0:
                        continue

                    profit = (p_sell * eff) - (p_buy / eff)
                    if profit <= 1e-4:
                        continue

                    # Power limit at tc
                    net_tc = PV[tc] - L[tc]
                    pass_c = max(0.0, net_tc) if net_tc > 0 else 0.0
                    rem_c_power = max(0.0, max_power_kw * 0.5 - pass_c - grid_charge[tc])
                    max_dE_charge = rem_c_power * eff

                    # Power limit at td
                    net_td = PV[td] - L[td]
                    pass_d = max(0.0, -net_td) if net_td < 0 else 0.0
                    rem_d_power = max(0.0, max_power_kw * 0.5 - pass_d - grid_discharge[td])
                    max_dE_discharge = rem_d_power / eff

                    dE = min(max_dE_charge, max_dE_discharge)
                    if dE <= 1e-4:
                        continue

                    # Path energy limit
                    if tc < td:
                        for t in range(tc, td):
                            dE = min(dE, E_max_val - E[t])
                    else:
                        for t in range(td, tc):
                            dE = min(dE, E[t] - E_min_val)

                    if dE <= 1e-4:
                        continue

                    total_profit = dE * profit
                    if total_profit > best_profit:
                        best_profit = total_profit
                        best_pair = (tc, td)
                        best_dE = dE

            if not best_pair or best_dE <= 1e-4:
                break

            # Apply transfer
            tc, td = best_pair
            grid_charge[tc] += best_dE / eff
            grid_discharge[td] += best_dE * eff

            if tc < td:
                for t in range(tc, td):
                    E[t] += best_dE
            else:
                for t in range(td, tc):
                    E[t] -= best_dE

        # ── Save optimized plan and forecast history ──────────────────────────
        plan_slots = []
        for idx in range(N):
            period = Periods[idx]
            plan_action = "HOLD"
            if grid_charge[idx] > 0.05:
                plan_action = "GRID_CHARGE"
            elif grid_discharge[idx] > 0.05:
                plan_action = "GRID_EXPORT"

            projected_soc = round((E[idx] / battery_kwh) * 100.0, 1)

            plan_slots.append({
                "start":          period.start.isoformat(),
                "end":            period.end.isoformat(),
                "import_price":   round(ImportPrice[idx], 4),
                "export_price":   round(ExportPrice[idx], 4),
                "action":         plan_action,
                "projected_soc":  projected_soc,
                "pv_kw":          round(PV[idx] / 0.5, 2),
                "home_load_kw":   round(L[idx] / 0.5, 2),
                "charge_kw":      round((grid_charge[idx]) / 0.5, 2),
                "discharge_kw":   round((grid_discharge[idx]) / 0.5, 2)
            })

        generated_at = datetime.now(timezone.utc).isoformat()
        horizon_start = Periods[0].start.isoformat()
        horizon_end = Periods[-1].end.isoformat()
        
        await db.save_forecast_history(
            generated_at=generated_at,
            start=horizon_start,
            end=horizon_end,
            plan_json=_json.dumps(plan_slots)
        )
        await db.prune_forecast_history(max_keep=48)

        # ── Determine action and target SOC for the first slot (current moment) ──
        current_opt = plan_slots[0]
        return current_opt["action"], current_opt["projected_soc"]

    # ── 6-Rule Category Chain ─────────────────────────────────────────────────

    async def evaluate_rules(
        self,
        snap: PriceSnapshot,
        soc_pct: Optional[float] = None,
        gateway_id: str = "",
        provider: Optional[str] = None,
        verbose: bool = True
    ) -> EvalDecision:
        """
        Run the Matrix Mixer (Phase 2), then fall back to scheduled overrides and legacy rulebook.
        """
        if not gateway_id:
            # Silence log spam for empty gateway context (likely UI poll before setup)
            return EvalDecision(action="HOLD", rule_name="No Context", priority=100)

        if verbose:
            logger.debug(f"SmartDispatch [{gateway_id}]: evaluate_rules ENTERING (soc={soc_pct})")

        # 1. Baseline & Context
        await db.check_and_trigger_session_reversion(gateway_id)
        dna = await db.get_gateway_by_full_serial(gateway_id) if gateway_id else None
        try:
            cfg = await db.get_smart_dispatch_config(gateway_id) if gateway_id else {}
        except Exception:
            cfg = {}
        
        site_snap = self._gateway_registry.get_site_snapshot() if self._gateway_registry else None
        ctx = await _build_context(snap, soc_pct, dna, cfg, site_snap=site_snap)
        baseline_ok = self._baseline_exists()

        # Check grid connectivity — FAIL CLOSED (GH #35).
        #
        # This previously defaulted to True in every unknown case, including
        # a live telemetry payload that simply lacked the key. The Off-Grid
        # Safety Lock below exists to prohibit GRID_CHARGE / GRID_EXPORT while
        # islanded, so defaulting to "connected" made the lock fail OPEN — it
        # permitted exactly the actions it exists to block, on the strength of
        # missing data. With cloud payloads increasingly arriving degraded,
        # absent must mean "assume islanded", not "assume fine".
        #
        # Scope note: True is still the starting value so the no-hardware and
        # emulation paths (no registry, no gateway) behave as before. The
        # change is specifically that a REAL payload missing/None-valued
        # grid_connected now resolves to False rather than True.
        grid_connected = True
        try:
            reg = self._gateway_registry
            if reg:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    _ld = gw.status.last_data
                    # `grid_connection_state` is the field the cloud actually
                    # populates ('Connected' / 'Outage'); `grid_connected` is a
                    # legacy boolean that real payloads do NOT carry. Check the
                    # real one first — the same order _build_evaluated_params
                    # already uses. Getting this wrong makes the fail-closed
                    # branch below fire on every healthy poll.
                    _state = _ld.get("grid_connection_state")
                    _raw_grid = _ld.get("grid_connected")
                    # Type-guard both reads. Only a real str/bool is evidence
                    # about grid state; anything else (notably a Mock from a
                    # stubbed registry) is "could not determine", which must
                    # NOT flip the safety posture — otherwise every mocked
                    # gateway reads as islanded and SD stops evaluating.
                    if isinstance(_state, str) and _state.strip():
                        grid_connected = _state.strip().lower() in (
                            "connected", "on", "true", "1", "grid", "ongrid", "on-grid",
                        )
                    elif isinstance(_raw_grid, bool):
                        grid_connected = _raw_grid
                    elif _state is None and _raw_grid is None:
                        # Neither field present on a live payload — genuinely
                        # unknown, so assume islanded rather than permit GRID_*.
                        grid_connected = False
                        logger.warning(
                            f"SmartDispatch [{gateway_id}]: telemetry present but neither "
                            f"'grid_connection_state' nor 'grid_connected' available — "
                            f"assuming islanded (fail-closed)"
                        )
        except Exception as _grid_exc:
            # Structural failure reaching the registry, not a data problem —
            # log rather than swallow, but do not flip the safety posture on it.
            logger.warning(f"SmartDispatch [{gateway_id}]: grid-state check failed — {_grid_exc}")

        if not grid_connected:
            logger.info(f"SmartDispatch [{gateway_id}]: Grid disconnected — engaging Off-Grid Safety Lock (HOLD no-op)")
            return EvalDecision(
                action="HOLD",
                preset_name=None,
                rule_id="__offgrid_safety__",
                rule_name="Off-Grid Safety Lock",
                priority=1,
                reason="grid_connected=False — GRID_CHARGE/GRID_EXPORT prohibited off-grid; native mode continues",
                conditions_met=["grid_connected=False"],
                can_execute=False,  # HOLD is a no-op — no hardware command issued off-grid
                baseline_missing=False,
                trigger_category="offgrid",
                dispatch_summary="Grid disconnected — engaging autonomous Off-Grid safety mode (no grid actions permitted).",
                requires_approval=False,
            )

        # Fetch time-based schedules
        try:
            schedules = await db.get_smart_dispatch_schedules(gateway_id) if gateway_id else []
        except Exception as exc:
            logger.debug(f"SmartDispatch: schedules fetch failed — {exc}")
            schedules = []

        strategy = cfg.get("strategy_mode", "active")

        # ── Normalize modern → legacy vocabulary ─────────────────────────────
        # The DB may hold either the modern names (auto/user_approval/info/
        # disabled) or the legacy names (active/proactive/passive) depending on
        # when the row was last written. evaluate_and_log() already normalizes
        # legacy → modern at line 3068 for its own gates; this function's
        # pipeline guards were written against the LEGACY vocabulary and
        # silently skip every branch when the DB holds "auto" or "user_approval".
        # Root cause of the 14-day "Engine dormant" streak. Fixed 2026-07-08
        # as backlog P1 cluster #1. Unified vocabulary is tracked separately.
        _LEGACY_STRATEGY_MAP = {"auto": "active", "user_approval": "active", "info": "passive"}
        strategy = _LEGACY_STRATEGY_MAP.get(strategy, strategy)

        # ── Synthesize & Optimize ──
        # Phase 2.B (2026-08-05) — routed through `MesoPlanner` so the
        # optimizer can be swapped (greedy → LP) via config without
        # touching this method. The planner currently delegates to
        # `self.synthesize_and_optimize`, so behaviour is unchanged.
        opt_action = "HOLD"
        opt_soc = soc_pct or 50.0
        if strategy in ("active", "proactive"):
            try:
                from src.services.smart_dispatch.meso import meso_planner
                opt_action, opt_soc = await meso_planner.solve(self, gateway_id, snap, soc_pct, cfg)
            except Exception as opt_err:
                logger.exception(f"SmartDispatch [{gateway_id}]: Optimizer failure: {opt_err}")

        # 2. Pipeline Execution
        winner = None

        if strategy == "active":
            # A. Dynamic Strategy Matrix Mixer (Highest Precedence)
            mixer = StrategyMixer(self._gateway_registry)
            winner = await mixer.evaluate(gateway_id, snap, soc_pct or 0.0, site_snap=site_snap)

        if not winner:
            # B. Time Schedules Override
            time_res = _eval_time_schedules(ctx, cfg, baseline_ok, schedules)
            if time_res:
                winner = time_res

        if not winner and strategy == "active":
            # C. Custom Builder Automations
            custom_matches = self._eval_custom_automations(ctx, baseline_ok, gateway_id)
            if custom_matches:
                custom_matches.sort(key=lambda x: x.priority)
                winner = custom_matches[0]

        if not winner and strategy in ("active", "proactive"):
            if opt_action in ("GRID_CHARGE", "GRID_EXPORT"):
                winner = EvalDecision(
                    action=opt_action,
                    preset_name=None,
                    rule_id="dynamic_arbitrage_solver",
                    rule_name="Dynamic Arbitrage Solver",
                    priority=50,
                    reason=f"Optimal rolling 24h path calculated action: {opt_action} (target SOC: {opt_soc}%)",
                    conditions_met=[f"opt_action={opt_action}"],
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                    trigger_category="active_arbitrage",
                    dispatch_summary=f"Greedy solver optimized action: {opt_action} (target SOC: {opt_soc}%)",
                    requires_approval=False,
                    action_payload={"target_soc": opt_soc}
                )

        if not winner and strategy == "active":
            # D. Legacy Rulebook Rule
            try:
                legacy = await self.evaluate(snap, soc_pct=soc_pct, provider=provider)
                if legacy.action not in ("RESUME_TOU", "RESUME_SC"):
                    winner = EvalDecision(
                        action=legacy.action,
                        preset_name=legacy.preset_name,
                        rule_id=legacy.rule_id,
                        rule_name=legacy.rule_name,
                        priority=legacy.priority,
                        reason=legacy.reason,
                        conditions_met=legacy.conditions_met,
                        can_execute=legacy.can_execute,
                        baseline_missing=legacy.baseline_missing,
                        trigger_category="legacy_rule",
                        dispatch_summary=f"User rule: {legacy.rule_name}",
                        requires_approval=False,
                    )
            except Exception as exc:
                logger.debug(f"SmartDispatch: legacy rulebook error — {exc}")

        # E. Fallback: Native EMS Mode (e.g. SC Fallback)
        if not winner:
            winner = self._sc_fallback(baseline_ok, gateway_id)

        winner.evaluated_params = _build_evaluated_params(ctx, cfg)
        self._last_winner_rule_id[gateway_id] = winner.rule_id
        return winner

    def _eval_custom_automations(self, ctx: dict, baseline_ok: bool, gateway_id: str) -> list[EvalDecision]:
        """
        Evaluate any custom automation rules created in the builder that utilize 
        pricing/dispatch logic or smart_dispatch actions.
        Priority 25 (Between Negative Export and Price Spike).
        """
        try:
            from src.main import get_app_state
            from src.services.scheduler_core import evaluate_conditions
            state = get_app_state()
            scheduler_wrapper = state.get("scheduler")
            if not scheduler_wrapper or not hasattr(scheduler_wrapper, 'scheduler'):
                return []
                
            registry = state.get("registry")
            live_data = {}
            if registry and gateway_id:
                try:
                    gw = registry.get_gateway(gateway_id)
                    if gw and getattr(gw, "status", None) and getattr(gw.status, "last_data", None):
                        live_data = gw.status.last_data
                except Exception:
                    pass

            # Retrieve gateway configuration for dispatch variables
            try:
                # Use synchronous registry method if available, or fetch from DB manually.
                # In _eval_custom_automations, this runs inside a sync/async boundary,
                # but this method is NOT async. Wait, _eval_custom_automations is sync!
                # It is called from evaluate() which is async. 
                # But _eval_custom_automations is defined as `def _eval_custom_automations(...)`.
                import sqlite3
                from src.main import get_app_state
                db_path = get_app_state().get("db_path")
                with sqlite3.connect(db_path, timeout=30.0) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA foreign_keys=ON;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    # Fallback to global if specific doesn't exist
                    row = conn.execute("SELECT * FROM smart_dispatch_config WHERE gateway_id = ?", (gateway_id,)).fetchone()
                    if not row:
                        row = conn.execute("SELECT * FROM smart_dispatch_config WHERE gateway_id = 'global'").fetchone()
                        
                    if row:
                        global_cfg = dict(row)
                    else:
                        global_cfg = {}
            except Exception:
                global_cfg = {}
                
            site_season = ""
            fl_kw = 0.0
            try:
                import sqlite3
                from src.main import get_app_state
                from src.services.smart_dispatch import get_site_season, get_active_forecast_load_kw
                from datetime import datetime
                db_path = get_app_state().get("db_path")
                with sqlite3.connect(db_path, timeout=30.0) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA foreign_keys=ON;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    lat_row = conn.execute("SELECT value FROM app_config WHERE key = ?", (f"lat_{gateway_id}",)).fetchone()
                    if lat_row and lat_row["value"]:
                        site_season = get_site_season(float(lat_row["value"]), datetime.now().month)
                        
                    fl_rows = conn.execute("SELECT * FROM forecast_loads").fetchall()
                    if fl_rows:
                        # Convert sqlite3.Row to dict
                        fl_loads = [dict(r) for r in fl_rows]
                        # Just grab basic forecast kw without live ha_states for UI sim (too slow to fetch HA synchronously)
                        fl_kw = get_active_forecast_load_kw(
                            fl_loads,
                            datetime.now(),
                            site_season,
                            is_current_slot=True,
                            ha_states={}
                        )
            except Exception:
                pass
                
            formatted_ctx = {
                "pricing": {
                    "import_price_c_kwh": ctx.get("import_c_kwh"),
                    "export_price_c_kwh": ctx.get("export_c_kwh"),
                    "spike_status": ctx.get("spike_status"),
                    "descriptor": ctx.get("descriptor"),
                    "daily_earnings": ctx.get("daily_earnings"),
                    "monthly_earnings": ctx.get("monthly_earnings"),
                    "demand_window_active": live_data.get("demand_window", False),
                },
                "dispatch": {
                    "min_soc_limit": 0,
                    "max_soc_limit": 100,
                    "min_export_price": float(global_cfg.get("min_export_price", 0.0)),
                    "max_charge_price": float(global_cfg.get("max_charge_price", 15.0)),
                    "site_season": site_season,
                    "forecast_load_kw": fl_kw,
                    # solar_pv_active: true if Enphase is configured OR FranklinWH native solar detected
                    "solar_pv_active": (
                        ctx.get("enphase_enabled", 0) > 0
                        or live_data.get("has_solar", False)
                        or (live_data.get("solar_kw", 0) or 0) > 0
                    ),
                },
                "gateway": {
                    # Solar presence — true if any PV source is producing or configured
                    "has_solar": (
                        live_data.get("has_solar", False)
                        or ctx.get("enphase_enabled", 0) > 0
                        or (live_data.get("solar_kw", 0) or 0) > 0
                    ),
                    # Grid connectivity
                    "grid_connected": live_data.get("grid_connection_state", "").lower() in ("connected", "on", "true", "1")
                        or live_data.get("grid_connected", True),
                    # Live battery SoC (same as battery.soc but under gateway namespace)
                    "battery_soc": live_data.get("battery_soc", 0.0),
                    # Current operating mode string
                    "operating_mode": live_data.get("operating_mode", ""),
                    # Run status — hardware action (Standby, Grid Charge, Grid Export, etc.)
                    "run_status": live_data.get("run_status", ""),
                },
                "site": ctx.get("site", {})
            }

                
            matched_decisions = []
            jobs = scheduler_wrapper.scheduler.get_jobs()
            for job in jobs:
                kwargs = job.kwargs
                gw = kwargs.get("gateway_serial")
                if gw and gw != gateway_id and gw != "ALL":
                    continue
                
                if job.next_run_time is None:
                    continue # Paused
                    
                actions = kwargs.get("actions", [])
                has_delegate = any(a.get("type", "").startswith("smart_dispatch.") for a in actions)
                conds = kwargs.get("conditions", [])
                has_delegate_metric = any(c.get("metric", "").startswith("pricing.") or c.get("metric", "").startswith("dispatch.") for c in conds)

                if not (has_delegate or has_delegate_metric):
                    continue
                    
                condition_logic = kwargs.get("condition_logic", "AND")
                matched, failed, passed = evaluate_conditions(live_data, condition_logic, conds, extra_context=formatted_ctx)
                if matched:
                    payload = {}
                    sd_action = "NONE"
                    for act in actions:
                        act_type = act.get("type", "")
                        if not act_type.startswith("smart_dispatch."):
                            continue
                            
                        if act_type == "smart_dispatch.force_charge":
                            sd_action = "GRID_CHARGE"
                        elif act_type == "smart_dispatch.force_discharge":
                            sd_action = "GRID_EXPORT"
                        elif act_type == "smart_dispatch.curtail_solar":
                            sd_action = "ENPHASE_CURTAIL"
                        elif act_type == "smart_dispatch.hold":
                            sd_action = "NONE"
                            
                        payload = act.get("payload", {})
                        
                        # Pack custom parameters into dispatch_summary for extraction downstream
                        # Ideally EvalDecision would have strong typing for this, but dispatch_summary or action can wrap it
                        # Let's pack it into a special dispatch_summary prefix for now, or just reason.
                        # Wait, we need to extract this in Phase 4 or cloud_dispatch!
                        
                        matched_decisions.append(EvalDecision(
                            action=sd_action,
                            preset_name=None,
                            rule_id=job.id,
                            rule_name=f"{job.name} (Custom Rule)",
                            priority=kwargs.get("priority", 25),
                            reason=", ".join(passed) if passed else str(payload), 
                            conditions_met=passed,
                            can_execute=baseline_ok,
                            baseline_missing=not baseline_ok,
                            trigger_category="automation",
                            dispatch_summary=f"Custom Rule Matched: {job.name} triggered {sd_action}",
                            requires_approval=False,
                            action_payload=payload
                        ))
            return matched_decisions
        except Exception as e:
            import traceback
            logger.error(f"SmartDispatch: _eval_custom_automations error - {e}\n{traceback.format_exc()}")
            return []

    # ── Core evaluation ──────────────────────────────────────────────────────

    async def evaluate(
        self,
        snap: PriceSnapshot,
        soc_pct: Optional[float] = None,
        provider: Optional[str] = None,
        full_serial: Optional[str] = None,
    ) -> RuleResult:
        """
        Evaluate the active rulebook against the current snapshot + SoC.
        Returns the highest-priority matching RuleResult.
        
        Phase 2: StrategyMixer (Matrix-driven) takes precedence if serial provided.
        """
        # 1. Try Matrix-driven Mixer (Phase 2)
        if full_serial:
            mixer = StrategyMixer(self._gateway_registry)
            matrix_result = await mixer.evaluate(full_serial, snap, soc_pct or 0.0)
            if matrix_result:
                return matrix_result

        # 2. Baseline & Context
        dna = await db.get_gateway_by_full_serial(full_serial) if full_serial else None
        try:
            cfg = await db.get_smart_dispatch_config(full_serial) if full_serial else {}
        except Exception:
            cfg = {}
        
        site_snap = self._gateway_registry.get_site_snapshot() if self._gateway_registry else None
        ctx = await _build_context(snap, soc_pct, dna, cfg, site_snap=site_snap)
        baseline_ok = self._baseline_exists()

        # 3. Fallback to Legacy Rulebook (Phase 0/1)
        rules = await db.get_active_rules(provider or snap.provider)
        if not rules:
            logger.debug("SmartDispatch: no active rules — returning default RESUME_TOU")
            return self._default_result(baseline_ok)

        for rule in rules:  # already ordered by priority ASC
            rule_id   = rule["rule_id"]
            rule_name = rule["name"]
            cond_json = rule.get("condition_json", "{}")

            # Provider scope filter
            p_scope = rule.get("provider_scope")
            if p_scope and p_scope != snap.provider:
                logger.debug(f"SmartDispatch: rule '{rule_name}' skipped (provider_scope={p_scope})")
                continue

            matched, descriptions = evaluate_condition(cond_json, ctx)

            if matched:
                action = rule["action"]
                params = {}
                try:
                    params = json.loads(rule.get("action_params") or "{}")
                except Exception:
                    pass

                preset_name = params.get("preset_name") if action == "APPLY_PRESET" else None
                if preset_name:
                    # Map legacy Amber presets to new generic Smart presets for backward compatibility
                    preset_mapping = {
                        "Amber Default": "Smart Default",
                        "Amber Force Charge": "Smart Force Charge",
                        "Amber Peak Discharge": "Smart Peak Discharge",
                        "Amber Solar Sponge": "Smart Solar Sponge",
                        "Amber Spike Hold": "Smart Spike Hold",
                    }
                    preset_name = preset_mapping.get(preset_name, preset_name)

                reason = (
                    f"Rule '{rule_name}' (priority {rule['priority']}): "
                    + ", ".join(descriptions)
                )
                logger.debug(f"SmartDispatch: matched rule '{rule_name}' → {action}"
                             + (f" preset='{preset_name}'" if preset_name else ""))

                return RuleResult(
                    action=action,
                    preset_name=preset_name,
                    rule_id=rule_id,
                    rule_name=rule_name,
                    priority=rule["priority"],
                    reason=reason,
                    conditions_met=descriptions,
                    confidence=1.0,
                    can_execute=baseline_ok,
                    baseline_missing=not baseline_ok,
                )

        # No rule matched — return default
        return self._default_result(baseline_ok)

    def _default_result(self, baseline_ok: bool = False) -> RuleResult:
        """No-rule-matched fallback. Returns NONE (no-op) — native gateway mode stays in place."""
        return RuleResult(
            action="NONE",
            preset_name=None,
            rule_id="__default__",
            rule_name="No Override (Catch-All)",
            priority=9999,
            reason="No active price signal — no override issued, gateway operates in native mode.",
            conditions_met=["(no rules matched)"],
            confidence=1.0,
            can_execute=baseline_ok,
            baseline_missing=not baseline_ok,
        )

    def _is_vpp_active(self, gateway_serial: str) -> tuple[bool, str]:
        """Return (True, reason) if the gateway is currently under VPP or
        Modbus external control, else (False, "").

        Rationale: when VPP Mode is active, the FranklinWH gateway is
        controlled by the utility/aggregator via cloud commands (typically
        Modbus over MQTT). Any HEMS notification asking the user to approve
        an action like GRID_EXPORT / GRID_CHARGE is futile — HEMS cannot
        dispatch while VPP owns the gateway. Firing the notification anyway
        erodes user trust ("why is it asking me to do something it can't
        do?"). Suppressed as SUPPRESSED audit entries so the user still
        sees the engine's intent in the audit log.

        Detection uses the same canonical flags as the frontend
        (src/static/js/app.js:209): runtime_mode == "VPP Mode" (set by
        gateway_service._reconcile_mode) OR run_status_desc contains "vpp"
        (case-insensitive). Both are exposed via
        gateway_registry.get_gateway(gid).status.last_data.
        """
        try:
            reg = getattr(self, "_gateway_registry", None)
            if not reg or not gateway_serial:
                return (False, "")
            gw = reg.get_gateway(gateway_serial)
            if not gw or not getattr(gw, "status", None) or not getattr(gw.status, "last_data", None):
                return (False, "")
            ld = gw.status.last_data
            runtime_mode = (ld.get("mode") or {}).get("runtime_mode", "") or ""
            run_status_desc = ld.get("run_status_desc") or ld.get("run_status_dec") or ""
            if str(runtime_mode).lower() == "vpp mode":
                return (True, f"runtime_mode={runtime_mode!r}")
            if "vpp" in str(run_status_desc).lower():
                return (True, f"run_status_desc={run_status_desc!r}")
            return (False, "")
        except Exception:
            # Fail-open: if the state check itself errors, don't accidentally
            # gag the entire notification stream — let the caller proceed.
            return (False, "")

    def _is_action_stale(
        self,
        result: "EvalDecision",
        gateway_serial: str,
        cfg: dict,
    ) -> tuple[bool, str]:
        """Return (True, reason) if the recommended action would be a no-op
        against the CURRENT live battery state — e.g. GRID_CHARGE while the
        battery is already at max SoC or already actively charging from
        solar/grid. Batch J (backlog f61d068, 2026-07-11).

        Applied AFTER Batch G's VPP gate — VPP suppression takes precedence
        in the audit trail (VPP is a stronger reason than stale-state; if
        both apply the user should see "VPP override" first).

        Signal-only actions (HOLD/STANDBY/PAUSED/NONE) always return False
        — they represent "do nothing / release control", which is never a
        no-op relative to current state.

        Fail-open on any error (missing registry, exception in state read)
        — better a spurious notification than accidental total silence.

        Sign convention (confirmed prod data 2026-07-14):
            battery_kw < 0  → charging (energy flowing INTO battery)
            battery_kw > 0  → discharging (energy flowing OUT of battery)
            grid_kw     < 0 → exporting to grid
            grid_kw     > 0 → importing from grid
        """
        action = (result.action or "").strip()
        # Signal-only actions — always emit
        if action in ("HOLD", "STANDBY", "PAUSED", "NONE", ""):
            return (False, "")

        # Read live state (same pattern as _is_vpp_active)
        try:
            reg = getattr(self, "_gateway_registry", None)
            if not reg or not gateway_serial:
                return (False, "")
            gw = reg.get_gateway(gateway_serial)
            if not gw or not getattr(gw, "status", None):
                return (False, "")
            ld = getattr(gw.status, "last_data", None) or {}
            soc = float(ld.get("battery_soc") or 0.0)
            bkw = float(ld.get("battery_kw") or 0.0)
            gkw = float(ld.get("grid_kw") or 0.0)
        except Exception:
            return (False, "")

        # Thresholds from smart_dispatch_config with sane fallbacks
        try:
            max_soc = float((cfg or {}).get("max_soc", 90.0) or 90.0)
        except (TypeError, ValueError):
            max_soc = 90.0
        try:
            min_soc = float((cfg or {}).get("min_soc", 20.0) or 20.0)
        except (TypeError, ValueError):
            min_soc = 20.0
        # 200 W dead-band — smaller flows are noise (idle self-consumption)
        _FLOW_THRESHOLD_KW = 0.200

        action_type = action.split(":")[0].upper()

        if action_type in ("GRID_CHARGE", "SOLAR_CHARGE"):
            if soc >= max_soc:
                return (True, f"battery at/above max SoC ({soc:.1f}% ≥ {max_soc:.0f}%)")
            if bkw < -_FLOW_THRESHOLD_KW:
                return (True, f"battery already charging ({bkw:+.2f} kW)")
        elif action_type in ("GRID_EXPORT", "FORCE_DISCHARGE"):
            if soc <= min_soc:
                return (True, f"battery at/below min SoC ({soc:.1f}% ≤ {min_soc:.0f}%)")
            if gkw <= -_FLOW_THRESHOLD_KW:
                return (True, f"already exporting to grid ({gkw:+.2f} kW)")

        return (False, "")

    def _is_event_stale(
        self,
        ev_key: str,
        gateway_serial: str,
        cfg: dict,
    ) -> tuple[bool, str]:
        """Return (True, reason) if the NOTIFICATION EVENT (not the raw
        action) would be misleading given current live battery state.

        Batch M-1 (2026-07-25 audit): Batch J's `_is_action_stale` only
        gates when `result.action` itself is GRID_CHARGE / GRID_EXPORT
        / etc. But the audit showed the SPAMMY notifications are
        `event=force_charge` / `event=export_bonus` firing while the
        underlying action is HOLD (from rules like "Negative Price
        Lockout" or "Max Price — Cease Charging" whose trigger_category
        routes them into charge/export event channels). The user sees
        "Force Charge" push while battery is already 100% full, which
        the per-action gate can't catch.

        Semantics:
          force_charge / spike   → stale when battery already charged
                                    (soc >= max_soc) OR already charging
                                    (battery_kw < -200W)
          export_bonus /
          negative_export        → stale when battery empty (soc <= min_soc)
                                    OR already exporting (grid_kw <= -200W)
          demand_charge / earnings_target / signal_change / custom /
          time_schedule / solar_optimization → never stale (informational
                                                only; user still wants
                                                to see them)

        Signal-level check that overlays the per-action check. Both
        gates can fire; the more specific reason wins in the audit log.
        """
        ev = (ev_key or "").strip().lower()
        # Only these event types have "current state contradicts intent" semantics
        if ev not in ("force_charge", "spike", "export_bonus", "negative_export"):
            return (False, "")

        try:
            reg = getattr(self, "_gateway_registry", None)
            if not reg or not gateway_serial:
                return (False, "")
            gw = reg.get_gateway(gateway_serial)
            if not gw or not getattr(gw, "status", None):
                return (False, "")
            ld = getattr(gw.status, "last_data", None) or {}
            soc = float(ld.get("battery_soc") or 0.0)
            bkw = float(ld.get("battery_kw") or 0.0)
            gkw = float(ld.get("grid_kw") or 0.0)
        except Exception:
            return (False, "")

        try:
            max_soc = float((cfg or {}).get("max_soc", 90.0) or 90.0)
        except (TypeError, ValueError):
            max_soc = 90.0
        try:
            min_soc = float((cfg or {}).get("min_soc", 20.0) or 20.0)
        except (TypeError, ValueError):
            min_soc = 20.0
        _FLOW_THRESHOLD_KW = 0.200

        if ev in ("force_charge", "spike"):
            if soc >= max_soc:
                return (True, f"event={ev!r} but battery at/above max ({soc:.1f}% ≥ {max_soc:.0f}%)")
            if bkw < -_FLOW_THRESHOLD_KW:
                return (True, f"event={ev!r} but battery already charging ({bkw:+.2f} kW)")
            # Batch O.2 (2026-07-29): asking to charge while grid is
            # actively being fed makes no sense — user would have to
            # reverse flow mid-cycle. Complement to the battery-flow
            # gate: catches "system currently exporting; prompt to
            # charge from grid" contradictions.
            if gkw < -_FLOW_THRESHOLD_KW:
                return (True, f"event={ev!r} but currently exporting to grid ({gkw:+.2f} kW)")
        else:  # export_bonus / negative_export
            if soc <= min_soc:
                return (True, f"event={ev!r} but battery at/below min ({soc:.1f}% ≤ {min_soc:.0f}%)")
            if gkw <= -_FLOW_THRESHOLD_KW:
                return (True, f"event={ev!r} but already exporting ({gkw:+.2f} kW)")
            # Batch O.2 (2026-07-29) — actual failure mode surfaced in
            # the audit: 7 of 11 export_bonus alerts fired while
            # battery_kw was < -200W (actively CHARGING). Grid-flow was
            # ~0 in those rows so the exporting-check above didn't
            # catch them; battery-flow tells the truth. You can't
            # simultaneously charge AND export.
            if bkw < -_FLOW_THRESHOLD_KW:
                return (True, f"event={ev!r} but battery currently charging ({bkw:+.2f} kW) — can't export mid-charge")

        return (False, "")

    def _sc_fallback(self, baseline_ok: bool = False, gateway_id: str = "") -> EvalDecision:
        """
        Graceful Baseline Mode Fallback.
        Instead of aggressively forcing a 'Self-Consumption' preset which destroys local TOU schedules,
        the Engine explicitly releases control, allowing the battery's native mode to run.
        """
        raw_mode = "Unknown"
        mode_id = None
        try:
            reg = self._gateway_registry
            if reg and gateway_id:
                gw = reg.get_gateway(gateway_id)
                if gw and gw.status and gw.status.last_data:
                    ld = gw.status.last_data
                    raw_mode = ld.get("operating_mode") or ld.get("mode", {}).get("work_mode_desc") or raw_mode
                    mode_id = ld.get("operating_mode_id") or ld.get("work_mode") or ld.get("mode", {}).get("work_mode")
        except Exception:
            pass
            
        # Robust mapping dictionary
        LABELS = {
            1: "Time-of-Use",
            2: "Self-Consumption",
            3: "Emergency Backup",
            4: "Off Grid",
            "1": "Time-of-Use",
            "2": "Self-Consumption",
            "3": "Emergency Backup",
            "4": "Off Grid",
            "Time of Use": "Time-of-Use",
            "Time-of-Use": "Time-of-Use",
            "Self-Consumption": "Self-Consumption",
            "Emergency Backup": "Emergency Backup",
            "Backup": "Emergency Backup",
            "Off Grid": "Off Grid",
            "Off-Grid": "Off Grid",
        }

        # Resolve mode ID first
        display_mode = None
        if mode_id is not None and mode_id in LABELS:
            display_mode = LABELS[mode_id]
        
        # Resolve raw mode string if ID resolution failed or wasn't in labels
        if not display_mode and raw_mode in LABELS:
            display_mode = LABELS[raw_mode]

        # Final fallback
        if not display_mode:
            display_mode = raw_mode if raw_mode else "Unknown"

        return EvalDecision(
            action="NONE",
            preset_name=None,
            rule_id="__baseline_fallback__",
            rule_name="Current Operating Mode",
            priority=9999,
            reason="No pricing events active",
            conditions_met=["fallback=true"],
            can_execute=baseline_ok,
            baseline_missing=False,
            trigger_category="fallback",
            dispatch_summary=f"Engine dormant — returning control to native gateway mode ({display_mode}).",
            requires_approval=False,
        )

    # ── Evaluate + log on state change ───────────────────────────────────────

    async def evaluate_and_log(
        self,
        snap: PriceSnapshot,
        utility_service_id: str = None,
        soc_pct: Optional[float] = None,
        gateway_serial: str = "",
        provider: Optional[str] = None,
    ) -> EvalDecision:
        """
        Run evaluate_rules() and write to pricing_eval_log + automation_history when
        the action changes from the previous cycle. De-duplication avoids log spam.
        """
        # Capture previous action BEFORE evaluation updates the state
        prev_action_snapshot = self._last_action.get(gateway_serial)

        # ── SoC=0 stale-window sentinel skip (2026-08-06) ───────────────────────
        # Batch I's `_normalise_stats` zero-collapse detector catches most
        # stale-window cloud poll returns, but its all-zeros signature
        # (soc==0 AND battery_kw==0 AND grid_kw==0 AND run_status==Standby)
        # is strict — any one non-zero field lets the stale row through,
        # `last_data` gets overwritten with soc=0, and the SD engine
        # evaluates on that garbage. Real-world impact (2026-08-06 audit):
        # 2 of 4 SENT notifications yesterday+today included "SoC: 0%" in
        # the payload while the battery was actually ~97%.
        #
        # Defence in depth: if soc_pct is exactly 0.0 AND the gateway's
        # last-known-good SoC (tracked in gateway_service.context) was
        # ≥5%, the 0 is almost certainly a stale sentinel. SoC does not
        # drop 97% → 0% in a 30s tick. Skip the evaluation entirely —
        # no pricing_eval_log write, no notification, no state churn.
        try:
            if soc_pct == 0.0 and self._gateway_registry and gateway_serial:
                gw_svc = self._gateway_registry.get_gateway(gateway_serial)
                _last_good = float((gw_svc.context or {}).get("_last_good_soc", 0.0) or 0.0) if gw_svc else 0.0
                if _last_good >= 5.0:
                    logger.debug(
                        f"[{gateway_serial}] SmartDispatch: skipping tick — soc=0 sentinel "
                        f"(last known good soc={_last_good:.1f}%); Batch I stale-window leak"
                    )
                    return self._default_result()
        except Exception:
            pass

        # ── System Orchestrator: Check for Manual Override Lock ──────────────────
        if self._gateway_registry and gateway_serial:
            lock = self._gateway_registry.get_exclusive_lock(gateway_serial)
            if lock and lock.get("type") == "manual_dispatch":
                logger.info(f"[{gateway_serial}] SmartDispatch: Manual override active ({lock['value']}) — suppressing automation.")
                # Ensure sd_signal is NONE so Automation Builder releases control
                await db.set_sd_signal(gateway_serial, "NONE", "manual_override")
                return EvalDecision(
                    action="HOLD", preset_name=None, rule_id="__manual_override__",
                    rule_name="Manual Override", priority=0,
                    reason=f"Manual dispatch ({lock['value']}) is currently active.",
                    conditions_met=["(manual override lock)"],
                    can_execute=False, baseline_missing=False,
                    trigger_category="manual_override",
                    dispatch_summary=f"Automation suppressed: manual {lock['value']} in progress.",
                    requires_approval=False,
                )

        # ── Hard gates: disabled + paused must short-circuit BEFORE any evaluation ──
        # Fetch strategy first so we never run evaluate_rules() when SD is off
        try:
            _early_cfg = await db.get_smart_dispatch_config(gateway_serial) if gateway_serial else {}
        except Exception:
            _early_cfg = {}
        _raw_strategy_early = _early_cfg.get("strategy_mode", "auto")
        _notif_mode_early = _early_cfg.get("notification_mode", "ask")
        
        # If notification mode is 'ask', we must route through user_approval to send notifications
        if _raw_strategy_early != "disabled" and _notif_mode_early == "ask":
            strategy = "user_approval"
        else:
            _strategy_map_early = {"passive": "info", "active": "auto", "proactive": "auto"}
            strategy = _strategy_map_early.get(_raw_strategy_early, _raw_strategy_early)

        # ── Stale telemetry gate (GH #35) ────────────────────────────────
        # SD issues real hardware commands off gw.status.last_data. FHAI now
        # deliberately RETAINS last-known-good when the cloud returns a
        # degraded payload (the Batch I stale-window drop), which is correct
        # for display but means last_data can be arbitrarily old. Acting on it
        # can mean charging an already-full battery or discharging past the
        # reserve, because the SoC we are reasoning about is frozen.
        #
        # Gate on last_data_age_s, NOT last_poll_age_s: the drop paths update
        # last_poll_at even when they publish nothing, so poll age stays small
        # while the data ages. Only a real write moves last_data_at.
        _max_age = _DEFAULT_MAX_TELEMETRY_AGE_S
        try:
            _cfg_age = int(_early_cfg.get("max_telemetry_age_s") or 0)
            if _cfg_age > 0:
                _max_age = _cfg_age
        except (TypeError, ValueError):
            pass
        _data_age = None
        try:
            _reg = self._gateway_registry
            _gw = _reg.get_gateway(gateway_serial) if _reg else None
            if _gw and getattr(_gw, "status", None):
                _data_age = getattr(_gw.status, "last_data_age_s", None)
        except Exception:
            _data_age = None
        # isinstance check keeps mocked/absent registries (and never-populated
        # gateways, where age is None) on the existing code path.
        if isinstance(_data_age, (int, float)) and _data_age > _max_age:
            _mins = int(_data_age // 60)
            logger.warning(
                f"[{gateway_serial}] SmartDispatch: telemetry {_data_age}s old "
                f"(limit {_max_age}s) — holding, not evaluating (GH #35)"
            )
            return EvalDecision(
                action="HOLD", preset_name=None, rule_id="__stale_telemetry__",
                rule_name="Stale Telemetry", priority=0,
                reason=(f"Telemetry is {_data_age}s old (limit {_max_age}s) — refusing to "
                        f"dispatch on data this stale."),
                conditions_met=[f"last_data_age_s={_data_age}"],
                can_execute=False, baseline_missing=False,
                trigger_category="stale_telemetry",
                dispatch_summary=(
                    f"Paused — no fresh gateway data for {_mins} min. "
                    f"Not dispatching until telemetry recovers."
                ),
                requires_approval=False,
            )

        if strategy == "disabled":
            logger.debug(f"[{gateway_serial}] SmartDispatch: strategy=disabled — skipping all evaluation (no rules run)")
            return EvalDecision(
                action="HOLD", preset_name=None, rule_id="__disabled__",
                rule_name="Dispatch Disabled", priority=9999,
                reason="Smart Dispatch is disabled — no rules evaluated, no signals written.",
                conditions_met=["(dispatch disabled)"],
                can_execute=False, baseline_missing=False,
                trigger_category="disabled",
                dispatch_summary="Smart Dispatch disabled — gateway runs in native mode.",
                requires_approval=False,
            )

        # Engine mode gate
        try:
            engine_mode = await db.get_engine_mode()
        except Exception:
            engine_mode = "signal_only"
            
        # Global engine_mode paused/signal_only always overrides to info
        if engine_mode != "active" and strategy != "disabled":
            strategy = "info"

        if engine_mode == "paused":
            paused = EvalDecision(
                action="PAUSED", preset_name=None, rule_id="__paused__",
                rule_name="Engine Paused", priority=0,
                reason="Smart Dispatch engine is manually paused.",
                conditions_met=["(engine paused)"],
                can_execute=False, baseline_missing=False,
                trigger_category="paused",
                dispatch_summary="Engine is paused — no dispatch actions.",
            )
            await db.log_pricing_eval(
                gateway_serial, "paused", "PAUSED",
                rule_name="Engine Paused",
                reason=paused.reason,
                dispatch_summary=paused.dispatch_summary,
                execution_status="signal",
                import_c_kwh=snap.import_c_kwh,
                export_c_kwh=snap.export_c_kwh,
                soc_pct=soc_pct,
                spike_status=snap.spike_status,
                demand_window=snap.demand_window,
            )
            return paused

        # Run the 6-rule chain
        result = await self.evaluate_rules(
            snap, soc_pct=soc_pct, gateway_id=gateway_serial, provider=provider
        )

        # Phase C: look-ahead window — pre-act on upcoming cheap pricing intervals
        # Only runs when: result is passive + lookahead_minutes > 0 + forecast data available.
        try:
            _la_cfg = await db.get_smart_dispatch_config(gateway_serial) if gateway_serial else {}
            _lookahead = int(_la_cfg.get("lookahead_minutes", 0))
            if (
                _lookahead > 0
                and result.action == "HOLD"
                and hasattr(snap, "forecast")
            ):
                from datetime import datetime, timezone as _tz
                _now = datetime.now(tz=_tz.utc)
                _max_charge_price = float(_la_cfg.get("max_charge_price", 15.0))
                for _period in sorted(snap.forecast, key=lambda p: getattr(p, "start", _now)):
                    if getattr(_period, "is_current", False):
                        continue
                    _pstart = getattr(_period, "start", None)
                    if not _pstart:
                        continue
                    if not hasattr(_pstart, "utcoffset") or _pstart.utcoffset() is None:
                        _pstart = _pstart.replace(tzinfo=_tz.utc)
                    _mins_away = (_pstart - _now).total_seconds() / 60.0
                    if _mins_away < 0 or _mins_away > _lookahead:
                        continue
                    _future_price = getattr(_period, "per_kwh", None) or getattr(_period, "import_c_kwh", None)
                    if _future_price is not None and float(_future_price) <= _max_charge_price:
                        result = EvalDecision(
                            action="GRID_CHARGE",
                            preset_name=None,
                            rule_id="__lookahead_charge__",
                            rule_name=f"Look-ahead: cheap window in {int(_mins_away)}min",
                            priority=3,
                            reason=(
                                f"Pre-charging: {_future_price:.2f}c/kWh window starts in "
                                f"{int(_mins_away)}min (look-ahead={_lookahead}min)"
                            ),
                            conditions_met=[f"upcoming_import={_future_price:.2f}c"],
                            can_execute=True,
                            baseline_missing=False,
                            trigger_category="lookahead_charge",
                            dispatch_summary=(
                                f"Pre-charging: cheap price {_future_price:.2f}c/kWh "
                                f"starts in {int(_mins_away)} minutes"
                            ),
                            requires_approval=False,
                        )
                        logger.info(
                            f"SmartDispatch [LOOKAHEAD] [{gateway_serial}] "
                            f"→ GRID_CHARGE pre-act: {_future_price:.2f}c in {int(_mins_away)}min"
                        )
                        break
        except Exception as _la_exc:
            logger.debug(f"SmartDispatch: look-ahead check skipped — {_la_exc}")

        # ── Intent Locking (Conflict Resolution) ──────────────────────────────
        active_intent = intent_manager.get_active_intent(gateway_serial)
        if active_intent and result.priority >= active_intent.priority:
            if result.action != active_intent.action:
                logger.info(
                    f"SmartDispatch [{gateway_serial}]: lock active ({active_intent.action}, P{active_intent.priority}). "
                    f"Skipping {result.action} (P{result.priority}) from {result.rule_name}."
                )
                # Mirror the active intent to keep signals stable
                result = EvalDecision(
                    action=active_intent.action,
                    preset_name=active_intent.payload.get("preset"),
                    rule_id=active_intent.rule_id,
                    rule_name=f"Locked: {active_intent.action} (P{active_intent.priority})",
                    priority=active_intent.priority,
                    reason=f"Active dispatch intent lock (Priority {active_intent.priority} vs {result.priority})",
                    trigger_category="locked",
                    dispatch_summary=f"Executing locked intent: {active_intent.action} until {time.strftime('%H:%M', time.localtime(active_intent.expires_at))}",
                    action_payload=active_intent.payload
                )

        # SHADOW MODE check (moved up)
        _is_shadow = bool(_early_cfg.get("shadow_mode", 0))

        # Request new intent (or refresh existing)
        # Skip intent locking if in shadow mode to avoid blocking actual automation
        granted = True
        if not _is_shadow:
            granted = intent_manager.request_intent(
                gateway_serial,
                result.action,
                result.rule_id,
                result.priority,
                duration_mins=result.action_payload.get("duration_mins", 5),
                payload=result.action_payload
            )
        else:
            logger.debug(f"SmartDispatch [{gateway_serial}]: Shadow Mode — skipping intent lock for {result.action}")
        
        # Phase 1: persist sd_signal every tick (unconditional — AB must always read current value)

        # Enrich the payload with SD-calculated parameters (power_kw, duration_mins, target_soc)
        # so Automation Builder rules can inherit dynamic values rather than using static fields.
        if gateway_serial:
            try:
                _sd_payload: dict = {}
                if result.action in ("GRID_CHARGE", "GRID_EXPORT"):
                    # Fetch gateway live data for power calculation
                    _last_data: dict = {}
                    _gw_cfg: dict = {}
                    try:
                        _gw_cfg = await db.get_smart_dispatch_config(gateway_serial)
                        reg = self._gateway_registry
                        if reg:
                            _gw = reg.get_gateway(gateway_serial)
                            if _gw and getattr(_gw, "status", None):
                                _last_data = getattr(_gw.status, "last_data", None) or {}
                    except Exception as _fetch_exc:
                        logger.debug(f"SmartDispatch: payload fetch failed — {_fetch_exc}")
                    _sd_payload = _calc_dispatch_payload(
                        result.action, snap, _last_data, _gw_cfg
                    )
                    if getattr(result, "action_payload", None):
                        for pk, pv in result.action_payload.items():
                            if pk != "_shadowed_rules" and pv is not None:
                                _sd_payload[pk] = pv
                    if _sd_payload:
                        logger.debug(
                            f"SmartDispatch: sd_signal payload [{gateway_serial}] "
                            f"{result.action} → {_sd_payload}"
                        )
                # DO NOT emit actionable signals to AB if we are in INFO mode!
                _emit_action = result.action if strategy not in ("info", "disabled") else "NONE"
                
                # SHADOW MODE ENFORCEMENT
                if _is_shadow and _emit_action != "NONE":
                    logger.info(f"SmartDispatch [{gateway_serial}]: Shadow Mode active — suppressed execution of {_emit_action}")
                    _emit_action = "NONE"
                    result.dispatch_summary = f"[SHADOW] {result.dispatch_summary}"
                
                await db.set_sd_signal(
                    gateway_serial,
                    _emit_action,
                    getattr(result, "trigger_category", "") or "",
                    payload=_sd_payload or None,
                )
            except Exception as _exc:
                logger.debug(f"SmartDispatch: sd_signal write failed — {_exc}")


        # ── Disabled: skip entirely — no evaluation, no signal, no rules ────
        if strategy == "disabled":
            logger.debug(f"[{gateway_serial}] SmartDispatch: disabled — skipping evaluation")
            return result

        prev_action = prev_action_snapshot
        action_key  = f"{result.action}:{result.preset_name or ''}"

        # ── Batch P (2026-07-30): SD ownership deference ──────────────────────
        # If a higher-priority controller (VPP, Modbus Bridge, or in future
        # Manual Dispatch) currently owns the gateway, SD should NOT compete.
        # We still EVALUATE rules — the audit trail benefits from knowing
        # what SD would have wanted — but we don't:
        #   - update self._last_action  (else the next post-release tick
        #                                misses the state change)
        #   - bridge via AutomationEngine.execute_sd_signal_list
        #     (else two writers race on the same aGate registers)
        # pricing_eval_log gets a shadow_reason column identifying WHY the
        # decision wasn't acted on. Downstream Diagnostics + CLI use this.
        _vpp_active_early, _vpp_reason_early = self._is_vpp_active(gateway_serial)
        _shadow_reason = "vpp_active" if _vpp_active_early else None

        # ── Liveness heartbeat — ALWAYS stamped regardless of dedup (GH #36) ──
        # Must sit here, beside the mandatory log, not next to the
        # pricing_eval_log write below. That write is gated on
        # `prev_action != action_key`, so a healthy engine returning a stable
        # decision writes nothing — which is precisely how scheduler_liveness
        # came to read a working engine as stalled and rebuild the scheduler
        # 733 times, cancelling in-flight jobs each time.
        try:
            from src.services import sd_heartbeat
            sd_heartbeat.mark_tick()
        except Exception:
            pass  # a heartbeat that can fail would masquerade as a stall

        # ── Mandatory per-tick log — ALWAYS emitted regardless of dedup ──────
        _soc_str = f"{soc_pct:.0f}%" if soc_pct is not None else "—"
        _same_as_prev = (prev_action == action_key)
        if _is_shadow:
            _log_status = "SHADOW"
        elif _vpp_active_early:
            _log_status = "VPP-SHADOW"
        else:
            _log_status = "TICK"
        logger.info(
            f"SmartDispatch [{_log_status}] [{gateway_serial or '?'}] "
            f"strategy={strategy} "
            f"action={result.action} rule='{result.rule_name}' "
            f"soc={_soc_str} import={snap.import_c_kwh or 0.0:.2f}¢ export={snap.export_c_kwh or 0.0:.2f}¢ "
            f"spike={snap.spike_status} ({'same as prev — dedup' if _same_as_prev else 'action changed'})"
            + (f" [SHADOW: {_shadow_reason}]" if _shadow_reason else "")
        )

        if prev_action != action_key:
            # Batch P: don't poison _last_action during external control.
            # Post-release, the first real SD tick should see the ACTUAL
            # prior state (whatever SD last decided BEFORE external takeover),
            # not the phantom decision we made mid-VPP.
            if not _vpp_active_early:
                self._last_action[gateway_serial] = action_key

            # Write to pricing_eval_log (primary transparency log)
            try:
                import json as _json
                shadowed_json = '[]'
                if getattr(result, "action_payload", None) and "_shadowed_rules" in result.action_payload:
                    shadowed_json = _json.dumps(result.action_payload["_shadowed_rules"])

                # Batch P: execution_status becomes "shadow" for VPP deferral
                # so consumers filtering on it can distinguish "did-not-fire"
                # from "signal-only-not-acted-on".
                if _is_shadow:
                    _exec_status = "shadow"
                elif _vpp_active_early:
                    _exec_status = "shadow"
                else:
                    _exec_status = "signal"

                await db.log_pricing_eval(
                    gateway_serial,
                    result.trigger_category,
                    result.action,
                    preset_name=result.preset_name,
                    rule_name=result.rule_name,
                    reason=result.reason,
                    dispatch_summary=result.dispatch_summary,
                    requires_approval=result.requires_approval,
                    execution_status=_exec_status,
                    import_c_kwh=snap.import_c_kwh,
                    export_c_kwh=snap.export_c_kwh,
                    soc_pct=soc_pct,
                    spike_status=snap.spike_status,
                    demand_window=snap.demand_window,
                    shadowed_rules_json=shadowed_json,
                    shadow_reason=_shadow_reason,
                )
            except Exception as exc:
                logger.warning(f"SmartDispatch: pricing_eval_log write failed — {exc}")

            # Write to legacy automation_history for backward compat
            try:
                await db.log_automation_trigger(
                    rule_id=result.rule_id,
                    rule_name=result.rule_name,
                    gateway_serial=gateway_serial,
                    action_type=result.action,
                    status="signal",
                    detail=result.reason,
                    action_payload=json.dumps({
                        "preset_name":       result.preset_name,
                        "trigger_category":  result.trigger_category,
                        "dispatch_summary":  result.dispatch_summary,
                        "can_execute":       result.can_execute,
                        "conditions_met":    result.conditions_met,
                    }),
                    source="automation_builder" if result.trigger_category == "automation" else "amber",
                )
                logger.info(
                    f"[{gateway_serial}] SmartDispatch: {prev_action!r} → {action_key!r} "
                    f"| rule='{result.rule_name}' category={result.trigger_category}"
                )
            except Exception as exc:
                logger.warning(f"SmartDispatch: automation_history write failed — {exc}")

            # ── Phase 1 AB Bridge: auto triggers immediate execution ─────────
            # Batch P (2026-07-30) — deference gate. During external control
            # (VPP / Modbus), do NOT bridge to execute_sd_signal_list even
            # under strategy=auto: two writers racing on the same aGate
            # registers would produce unpredictable behaviour. The SUPPRESSED
            # audit entry from Batch G's notification hook is enough visibility;
            # the log line above already prints [VPP-SHADOW]. This is the
            # critical bug fix P.medium closes.
            if _vpp_active_early:
                logger.debug(
                    f"SmartDispatch [{gateway_serial}]: AB Bridge skipped — "
                    f"VPP owns the gateway ({_vpp_reason_early})"
                )
            elif strategy == "auto" and result.can_execute:
                from src.main import get_app_state
                automation_engine = get_app_state().get("scheduler")
                if automation_engine:
                    if result.action in ("HOLD", "STANDBY", "PAUSED", "NONE"):
                        # CLEAR OVERRIDE: If we were in an active dispatch and now move to NONE/HOLD,
                        # we should explicitly tell AB to resume native mode.
                        # Splitting prev_action safely
                        prev_act_type = prev_action.split(":")[0] if prev_action else ""
                        if prev_act_type in ("GRID_CHARGE", "GRID_EXPORT"):
                            logger.info(f"SmartDispatch [{gateway_serial}]: Ending active dispatch ({prev_act_type}). Clearing AB overrides via RESUME_NATIVE.")
                            await automation_engine.execute_sd_signal_list(
                                gateway_serial=gateway_serial,
                                signals=[{"order": 1, "action": "RESUME_NATIVE"}],
                                dispatch_guid=f"SD-CLEAR-{int(soc_pct or 0)}"
                            )
                    else:
                        logger.info(f"SmartDispatch [{gateway_serial}]: Bridging {result.action} to AutomationEngine execution")
                        await automation_engine.execute_sd_signal_list(
                            gateway_serial=gateway_serial,
                            signals=[{"order": 1, "action": result.action}],
                            dispatch_guid=f"SD-TICK-{int(soc_pct or 0)}"
                        )
                else:
                    logger.error("SmartDispatch: Execution bridge failed — automation_engine not found in app_state")

            # ── VPP Mode master gate (Batch G, 2026-07-11) ────────────────────
            # When the gateway is under VPP or Modbus external control, HEMS
            # cannot dispatch — so notifications, cooldown sweeps, and audit
            # entries for HEMS-originated actions are all suppressed. We
            # still write a single SUPPRESSED audit entry per tick so the
            # user can see WHY the engine's decision didn't ship.
            # Batch P (2026-07-30): reuse the early computation instead of
            # re-hitting the registry. Identical value; saves one call.
            _vpp_active, _vpp_reason = _vpp_active_early, _vpp_reason_early

            # ── Sweep expired-and-ignored pendings → arm cooldowns ───────────
            # Runs once per tick before either strategy branch inspects
            # cooldowns. Converts pending_approvals whose TTL has elapsed
            # without user response into notification_cooldown records keyed
            # by (gateway, ev_key). This is the core dedup mechanism: once a
            # user ignores a same-rule prompt for the full TTL, the engine
            # stops re-firing that rule until the cooldown expires.
            #
            # Batch G: skip the sweep while VPP is active. Otherwise the
            # instant VPP hands control back to HEMS, any pending that
            # expired *during* the VPP window would arm a fresh cooldown
            # and gag the very first legit post-VPP notification.
            if _vpp_active:
                logger.debug(
                    f"[{gateway_serial}] SmartDispatch: cooldown sweep skipped — "
                    f"VPP override active ({_vpp_reason})"
                )
            else:
                try:
                    if gateway_serial:
                        _armed = await db.sweep_expired_pendings_and_arm_cooldowns(gateway_serial)
                        if _armed:
                            logger.info(
                                f"[{gateway_serial}] SmartDispatch: "
                                f"armed {_armed} cooldown(s) from expired pending approvals"
                            )
                except Exception as _sweep_exc:
                    logger.debug(f"SmartDispatch: cooldown sweep error — {_sweep_exc}")

            # ── User Approval: send HA notification ──────────────────────────
            # SD NEVER executes hardware commands from its tick loop except via the AB bridge above.
            # 'info' is signal-only. Only 'user_approval'
            # sends an HA notification so the user can manually approve/skip.
            if strategy == "user_approval":
                if result.can_execute and result.action not in ("STANDBY", "PAUSED", "NONE"):
                    # Batch J: fetch cfg once for stale-state thresholds.
                    _cfg_stale = await db.get_smart_dispatch_config(gateway_serial) if gateway_serial else {}
                    # Batch J: pre-compute stale-state check so it can be
                    # evaluated in the same if/elif chain as VPP. VPP takes
                    # precedence (Batch G) so it's checked first.
                    _stale, _stale_reason = self._is_action_stale(result, gateway_serial, _cfg_stale)
                    # Batch G master gate — VPP owns the gateway, HEMS can't
                    # dispatch. Write SUPPRESSED audit entry once per rule
                    # emit and skip the entire actionable pipeline (no
                    # set_pending_approval, no _request_approval).
                    if _vpp_active:
                        _ev_key_vpp = NOTIFICATION_CATEGORY_MAP.get(
                            result.trigger_category or "", ("signal_change", "")
                        )[0]
                        asyncio.ensure_future(db.add_notification_log(
                            "SUPPRESSED", _ev_key_vpp,
                            f"Actionable prompt suppressed by VPP override "
                            f"({_vpp_reason}) — HEMS cannot dispatch while VPP "
                            f"owns the gateway. Would have prompted: "
                            f"action={result.action}, rule={result.rule_name!r}, "
                            f"import={snap.import_c_kwh:.2f}c, "
                            f"export={snap.export_c_kwh:.2f}c"
                        ))
                        logger.info(
                            f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: "
                            f"{result.action} — SUPPRESSED by VPP override "
                            f"({_vpp_reason})"
                        )
                        result.requires_approval = True
                    elif _stale:
                        # Batch J: recommendation would be a no-op against
                        # current live state (already charging, already full,
                        # already exporting, etc). Audit-only, skip prompt.
                        _ev_key_stale = NOTIFICATION_CATEGORY_MAP.get(
                            result.trigger_category or "", ("signal_change", "")
                        )[0]
                        asyncio.ensure_future(db.add_notification_log(
                            "SUPPRESSED", _ev_key_stale,
                            f"Actionable prompt suppressed by stale-state: {_stale_reason}. "
                            f"Would have prompted: action={result.action}, "
                            f"rule={result.rule_name!r}, "
                            f"import={snap.import_c_kwh:.2f}c, "
                            f"export={snap.export_c_kwh:.2f}c"
                        ))
                        logger.info(
                            f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: "
                            f"{result.action} — SUPPRESSED by stale-state ({_stale_reason})"
                        )
                        result.requires_approval = True
                    else:
                        import hashlib
                        decision_hash = hashlib.md5(
                            f"{result.action}:{result.rule_id}:{gateway_serial}".encode()
                        ).hexdigest()[:12]
                        existing_pending = await db.get_pending_approval(gateway_serial)
                        if existing_pending and existing_pending.get("decision_hash") == decision_hash:
                            logger.debug(f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: {result.action} — already pending, suppressing re-send")
                            result.requires_approval = True
                        else:
                            # ── Cooldown check (backlog P2 dated 2026-07-09, schema v47) ──
                            # If the user has been ignoring same-rule prompts and a
                            # previous pending_approval expired without response, an
                            # active cooldown record blocks new same-rule emits. This
                            # is the same check that runs on the info-path (line
                            # ~3455 below) but with the ev_key derived from the
                            # actionable path's own trigger_category context.
                            _actionable_ev_key = NOTIFICATION_CATEGORY_MAP.get(
                                result.trigger_category or "", ("signal_change", "")
                            )[0]
                            _actionable_cd = await db.is_notification_cooldown_active(
                                gateway_serial=gateway_serial, rule_id=_actionable_ev_key
                            ) if gateway_serial else None
                            if _actionable_cd:
                                _cd_exp_iso = ""
                                try:
                                    import datetime as _dt2
                                    _cd_exp_iso = _dt2.datetime.fromtimestamp(
                                        int(_actionable_cd.get("expires_at") or 0)
                                    ).isoformat(timespec="seconds")
                                except Exception:
                                    pass
                                asyncio.ensure_future(db.add_notification_log(
                                    "SUPPRESSED", _actionable_ev_key,
                                    f"Actionable prompt suppressed by cooldown "
                                    f"(gw={gateway_serial}, rule={_actionable_ev_key}, "
                                    f"ignored_count={_actionable_cd.get('ignored_count')}, "
                                    f"cooldown_expires_at={_cd_exp_iso}). "
                                    f"Would have prompted: action={result.action}, "
                                    f"rule={result.rule_name!r}, "
                                    f"import={snap.import_c_kwh:.2f}c, "
                                    f"export={snap.export_c_kwh:.2f}c"
                                ))
                                logger.info(
                                    f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: "
                                    f"{result.action} — SUPPRESSED by cooldown "
                                    f"(rule={_actionable_ev_key}, expires={_cd_exp_iso})"
                                )
                                result.requires_approval = True
                            else:
                                notif_settings_ua = await db.get_notification_settings()
                                max_per_hour = int(notif_settings_ua.get("actionable_rate_limit", 3))
                                can_send = await db.check_notification_rate_limit(gateway_serial, max_per_hour)
                                if can_send:
                                    import uuid
                                    request_id = str(uuid.uuid4())
                                    logger.info(f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: {result.action} — storing pending approval (request_id={request_id})")
                                    await db.set_pending_approval(
                                        gateway_serial,
                                        request_id=request_id,
                                        rule_id=result.rule_id,
                                        rule_name=result.rule_name,
                                        action=result.action,
                                        dispatch_summary=result.dispatch_summary or result.reason,
                                        ttl_secs=int(notif_settings_ua.get("actionable_ttl", 1800)),
                                        action_context={
                                            "trigger_category": result.trigger_category or "",
                                            "ev_key": _actionable_ev_key,
                                        },
                                    )
                                    result.requires_approval = True
                                    await self._request_approval(result, gateway_serial, request_id, notif_settings_ua)
                                else:
                                    logger.warning(f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: {result.action} — rate limited ({max_per_hour}/hr)")
                                    result.requires_approval = True
            else:
                # info / auto — log only, no hardware action, no dispatch
                logger.debug(f"[{gateway_serial}] SmartDispatch [{strategy.upper()}]: {result.action} — signal recorded, no hardware command issued")

            # ── Notification hook (info/status notifications only) ────────────
            # `asyncio` is imported at module scope (line 34) — a nested
            # `import asyncio` here would shadow it as a local and make every
            # earlier `asyncio.ensure_future(...)` in this function raise
            # UnboundLocalError. Only the send_ha_notification import stays.
            try:
                from src.services.notification_sender import send_ha_notification
                cfg = await db.get_smart_dispatch_config(gateway_serial) if gateway_serial else {}
                notif_settings = await db.get_notification_settings()
                notif_mode = cfg.get("notification_mode", "ask")
                # Hard guard: never send any notification when SD is disabled
                if cfg.get("strategy_mode") == "disabled":
                    logger.debug(f"[{gateway_serial}] SmartDispatch: notifications suppressed — strategy=disabled")
                else:
                    _category_map = NOTIFICATION_CATEGORY_MAP
                    if notif_settings.get("enabled") and notif_mode != "silent":
                        # If no operation is performed, do not trigger info notifications
                        # EXCEPT in Shadow Mode where we want to see what WOULD have happened.
                        if result.action in ("HOLD", "NONE") and not _is_shadow:
                            pass
                        else:
                            ev_key, toggle_key = _category_map.get(
                                result.trigger_category, ("signal_change", "notify_on_spike")
                            )
                            if cfg.get(toggle_key, 1):
                                # ── Batch G master gate: VPP override ─────────────
                                # HEMS cannot dispatch while VPP owns the gateway
                                # (banner "VPP Control Overriding HEMS Dispatch").
                                # Info notifications are just as useless as
                                # actionable ones in this state — user gets a
                                # push about a decision HEMS won't execute. Log
                                # a SUPPRESSED audit entry so intent is still
                                # visible, then skip send_ha_notification.
                                # Batch J pre-compute — same helper as
                                # actionable path; VPP takes precedence.
                                _stale_info, _stale_info_reason = self._is_action_stale(
                                    result, gateway_serial, cfg
                                )
                                if _vpp_active:
                                    asyncio.ensure_future(db.add_notification_log(
                                        "SUPPRESSED", ev_key,
                                        f"Info notification suppressed by VPP override "
                                        f"({_vpp_reason}) — HEMS cannot dispatch while VPP "
                                        f"owns the gateway. Underlying decision: "
                                        f"action={result.action}, rule={result.rule_name!r}, "
                                        f"import={snap.import_c_kwh:.2f}c, "
                                        f"export={snap.export_c_kwh:.2f}c"
                                    ))
                                    logger.debug(
                                        f"[{gateway_serial}] SmartDispatch: "
                                        f"info notification suppressed by VPP override "
                                        f"({_vpp_reason})"
                                    )
                                    cd = None
                                    _skip_send_due_to_vpp = True
                                elif _stale_info:
                                    # Batch J: recommendation would be no-op
                                    # against current state (e.g. GRID_CHARGE
                                    # when battery is already charging or full).
                                    asyncio.ensure_future(db.add_notification_log(
                                        "SUPPRESSED", ev_key,
                                        f"Info notification suppressed by stale-state: "
                                        f"{_stale_info_reason}. "
                                        f"Underlying decision: action={result.action}, "
                                        f"rule={result.rule_name!r}, "
                                        f"import={snap.import_c_kwh:.2f}c, "
                                        f"export={snap.export_c_kwh:.2f}c"
                                    ))
                                    logger.debug(
                                        f"[{gateway_serial}] SmartDispatch: "
                                        f"info notification suppressed by stale-state "
                                        f"({_stale_info_reason})"
                                    )
                                    cd = None
                                    _skip_send_due_to_vpp = True
                                else:
                                    _skip_send_due_to_vpp = False
                                    # Batch M-1: event-level stale gate — catches
                                    # cases Batch J misses because the underlying
                                    # action is HOLD but the notification event
                                    # (force_charge / export_bonus) implies an
                                    # opposite battery state.
                                    _ev_stale, _ev_stale_reason = self._is_event_stale(
                                        ev_key, gateway_serial, cfg
                                    )
                                    if _ev_stale:
                                        asyncio.ensure_future(db.add_notification_log(
                                            "SUPPRESSED", ev_key,
                                            f"Info notification suppressed by event-stale: "
                                            f"{_ev_stale_reason}. "
                                            f"Underlying decision: action={result.action}, "
                                            f"rule={result.rule_name!r}, "
                                            f"import={snap.import_c_kwh:.2f}c, "
                                            f"export={snap.export_c_kwh:.2f}c"
                                        ))
                                        logger.debug(
                                            f"[{gateway_serial}] SmartDispatch: "
                                            f"info notification suppressed by event-stale "
                                            f"({_ev_stale_reason})"
                                        )
                                        cd = None
                                        _skip_send_due_to_vpp = True
                                    else:
                                        # ── Cooldown check (backlog P2 dated 2026-07-09) ────
                                        # If a same-rule pending_approval TTL previously
                                        # expired without user response, the engine parked
                                        # a cooldown record for (gateway, ev_key). While
                                        # that cooldown is active AND its rule is enabled
                                        # in notification_cooldown_rules, we log a SUPPRESSED
                                        # audit entry instead of firing another notification.
                                        # This eliminates the perceived "duplicate" spam
                                        # where the same matrix rule re-fires every
                                        # 30-60min while the user ignores prompts.
                                        cd = await db.is_notification_cooldown_active(
                                            gateway_serial=gateway_serial, rule_id=ev_key
                                        ) if gateway_serial else None
                                if cd:
                                    _expires_iso = ""
                                    try:
                                        import datetime as _dt
                                        _expires_iso = _dt.datetime.fromtimestamp(
                                            int(cd.get("expires_at") or 0)
                                        ).isoformat(timespec="seconds")
                                    except Exception:
                                        pass
                                    asyncio.ensure_future(db.add_notification_log(
                                        "SUPPRESSED", ev_key,
                                        f"Suppressed by cooldown (gw={gateway_serial}, "
                                        f"rule={ev_key}, ignored_count={cd.get('ignored_count')}, "
                                        f"cooldown_expires_at={_expires_iso}). "
                                        f"Underlying decision: action={result.action}, "
                                        f"rule={result.rule_name!r}, "
                                        f"import={snap.import_c_kwh:.2f}c, "
                                        f"export={snap.export_c_kwh:.2f}c"
                                    ))
                                    logger.debug(
                                        f"[{gateway_serial}] SmartDispatch: "
                                        f"notification suppressed by cooldown "
                                        f"(rule={ev_key}, expires={_expires_iso})"
                                    )
                                elif not _skip_send_due_to_vpp:
                                    asyncio.ensure_future(send_ha_notification(ev_key, {
                                        "action":           result.action,
                                        "preset_name":      result.preset_name,
                                        "import_c_kwh":     snap.import_c_kwh,
                                        "export_c_kwh":     snap.export_c_kwh,
                                        "tariff_type":      snap.tariff_type,
                                        "dispatch_summary": result.dispatch_summary,
                                        "requires_approval": result.requires_approval,
                                    }, notif_settings))
            except Exception as exc:
                logger.debug(f"SmartDispatch: notification hook error — {exc}")

        # ── T-30min lookahead pre-notify ─────────────────────────────────────
        # Only fire when user is in user_approval mode and has a forecast.
        # Heuristic-based (price thresholds vs cfg), not full eval chain —
        # bounded cost so it's safe to run inline. Dedupes via
        # `sd_lookahead_dedup` (persisted across restarts) + rate-gated to
        # one scan per 10 min per gateway.
        try:
            if strategy == "user_approval" and snap.forecast:
                # Batch M-3 (2026-07-25): per-tick collapse. Skip lookahead
                # when there's already an active pending_approval — the user
                # is being prompted right now for a live decision; another
                # push (even for a different upcoming window) reads as spam.
                # Also skip when the actionable branch just sent a
                # notification THIS tick (result.requires_approval + fresh
                # pending write) — same reason.
                _skip_lookahead = False
                try:
                    if gateway_serial:
                        _active_pending = await db.get_pending_approval(gateway_serial)
                        if _active_pending:
                            _skip_lookahead = True
                            logger.debug(
                                f"[{gateway_serial}] SmartDispatch: lookahead scan "
                                f"skipped — active pending exists "
                                f"(req={_active_pending.get('request_id')!r})"
                            )
                except Exception:
                    pass
                if not _skip_lookahead:
                    await self._maybe_send_lookahead_notifications(
                        snap, gateway_serial, cfg, notif_settings
                    )
        except Exception as exc:
            logger.debug(f"SmartDispatch: lookahead hook error — {exc}")

        return result

    # ── T-30min lookahead helpers ─────────────────────────────────────────────

    async def _maybe_send_lookahead_notifications(
        self,
        snap: "PriceSnapshot",
        gateway_serial: str,
        cfg: dict,
        notif_settings: dict,
    ) -> None:
        """Delegates to `smart_dispatch.lookahead.maybe_send`. Body moved
        to its own module in v0.2.3 (Phase 1 pilot, 2026-08-02)."""
        from src.services.smart_dispatch import lookahead as _lookahead
        await _lookahead.maybe_send(self, snap, gateway_serial, cfg, notif_settings, logger)

    # ── User Approval: HA actionable notification dispatch ────────────────
    # NOTE: a duplicate `_request_approval` used to live above this one with a
    # `decision_hash` signature and an early-exit HA-unconfigured impairment
    # check. Python's later-def-wins meant only the version below ever ran;
    # the dead sibling was removed in v0.2.2 (Batch P0.4). If HA is
    # unconfigured, `send_ha_notification` returns `sent=False` and the
    # `__approval_send_failed__` log at the tail of this function still fires.

    async def _request_approval(self, result: EvalDecision, gateway_serial: str, request_id: str, notif_settings: dict) -> None:
        """Helper to build context and send the HA push notification."""
        from src.services.notification_sender import send_ha_notification
        context = {
            "action":           result.action,
            "rule_name":        result.rule_name,
            "rule_id":          result.rule_id,
            "dispatch_summary": result.dispatch_summary or result.reason or "",
            "gateway_serial":   gateway_serial,
        }
        notif_result = await send_ha_notification(
            "user_approval_request",
            context,
            notif_settings,
            force_actionable=True,
            request_id=request_id,
        )

        if notif_result.get("sent"):
            logger.info(
                f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: "
                f"Approval request sent — action={result.action} request_id={request_id}"
            )
        else:
            err = notif_result.get("error", "unknown")
            logger.warning(
                f"[{gateway_serial}] SmartDispatch [USER_APPROVAL]: "
                f"Notification send failed ({err}) — action={result.action} stays pending"
            )
            await db.log_automation_trigger(
                rule_id="__approval_send_failed__",
                rule_name="User Approval — Send Failed",
                gateway_serial=gateway_serial,
                action_type=result.action,
                status="failed",
                detail=f"HA notification send failed: {err}",
                source="amber",
            )

    # ── Active mode: dispatch execution ──────────────────────────────────

    async def _execute_dispatch_plan(self, result: EvalDecision) -> None:
        """TOMBSTONED — SD is a signal-only engine. AB handles all execution.

        This method intentionally does nothing. Direct cloud_dispatch calls
        have been removed from SmartDispatch. The signal is written to sd_signal
        KV by evaluate_and_log(); Automation Builder reads it and acts.
        """
        logger.warning(
            f"SmartDispatch: _execute_dispatch_plan called — this is a bug. "
            f"SD must not execute hardware commands. action={result.action}"
        )



# ── Singleton ────────────────────────────────────────────────────────────────
smart_dispatch_engine = SmartDispatchEngine()
