"""Micro ticker — 30 s SmartDispatch evaluation loop.

Phase 2.D (v0.4.0, 2026-08-05) — third and final temporal loop.
Fires every 30 s and invokes `SmartDispatchEngine.evaluate_and_log`
for every gateway that has opted into the new path via
`smart_dispatch_config.sd_use_micro_ticker=1` (schema v53).

Coexistence with the legacy path:
- Default (`sd_use_micro_ticker=0`): PricingService.tick continues to
  fire SD on price refresh (rate-limited to 300 s). MicroTicker
  skips this gateway.
- Opt-in (`sd_use_micro_ticker=1`): PricingService.tick skips SD for
  this gateway. MicroTicker fires it on the 30 s cadence.

Exactly one path fires per gateway — no double-fire, no drift.
The flag is per-gateway so a rollout can start with a single test
gateway and expand once soaked. Instantly reversible without a
restart because the flag is read fresh on every tick.

Per-gateway rate-limit backstop of 20 s ensures overlapping ticks
can't stack even if evaluate_and_log runs longer than 30 s once."""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.services import db

logger = logging.getLogger(__name__)


class MicroTicker:
    """Callable target for APScheduler job `sd:micro:tick`. See module
    docstring for the feature-flag routing model. Idempotent — safe
    to fire more often than intended; the internal rate limit and
    the engine's own action-stale + cooldown gates handle it."""

    # Backstop against evaluate_and_log runs that exceed the 30s
    # interval — never fire the same gateway more often than this.
    _MIN_INTERVAL_SECS = 20
    _last_fire: dict[str, float] = {}

    @staticmethod
    async def _tick_for_gateway(short_id: str) -> bool:
        """Run one SD evaluation for one gateway. `short_id` is the
        smart_dispatch_config-keying identifier (also the one PricingService
        + evaluate_and_log use throughout). Returns True on a successful
        eval, False otherwise. Never raises."""
        try:
            from src.services.smart_dispatch import smart_dispatch_engine
            from src.services.pricing.service import pricing_registry
        except Exception as exc:
            logger.error(f"MicroTicker[{short_id}]: import failure — {exc!r}")
            return False

        now = time.time()
        last = MicroTicker._last_fire.get(short_id, 0.0)
        if now - last < MicroTicker._MIN_INTERVAL_SECS:
            return False
        MicroTicker._last_fire[short_id] = now

        try:
            # Resolve pricing service (same pattern as MesoScheduler /
            # PricingService.tick). Fall back to primary if unlinked.
            utility = await db.get_utility_service_for_gateway(short_id)
            pricing_svc = (
                pricing_registry.get_service(utility["id"]) if utility else None
            ) or pricing_registry.get_primary_service()
            if pricing_svc is None:
                logger.debug(f"MicroTicker[{short_id}]: no pricing service — skip")
                return False

            snap = pricing_svc.get_snapshot()
            if snap is None:
                logger.debug(f"MicroTicker[{short_id}]: no snapshot cached — skip")
                return False

            # Live SoC from the gateway registry — registry keys by
            # short_id, same as evaluate_and_log expects.
            soc_pct: Optional[float] = None
            try:
                reg = smart_dispatch_engine._gateway_registry if smart_dispatch_engine else None
                if reg:
                    gw_svc = reg.get_gateway(short_id)
                    if gw_svc and gw_svc.status and gw_svc.status.last_data:
                        soc_pct = gw_svc.status.last_data.get("battery_soc")
            except Exception:
                pass

            await smart_dispatch_engine.evaluate_and_log(
                snap,
                utility_service_id=pricing_svc.utility_service_id,
                soc_pct=soc_pct,
                gateway_serial=short_id,
            )
            return True
        except Exception as exc:
            logger.error(f"MicroTicker[{short_id}]: tick failed — {exc!r}")
            return False

    @staticmethod
    async def run_all() -> dict[str, bool]:
        """Enumerate every gateway with `sd_use_micro_ticker=1` and
        tick each. Returns `{short_id: success}` for scheduler / API
        introspection."""
        # The scheduler firing is the thing the liveness monitor is watching
        # for, and it has fired — whether any gateway opted in is a separate
        # question. mark_tick() used to live inside per-gateway evaluation, so
        # an install with no opted-in gateway stamped nothing, the monitor fell
        # back to a change-log that a site without dynamic pricing never writes,
        # and it rebuilt a perfectly healthy scheduler three times before
        # standing down.
        try:
            from src.services import sd_heartbeat
            sd_heartbeat.mark_tick()
        except Exception:
            pass   # a heartbeat failure must never stop the tick itself

        results: dict[str, bool] = {}
        try:
            gateways = await db.get_all_gateways()
        except Exception as exc:
            logger.error(f"MicroTicker.run_all: could not enumerate gateways — {exc!r}")
            return results

        for gw in gateways or []:
            short_id = gw.get("short_id")
            if not short_id:
                continue
            # Read the opt-in flag fresh each tick so a live toggle
            # takes effect on the next fire.
            try:
                cfg = await db.get_smart_dispatch_config(short_id) or {}
            except Exception:
                cfg = {}
            if not bool(cfg.get("sd_use_micro_ticker", 0)):
                continue
            results[short_id] = await MicroTicker._tick_for_gateway(short_id)

        if results:
            ok = sum(1 for v in results.values() if v)
            logger.debug(f"MicroTicker.run_all: {ok}/{len(results)} opted-in gateways ticked")
        return results
