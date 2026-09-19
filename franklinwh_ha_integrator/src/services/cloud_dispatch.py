"""
Cloud TOU Dispatch Control for franklinwh-ha-integrator.

Uses Time-of-Use (TOU) schedule manipulation to control battery behaviour.
Ported from FEM to be pure async.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = 'data'
_BACKUP_FILE = os.path.join(_DATA_DIR, 'tou_backup.json')

MAX_POWER_KW = 5.0
MIN_POWER_KW = 0.1
MAX_DURATION_MIN = 480      # 8 hours
MIN_DURATION_MIN = 5
DEFAULT_DURATION_MIN = 60
DEFAULT_MAX_SOC = 100
DEFAULT_WAVE_TYPE = 0       # Off-Peak

DISPATCH_CHARGE = 8
DISPATCH_DISCHARGE = 7
DISPATCH_SELF = 6

ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_STOP = "stop"
VALID_ACTIONS = {ACTION_CHARGE, ACTION_DISCHARGE, ACTION_STOP}

MODE_SELF_CONSUMPTION = "Self Consumption"
MODE_CODES = {
    "Self Consumption": 2,
    "Time of Use": 1,
    "Emergency Backup": 3,
}

@dataclass
class CloudDispatchState:
    active: bool = False
    action: str = ""
    power_kw: float = 0.0
    max_soc: Optional[int] = None
    duration_min: int = DEFAULT_DURATION_MIN
    started_at: float = 0.0
    end_time: str = ""
    start_time: str = ""
    dispatch_code: int = 0
    command_result: str = ""
    previous_mode: Optional[str] = None
    last_status: dict = field(default_factory=dict)


class AsyncCloudDispatchService:
    """Async Battery dispatch control via Cloud TOU API."""

    def __init__(self, gateway: Any):
        self._gateway = gateway
        self._state = CloudDispatchState()
        self._state_lock = asyncio.Lock()
        logger.info("☁️ AsyncCloudDispatchService initialized")

    async def charge(
        self,
        power_kw: float = 2.0,
        max_soc: int = DEFAULT_MAX_SOC,
        duration_min: int = DEFAULT_DURATION_MIN,
        start_time: Optional[str] = None,
    ) -> dict:
        return await self._dispatch(
            action=ACTION_CHARGE,
            power_kw=power_kw,
            dispatch_code=DISPATCH_CHARGE,
            max_soc=max_soc,
            duration_min=duration_min,
            start_time=start_time,
        )

    async def discharge(
        self,
        power_kw: float = 2.0,
        duration_min: int = DEFAULT_DURATION_MIN,
        min_soc: Optional[int] = None,
        start_time: Optional[str] = None,
    ) -> dict:
        return await self._dispatch(
            action=ACTION_DISCHARGE,
            power_kw=power_kw,
            dispatch_code=DISPATCH_DISCHARGE,
            duration_min=duration_min,
            min_soc=min_soc,
            start_time=start_time,
        )

    async def stop(self) -> dict:
        return await self._do_stop(reason="user_stop")

    async def run_preset(self, name: str, schedule_data: list) -> dict:
        return await self._dispatch_custom_schedule(action=f"preset: {name}", detail_vo_list=schedule_data)

    async def run_custom_schedule(self, schedule_data: list) -> dict:
        return await self._dispatch_custom_schedule(action="custom_schedule", detail_vo_list=schedule_data)

    async def status(self, last_data: Optional[dict] = None) -> dict:
        async with self._state_lock:
            s = self._state
            elapsed = time.time() - s.started_at if s.active else 0
            remaining = max(0, (s.duration_min * 60) - elapsed) if s.active else 0

            # run_status: live hardware state from get_stats() telemetry
            # 0=Standby, 1=Charging, 2=Discharging  (FranklinWH cloud enum)
            _RUN_STATUS_LABELS = {0: "Standby", 1: "Charging", 2: "Discharging"}
            run_status_int = None
            run_status_label = None
            if last_data and "run_status" in last_data:
                run_status_int = int(last_data["run_status"] or 0)
                run_status_label = _RUN_STATUS_LABELS.get(run_status_int, f"Unknown ({run_status_int})")

            return {
                "method": "cloud",
                "active": s.active,
                "action": s.action,
                "power_kw": s.power_kw,
                "max_soc": s.max_soc,
                "duration_min": s.duration_min,
                "elapsed_s": round(elapsed),
                "remaining_s": round(remaining),
                "start_time": s.start_time,
                "end_time": s.end_time,
                "dispatch_code": s.dispatch_code,
                "command_result": s.command_result,
                "previous_mode": s.previous_mode,
                "run_status": run_status_int,
                "run_status_label": run_status_label,
            }

    @property
    def is_active(self) -> bool:
        return self._state.active

    async def _dispatch(
        self,
        action: str,
        power_kw: float,
        dispatch_code: int,
        max_soc: Optional[int] = None,
        min_soc: Optional[int] = None,
        duration_min: int = DEFAULT_DURATION_MIN,
        start_time: Optional[str] = None,
    ) -> dict:
        error = self._validate(action, power_kw, max_soc, duration_min)
        if error:
            return error

        if power_kw == 0:
            return await self._do_stop(reason="zero_power")

        now = datetime.now()
        if start_time:
            try:
                h, m = map(int, start_time.split(":"))
                start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if start_dt < now:
                    start_dt += timedelta(days=1)
            except (ValueError, AttributeError):
                return {"success": False, "error": f"Invalid start_time: {start_time}"}
        else:
            start_dt = now

        end_dt = start_dt + timedelta(minutes=duration_min)
        start_hhmm = start_dt.strftime("%H:%M")
        if end_dt.date() > start_dt.date():
            end_hhmm = "24:00"
        else:
            end_hhmm = end_dt.strftime("%H:%M")

        if self._state.active:
            await self._do_stop(reason="new_command")

        await self._backup_tou_schedule()
        previous_mode = await self._get_current_mode()
        # Normalize: if gateway is already in TOU mode it was put there by a previous dispatch,
        # not by the user. Restoring to TOU/Standby on Stop is wrong — always restore to
        # Self-Consumption unless the user was explicitly in Emergency Backup.
        _TOU_MODES = {"Time of Use", "Time-of-Use", "TOU"}
        if previous_mode in _TOU_MODES or previous_mode is None:
            previous_mode = MODE_SELF_CONSUMPTION
            logger.debug(f"☁️ Cloud dispatch: previous_mode normalised to Self-Consumption (was TOU/None)")


        power_int = int(round(power_kw))
        power_w   = int(round(power_kw * 1000))   # API expects watts for power limit fields
        action_name = "Charge" if action == ACTION_CHARGE else "Discharge"
        schedule_entry = {
            "name": f"FHAI {action_name}",
            "startHourTime": start_hhmm,
            "endHourTime": end_hhmm,
            "waveType": DEFAULT_WAVE_TYPE,
            "dispatchId": dispatch_code,
            # chargePower/dischargePower: integer kW (display/reference field)
            "chargePower":     power_int if action == ACTION_CHARGE    else 0,
            "dischargePower":  power_int if action == ACTION_DISCHARGE else 0,
            # gridChargeMax/gridDischargeMax: integer W (API power-cap field — must be in watts)
            "gridChargeMax":    power_w if action == ACTION_CHARGE    else None,
            "gridDischargeMax": power_w if action == ACTION_DISCHARGE else None,
        }

        if action == ACTION_CHARGE and max_soc is not None:
            schedule_entry["maxChargeSoc"] = max_soc

        if action == ACTION_DISCHARGE and min_soc is not None:
            schedule_entry["minDischargeSoc"] = min_soc

        try:
            client = await self._gateway._get_or_create_client()
            result = await client.set_tou_schedule(
                touMode="CUSTOM",
                touSchedule=[schedule_entry],
            )
            # Post-save: call calculate_expected_earnings to mirror mobile app
            # flow and potentially force gateway to pick up the new schedule.
            try:
                tou_detail = await client.get_tou_dispatch_detail()
                tou_payload = tou_detail.get("result", {})
                if tou_payload:
                    await client.calculate_expected_earnings(tou_payload)
                    logger.debug("☁️ Cloud dispatch: earnings hook called (post-save)")
            except Exception as _earn_exc:
                logger.debug(f"☁️ Cloud dispatch: earnings hook failed (non-fatal): {_earn_exc}")
        except Exception as e:
            logger.error(f"☁️ Cloud dispatch {action} failed: {e}")
            return {"success": False, "error": str(e)}

        # saveTouDispatch (called by set_tou_schedule) already saves the schedule
        # and sets touSendStatus=1 on the cloud. However, the gateway only polls
        # the cloud on its own background interval (~5-10 minutes without a push).
        #
        # Calling set_operating_mode(TOU) sends an MQTT relay notification directly
        # to the gateway hardware, triggering an immediate cloud sync so the new
        # FHAI Dispatch schedule is applied within ~30 seconds rather than ~10 minutes.
        # This is NOT a mode switch (the mode is already TOU) — it is a push trigger.
        try:
            await self._gateway.set_operating_mode(work_mode=1)  # TOU — push trigger
            logger.info(f"☁️ Cloud dispatch {action}: MQTT push sent (gateway will sync schedule within ~30s)")
        except Exception as e:
            logger.warning(f"☁️ Cloud dispatch {action}: MQTT push failed — gateway will sync on its own interval: {e}")

        async with self._state_lock:
            self._state = CloudDispatchState(
                active=True,
                action=action,
                power_kw=power_kw,
                max_soc=max_soc,
                duration_min=duration_min,
                started_at=time.time(),
                start_time=start_hhmm,
                end_time=end_hhmm,
                dispatch_code=dispatch_code,
                command_result=f"TOU schedule set: {action}",
                previous_mode=previous_mode,
            )

        return {"success": True, "action": action, "api_result": result}

    async def _dispatch_custom_schedule(self, action: str, detail_vo_list: list) -> dict:
        if self._state.active:
            await self._do_stop(reason="new_command")

        await self._backup_tou_schedule()
        previous_mode = await self._get_current_mode()

        try:
            client = await self._gateway._get_or_create_client()
            result = await client.set_tou_schedule(
                touMode="CUSTOM",
                touSchedule=detail_vo_list,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        async with self._state_lock:
            self._state = CloudDispatchState(
                active=True,
                action=action,
                started_at=time.time(),
                command_result=f"Custom schedule ({len(detail_vo_list)} blocks)",
                previous_mode=previous_mode,
            )

        return {"success": True, "action": action, "api_result": result}

    async def _do_stop(self, reason: str = "unknown") -> dict:
        async with self._state_lock:
            prev_mode = self._state.previous_mode or MODE_SELF_CONSUMPTION
            self._state = CloudDispatchState(active=False)

        restore_result = await self._restore_mode(prev_mode)
        tou_restore = await self._restore_tou_schedule()

        return {"success": True, "reason": reason, "restored_mode": prev_mode, "tou_restore": tou_restore}

    def _validate(self, action: str, power_kw: float, max_soc: Optional[int], duration_min: int) -> Optional[dict]:
        if action not in VALID_ACTIONS:
            return {"success": False, "error": f"Invalid action: {action}"}
        if action == ACTION_CHARGE and max_soc is not None and not (20 <= max_soc <= 100):
            return {"success": False, "error": f"Max SoC {max_soc}% out of range (20–100%)"}
        return None

    async def _get_current_mode(self) -> Optional[str]:
        try:
            res = await self._gateway.get_operating_mode()
            if res.get("ok"):
                mode_dict = res.get("mode", {})
                return str(mode_dict.get('workModeStr', MODE_SELF_CONSUMPTION))
        except Exception:
            pass
        return None

    async def _restore_mode(self, mode_name: str) -> Optional[str]:
        mode_code = MODE_CODES.get(mode_name, 2)
        try:
            result = await self._gateway.set_operating_mode(work_mode=mode_code)
            return f"Restored mode code {mode_code}: {result}"
        except Exception as e:
            return f"Restore failed: {e}"

    async def _backup_tou_schedule(self) -> None:
        try:
            if os.path.exists(_BACKUP_FILE):
                if (time.time() - os.path.getmtime(_BACKUP_FILE)) < 300:
                    return

            res = await self._gateway.get_tou_schedule()
            if not res.get("ok"):
                return

            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_BACKUP_FILE, "w") as f:
                # Save under "detail" key — full raw payload with all seasons.
                # The restore path reads both "detail" and legacy "result" key.
                json.dump({"backed_up_at": datetime.now().isoformat(), "detail": res["detail"]}, f)
            logger.debug(
                f"☁️ TOU backup: {len(res['detail'].get('strategyList', []))} season(s) saved"
            )
        except Exception as e:
            logger.warning(f"☁️ TOU backup failed: {e}")

    async def _restore_tou_schedule(self) -> str:
        """Restore full multi-season TOU schedule from backup after dispatch stops."""
        if not os.path.exists(_BACKUP_FILE):
            return "no_backup_file"
        try:
            with open(_BACKUP_FILE, "r") as f:
                backup = json.load(f)

            # Support both the new "detail" key and the legacy "result" key.
            result_data = backup.get("detail") or backup.get("result", {})
            # Unwrap the FranklinWH API envelope if present:
            # get_tou_dispatch_detail() returns {"code": 200, "result": {"strategyList": [...]}}
            # so result_data may itself be that envelope — unwrap one level.
            if isinstance(result_data.get("result"), dict):
                result_data = result_data["result"]
            strategy_list = result_data.get("strategyList", [])


            client = await self._gateway._get_or_create_client()

            if strategy_list:
                # Multi-season restore — sends full strategyList back so all
                # seasons and day-types are preserved exactly (Fix 2).
                await client.set_tou_schedule_multi(strategy_list)
                status = f"restored ({len(strategy_list)} season(s))"
                logger.info(f"☁️ TOU restore: {status} via set_tou_schedule_multi")
            else:
                # Flat fallback for legacy single-season backups.
                day_type_list = result_data.get("dayTypeVoList") or []
                detail_vo_list = (
                    day_type_list[0].get("detailVoList", []) if day_type_list
                    else result_data.get("detailVoList", [])
                )
                if not detail_vo_list:
                    return "restore_skipped: no schedule data in backup"
                await client.set_tou_schedule(
                    touMode="CUSTOM",
                    touSchedule=detail_vo_list,
                    seasons=[{"name": "Season 1", "months": "1,2,3,4,5,6,7,8,9,10,11,12"}],
                )
                status = "restored (flat, legacy format)"
                logger.info(f"☁️ TOU restore: {status}")

            try:
                os.remove(_BACKUP_FILE)
            except OSError:
                pass
            return status
        except Exception as e:
            logger.error(f"☁️ TOU restore error: {e}")
            return f"restore_error: {e}"

