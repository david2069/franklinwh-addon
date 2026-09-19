"""Scheduler liveness monitor + auto-restart (v0.4.8, 2026-08-06).

Guards against silent APScheduler failure. Root incident:
  2026-08-06 02:01:32 — AsyncIOScheduler.wakeup() threw
  sqlite3.OperationalError("database is locked") inside SQLAlchemyJobStore.
  update_job. Under uvloop, the wakeup callback dies quietly; the
  scheduler flag `.running` stays True, `.get_jobs()` still returns rows,
  but NO further jobs ever fire. Every scheduled evaluation went silent
  for ~5 hours until manual container restart.

Fix strategy (defence-in-depth):
1. `PRAGMA busy_timeout=5000` on the jobstore's SQLAlchemy engine —
   prevents the crash class in the first place (see scheduler_core.py).
2. This module — an independent asyncio task on the uvicorn event loop
   (NOT the scheduler's own executor loop) that polls a liveness signal
   every 60 s. If the last SmartDispatch evaluation is older than 120 s
   (2× the MicroTicker interval + buffer), the scheduler is presumed
   stalled and gets stopped + rebuilt + re-registered.

Liveness signal is `MAX(ts) FROM pricing_eval_log` for gateway 24170091 —
the most reliable end-to-end proof that the whole chain (APScheduler →
MicroTicker.run_all → smart_dispatch_engine.evaluate_and_log →
sqlite write) is functioning. If any link breaks, the signal ages out
and the monitor acts.

State is exposed on `app_state['scheduler_liveness']` so `/api/health`
can report it. Restart events are logged as WARNING (rare + notable)
and increment `restart_count` for external monitoring."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Configuration
_CHECK_INTERVAL_SECS = 60         # poll cadence
_MAX_AGE_SECS        = 120        # 2 × MicroTicker interval + buffer
_STARTUP_GRACE_SECS  = 90         # ignore stalls in the first 90 s (engine still booting)

# Stop restarting after this many consecutive rebuilds that did NOT restore the
# signal (GH #36). If N rebuilds change nothing, the diagnosis is wrong and the
# restarts are pure harm — each one calls shutdown(wait=False) and cancels
# in-flight jobs. Observed live at restart #733 against a perfectly healthy
# engine. Escalate loudly and stand down instead of looping forever.
_MAX_INEFFECTIVE_RESTARTS = 3

# Per-process state (populated by run_monitor). Consumers read via get_state().
_state: dict[str, Any] = {
    "alive":            None,     # bool | None (None = not checked yet)
    "last_eval_at":     None,     # ISO string (from pricing_eval_log)
    "last_check_at":    None,     # ISO string
    "last_restart_at":  None,     # ISO string, only set after a restart fire
    "restart_count":    0,
    "consecutive_stalls": 0,
    "signal_source":    None,     # 'heartbeat' | 'pricing_eval_log' | None
    "ineffective_restarts": 0,    # rebuilds that did not restore the signal
    "escalated":        False,    # True once we stand down (needs human attention)
}


def get_state() -> dict[str, Any]:
    """Return a copy of the current liveness state. Read-only for consumers."""
    return dict(_state)


async def _fetch_last_eval_ts() -> str | None:
    """Latest pricing_eval_log timestamp across all gateways, or None if empty.
    Chosen as the liveness signal because it's the tail of the full pipeline
    (APScheduler → MicroTicker → engine → sqlite). Any break in that chain
    means the signal ages out."""
    from src.services import db
    try:
        async with db.get_db() as conn:
            async with conn.execute(
                "SELECT MAX(ts) FROM pricing_eval_log"
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        logger.debug(f"scheduler_liveness: could not query pricing_eval_log — {exc!r}")
        return None


def _age_secs(iso_ts: str | None) -> float:
    """Return seconds since iso_ts, or infinity if unparseable / None.

    `pricing_eval_log.ts` is stored as naive 'YYYY-MM-DD HH:MM:SS' in **UTC**
    (verified 2026-08-06: `datetime.now(timezone.utc)` matches the stored
    value, while `datetime.now()` local is 10 h ahead in AEST). The naive
    string has no tz suffix, so we must ATTACH `timezone.utc` before
    calling `.timestamp()` — otherwise fromisoformat treats it as local
    and the age is off by the container's UTC offset (would false-stall
    the monitor 10 h after every write in AEST). This bug bit the v0.4.8
    smoke test."""
    if not iso_ts:
        return float("inf")
    from datetime import datetime, timezone
    try:
        stored_utc = datetime.fromisoformat(iso_ts).replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - stored_utc.timestamp())
    except Exception:
        return float("inf")


async def _measure_liveness() -> tuple[float, str, str | None]:
    """Return (age_secs, source, display_ts) for the freshest proof-of-life.

    Prefers the in-process tick heartbeat (GH #36). The historical signal,
    `MAX(ts) FROM pricing_eval_log`, is a CHANGE log: evaluate_and_log only
    writes when the decision differs from the previous one, so a healthy
    engine holding a stable decision writes nothing and the signal ages out.
    That is what produced the 733-restart loop.

    pricing_eval_log is retained as a fallback for the boot window, before the
    first tick has stamped the heartbeat — at which point it is the only
    evidence available.
    """
    from src.services import sd_heartbeat

    hb_age = sd_heartbeat.age_s()
    if hb_age is not None:
        wall = sd_heartbeat.last_tick_wall()
        display = None
        if wall:
            from datetime import datetime
            display = datetime.fromtimestamp(wall).replace(microsecond=0).isoformat()
        return hb_age, "heartbeat", display

    # No tick recorded yet this process — fall back to the persisted log.
    last_ts = await _fetch_last_eval_ts()
    return _age_secs(last_ts), "pricing_eval_log", last_ts


async def _restart_scheduler() -> bool:
    """Stop + rebuild + re-register the AsyncIOScheduler. Returns True on
    success. Called from the monitor when the liveness signal is stale."""
    from src.app_state import get_app_state
    engine = get_app_state().get("scheduler")
    if not engine:
        logger.error("scheduler_liveness: scheduler engine missing from app_state — cannot restart")
        return False

    try:
        # 1. Best-effort stop, draining in-flight jobs first (GH #36).
        #
        # engine.stop() calls shutdown(wait=False), which CANCELS running jobs
        # — the MicroTicker died mid-`get_db()` with CancelledError on every
        # rebuild. Harmless when the scheduler really is wedged, but this
        # monitor can and did fire against a healthy engine, and there it
        # destroyed ~25% of ticks. Give in-flight work a bounded chance to
        # finish, then fall back to the hard stop if it will not drain (the
        # wakeup thread may genuinely be dead, which is the original
        # incident this module was written for).
        try:
            await asyncio.wait_for(engine.stop_graceful(), timeout=10)
        except (asyncio.TimeoutError, AttributeError, Exception) as stop_exc:
            logger.warning(
                f"scheduler_liveness: graceful stop unavailable/timed out "
                f"({stop_exc!r}) — falling back to immediate stop"
            )
            try:
                await engine.stop()
            except Exception as hard_exc:
                logger.warning(f"scheduler_liveness: stop() raised (continuing) — {hard_exc!r}")

        # 2. Rebuild the AsyncIOScheduler + jobstore in place. We can't reuse
        #    the old scheduler object because AsyncIOScheduler internal state
        #    is inconsistent post-crash.
        from src.services.scheduler_core import init_engine
        from src.app_state import app_state
        registry = engine.registry  # preserve registry reference
        db_path = app_state.get("db_path")
        if not db_path:
            logger.error("scheduler_liveness: db_path missing from app_state — cannot rebuild")
            return False
        new_engine = init_engine(str(db_path), registry)
        new_engine.start()
        new_engine.register_sd_jobs()

        # CRITICAL: repoint app_state at the new engine. Every route that
        # reads user automations (e.g. /api/scheduler/jobs) resolves via
        # `state.get("scheduler")` — leaving the stale ref makes it
        # return an empty list even though the new engine is running fine
        # and the user jobs are still in the DB. Caught in v0.4.9 when
        # the (spurious) restart storm hid all 7 user automations from
        # the UI.
        app_state["scheduler"] = new_engine

        _state["last_restart_at"] = _now_iso()
        _state["restart_count"] += 1
        # Log the count BEFORE clearing it — this previously read
        # "...after 0 stall detections" on every restart because the reset
        # happened first, hiding how many detections actually preceded it.
        _stalls_before_reset = _state["consecutive_stalls"]
        _state["consecutive_stalls"] = 0
        logger.warning(
            f"scheduler_liveness: rebuilt AsyncIOScheduler after {_stalls_before_reset} "
            f"stall detections (restart #{_state['restart_count']})"
        )
        return True
    except Exception as exc:
        logger.exception(f"scheduler_liveness: rebuild failed — {exc!r}")
        return False


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().replace(microsecond=0).isoformat()


async def run_monitor() -> None:
    """Long-running coroutine — spawned by main.py lifespan startup as an
    asyncio.create_task so it lives on the uvicorn event loop, independent
    of APScheduler's own task lifecycle. Kills would take out the scheduler,
    not this monitor."""
    logger.info(
        f"scheduler_liveness: monitor started "
        f"(check={_CHECK_INTERVAL_SECS}s, max_age={_MAX_AGE_SECS}s, grace={_STARTUP_GRACE_SECS}s)"
    )
    # Startup grace — the engine hasn't ticked yet.
    await asyncio.sleep(_STARTUP_GRACE_SECS)
    while True:
        try:
            age, source, display_ts = await _measure_liveness()

            # A signal that has never been written is not a signal that stopped.
            # The fallback source is a change log, so a site that has never run
            # dynamic pricing has no rows at all and reports an infinite age
            # forever. Read that as "nothing to judge by" and leave the
            # scheduler alone — restarting it cannot create history.
            never_written = display_ts is None and source != "heartbeat"
            alive = never_written or age <= _MAX_AGE_SECS
            if never_written:
                logger.debug(
                    f"scheduler_liveness: no {source} history yet — nothing to "
                    "judge liveness by, not treating as a stall"
                )
            _state["last_eval_at"] = display_ts
            _state["signal_source"] = source
            _state["last_check_at"] = _now_iso()
            _state["alive"] = alive

            if alive:
                _state["consecutive_stalls"] = 0
                # A restart is only judged effective once the signal recovers.
                _state["ineffective_restarts"] = 0
                _state["escalated"] = False
                logger.debug(
                    f"scheduler_liveness: ok (source={source}, last={display_ts}, age={age:.0f}s)")
            elif _state["escalated"]:
                # Stood down — keep reporting, stop rebuilding (GH #36).
                logger.error(
                    f"scheduler_liveness: still stalled (source={source}, age={age:.0f}s) but "
                    f"ESCALATED after {_state['ineffective_restarts']} ineffective restarts — "
                    f"not rebuilding. Manual investigation required."
                )
            else:
                _state["consecutive_stalls"] += 1
                # Require 2 consecutive stall detections before restarting —
                # avoids a false-positive on a transient DB read hiccup.
                if _state["consecutive_stalls"] >= 2:
                    logger.warning(
                        f"scheduler_liveness: STALL confirmed "
                        f"(source={source}, last={display_ts}, age={age:.0f}s, "
                        f"consecutive_stalls={_state['consecutive_stalls']}) — restarting scheduler"
                    )
                    await _restart_scheduler()
                    # Count it as ineffective until a later check proves otherwise
                    # (the `alive` branch above resets this). Rebuilding forever
                    # against a signal that never moves is pure harm: each cycle
                    # cancels in-flight jobs.
                    _state["ineffective_restarts"] += 1
                    if _state["ineffective_restarts"] >= _MAX_INEFFECTIVE_RESTARTS:
                        _state["escalated"] = True
                        logger.error(
                            f"scheduler_liveness: {_state['ineffective_restarts']} consecutive "
                            f"restarts failed to restore the signal (source={source}) — STANDING "
                            f"DOWN. Either the liveness signal is wrong or the fault is not the "
                            f"scheduler. No further automatic rebuilds until it recovers."
                        )
                else:
                    logger.warning(
                        f"scheduler_liveness: possible stall "
                        f"(source={source}, last={display_ts}, age={age:.0f}s) — will confirm on next check"
                    )
        except Exception as exc:
            # Monitor MUST NOT die. Anything at all → log + continue.
            logger.exception(f"scheduler_liveness: monitor tick error (non-fatal) — {exc!r}")
        await asyncio.sleep(_CHECK_INTERVAL_SECS)
