"""
Manual Enphase export-limit (DPEL) test, with an automatic revert.

`DPELController.set_export_limit()` existed and nothing called it, so there was
no way to command a limit, and no way to undo one. This is the smallest safe
path for a supervised test:

* **Apply** reads the current limit first (the value to restore), writes the
  new one, and reads it back. If the current limit cannot be read the caller
  must say what to restore to — otherwise a revert would have nothing to put
  back.
* **Revert** writes the original back and reads it back.
* **Auto-revert** runs in fwhai, not the browser, so a closed tab or a dropped
  connection cannot leave solar capped. The test is persisted, so a restart
  reverts on start-up if the time has passed, or re-arms the timer if not.

One test at a time. Every apply and revert is audited.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from src.services import db

logger = logging.getLogger(__name__)

STATE_KEY = "enphase.dpel_test"
DEFAULT_MINUTES = 10
MAX_MINUTES = 60
#: Retries for an auto-revert that fails (Envoy briefly unreachable).
REVERT_RETRIES = 10
REVERT_RETRY_S = 60

_lock = asyncio.Lock()
_timer: Optional[asyncio.Task] = None


async def _controller():
    """A DPELController from the stored Solar Setup → Enphase settings."""
    from src.services.enphase_envoy import DPELController
    cfg = await db.get_solar_forecast_config()
    host = cfg.get("enphase_host") or ""
    if not host:
        raise RuntimeError("Envoy host not configured — set it in Solar Setup → Enphase")
    return DPELController(
        host=host,
        username=cfg.get("enphase_user") or "installer",
        password=cfg.get("enphase_password") or "",
        token=cfg.get("enphase_token") or None,
    ), int(cfg.get("enphase_slew_rate") or 500)


async def _load() -> Optional[dict]:
    raw = await db.get_config_value(STATE_KEY, None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def _save(state: Optional[dict]) -> None:
    await db.set_config_value(STATE_KEY, json.dumps(state) if state else "")


async def _audit(details: str) -> None:
    try:
        await db.log_admin_audit(event="Enphase:DPEL", source="ui", details=details)
    except Exception:
        logger.debug("DPEL audit write failed", exc_info=True)


async def status() -> dict:
    state = await _load()
    if not state:
        return {"ok": True, "active": False}
    return {"ok": True, "active": True,
            "remaining_s": max(0, int(state["expires_at"] - time.time())), **state}


async def apply(watt: int, duration_min: int = DEFAULT_MINUTES,
                restore_watt: Optional[int] = None) -> dict:
    if watt < 0:
        return {"ok": False, "error": "watt must be 0 or more"}
    if not 1 <= duration_min <= MAX_MINUTES:
        return {"ok": False, "error": f"duration_min must be 1-{MAX_MINUTES}"}
    async with _lock:
        if await _load():
            return {"ok": False, "error": "A DPEL test is already running — revert it first"}
        try:
            ctrl, slew = await _controller()
            before = await asyncio.to_thread(ctrl.read_export_limit)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        original = before.get("watt") if before.get("ok") else None
        if original is None:
            if restore_watt is None:
                return {"ok": False, "before": before,
                        "error": "Could not read the current limit, so there is nothing to "
                                 "revert to. Pass restore_watt to say what to restore."}
            original = int(restore_watt)

        result = await asyncio.to_thread(ctrl.set_export_limit, int(watt), slew)
        if not result.get("success"):
            await _audit(f"apply {watt} W failed: {result.get('error')}")
            return {"ok": False, "error": result.get("error"), "before": before}

        after = await asyncio.to_thread(ctrl.read_export_limit)
        verified = (after.get("watt") == int(watt)) if after.get("ok") and after.get("watt") is not None else None

        now = time.time()
        state = {
            "target_watt": int(watt), "original_watt": int(original),
            "original_source": "read" if before.get("watt") is not None else "restore_watt",
            "applied_at": now, "expires_at": now + duration_min * 60,
            "endpoint": result.get("endpoint"), "method": result.get("method"),
            "verified": verified,
        }
        await _save(state)
        _arm(state["expires_at"] - now)
        await _audit(f"apply {watt} W for {duration_min} min (was {original} W, "
                     f"read-back {'confirmed' if verified else 'unconfirmed' if verified is None else 'MISMATCH'})")
        logger.warning(f"Enphase DPEL test: limit {watt} W for {duration_min} min; reverts to {original} W")
        return {"ok": True, "state": state, "before": before, "after": after}


async def revert(reason: str = "manual") -> dict:
    global _timer
    async with _lock:
        state = await _load()
        if not state:
            return {"ok": True, "active": False, "message": "No DPEL test is running"}
        try:
            ctrl, slew = await _controller()
            result = await asyncio.to_thread(ctrl.set_export_limit, int(state["original_watt"]), slew)
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        if not result.get("success"):
            await _audit(f"revert ({reason}) to {state['original_watt']} W FAILED: {result.get('error')}")
            logger.error(f"Enphase DPEL revert failed: {result.get('error')} — limit still {state['target_watt']} W")
            return {"ok": False, "error": result.get("error"), "state": state}
        after = await asyncio.to_thread(ctrl.read_export_limit)
        await _save(None)
        if _timer and _timer is not asyncio.current_task():
            _timer.cancel()
        _timer = None
        await _audit(f"revert ({reason}) to {state['original_watt']} W")
        logger.warning(f"Enphase DPEL test reverted ({reason}) to {state['original_watt']} W")
        return {"ok": True, "restored_watt": state["original_watt"], "after": after}


def _arm(delay_s: float) -> None:
    global _timer
    if _timer and not _timer.done():
        _timer.cancel()
    _timer = asyncio.create_task(_auto_revert(max(0.0, delay_s)), name="dpel_auto_revert")


async def _auto_revert(delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    for attempt in range(1, REVERT_RETRIES + 1):
        out = await revert(reason="auto")
        if out.get("ok"):
            return
        logger.error(f"Enphase DPEL auto-revert attempt {attempt}/{REVERT_RETRIES} failed")
        await asyncio.sleep(REVERT_RETRY_S)


async def resume_on_startup() -> None:
    """A test survives a restart: revert now if due, otherwise re-arm."""
    state = await _load()
    if not state:
        return
    remaining = state["expires_at"] - time.time()
    logger.warning(f"Enphase DPEL test found at start-up; {'reverting now' if remaining <= 0 else f'reverting in {int(remaining)} s'}")
    _arm(remaining)
