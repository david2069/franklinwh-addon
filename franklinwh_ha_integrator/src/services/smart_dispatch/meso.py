"""Meso planner — 24 h dispatch plan generator.

Phase 2.B (v0.2.5, 2026-08-05) — second of the three temporal loops.
Establishes the `PlanOptimizer` protocol as the seam where Phase 3's
LP solver will plug in, without moving the 541-line greedy heuristic
body out of `SmartDispatchEngine.synthesize_and_optimize`. The body
stays put; `GreedyOptimizer` adapts it to the protocol.

Contract:
- `PlanOptimizer.solve(engine, gateway_id, snap, soc_pct, cfg) ->
  (action: str, target_soc: float)` — mirrors the existing tuple
  return of `synthesize_and_optimize` so callers upgrade painlessly.
- `MesoPlanner` selects an optimizer (today: greedy; tomorrow: LP if
  `smart_dispatch_config.use_lp_optimizer` is enabled). Callers use
  `MesoPlanner.solve(...)` instead of calling the engine method
  directly so the swap happens in one place.

Cadence: Phase 2.C wires this to an APScheduler cron job
(00:05/06:05/12:05/18:05) plus on-price-refresh invalidation. Today
the caller is `SmartDispatchEngine.evaluate_rules`, which invokes
`MesoPlanner.solve()` on-demand per tick — identical to prior behaviour."""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from src.services.pricing.base import PriceSnapshot

logger = logging.getLogger(__name__)


@runtime_checkable
class PlanOptimizer(Protocol):
    """Interface a Meso-loop optimizer must satisfy.

    Implementations translate `(price snapshot, live SoC, gateway config)`
    into a first-slot dispatch decision plus the projected end-of-slot
    SoC. Multi-slot horizons live in the implementation's internal
    state (e.g. `sd_forecast_history` for the greedy heuristic) — the
    protocol return type deliberately matches the legacy
    `synthesize_and_optimize` tuple so the seam is minimally intrusive."""

    async def solve(
        self,
        engine: Any,
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
        force: bool = False,
    ) -> tuple[str, float]: ...


class GreedyOptimizer:
    """Adapter over the existing 541-line greedy heuristic in
    `SmartDispatchEngine.synthesize_and_optimize`. Kept as an adapter
    (not an extraction) in Phase 2.B to keep the diff small and
    behaviour-preserving — the body relocates in a later phase if it
    proves useful.

    Cadence controller, PV/load synthesis, extreme-weather scaling all
    live in the wrapped method; this class exists purely so
    `MesoPlanner` can swap it for `LPOptimizer` in Phase 3 without
    touching `evaluate_rules`."""

    async def solve(
        self,
        engine: Any,
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
        force: bool = False,
    ) -> tuple[str, float]:
        return await engine.synthesize_and_optimize(gateway_id, snap, soc_pct, cfg, force=force)


class MesoPlanner:
    """Orchestrator that selects a `PlanOptimizer` and drives it. Reads
    the `use_lp_optimizer` config flag lazily inside `solve()` so a
    live toggle takes effect without a process restart. Today only the
    greedy adapter is available; Phase 3 adds an `LPOptimizer` class
    that gets picked up here."""

    def __init__(self) -> None:
        self._greedy = GreedyOptimizer()
        # Phase 3.A (v0.5.0) — LPOptimizer wired in. In this release
        # the LP body itself is a delegate to greedy (see lp_optimizer.py
        # module docstring); the real solve lands in v0.5.1 (Phase 3.B).
        # Wiring first so the routing dispatch matures separately from
        # the LP model correctness debate.
        from src.services.smart_dispatch.lp_optimizer import LPOptimizer
        self._lp = LPOptimizer()

    async def solve(
        self,
        engine: Any,
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
        force: bool = False,
    ) -> tuple[str, float]:
        """Route to the configured optimizer. Falls back to greedy on any
        exception raised by LP — Meso must never block the engine."""
        want_lp = False
        try:
            want_lp = bool(cfg.get("use_lp_optimizer", 0))
        except Exception:
            pass

        if want_lp:
            try:
                return await self._lp.solve(engine, gateway_id, snap, soc_pct, cfg, force=force)
            except Exception as lp_exc:
                logger.warning(
                    f"MesoPlanner[{gateway_id}]: LPOptimizer raised — falling back to greedy: {lp_exc!r}"
                )

        return await self._greedy.solve(engine, gateway_id, snap, soc_pct, cfg, force=force)


# Module-level singleton — safe because the planner holds no per-request
# state (the greedy adapter is stateless too). `evaluate_rules` and any
# future scheduler job import this reference.
meso_planner = MesoPlanner()


class MesoScheduler:
    """Callable target for the APScheduler cron job `sd:meso:cron`.

    Phase 2.C (v0.3.0, 2026-08-05) — runs `meso_planner.solve()` for
    every registered gateway on a fixed cron (00:05 / 06:05 / 12:05 /
    18:05 site-local). This is the scheduled complement to the
    on-tick invocation in `SmartDispatchEngine.evaluate_rules` — the
    cron guarantees a fresh 24h plan gets computed at each boundary
    even if no price refresh landed on a tick, and `force=True`
    bypasses the greedy heuristic's internal 6h cadence controller so
    the scheduled fire always re-optimizes.

    Non-actionable — writing plan → `sd_forecast_history` is what the
    greedy body already does at line 803 of the engine. The Micro
    ticker (Phase 2.D) reads the freshest plan from that table."""

    @staticmethod
    async def _solve_for_gateway(short_id: str) -> bool:
        """Best-effort plan solve for one gateway. `short_id` is the
        smart_dispatch_config-keying identifier used throughout SD.
        Returns True on success, False otherwise. Never raises — Meso
        must not block the scheduler even if a single gateway is
        misconfigured."""
        try:
            from src.services import db
            from src.services.smart_dispatch import smart_dispatch_engine
            from src.services.pricing.service import pricing_registry
        except Exception as exc:
            logger.error(f"MesoScheduler[{short_id}]: import failure — {exc!r}")
            return False

        try:
            # Resolve the pricing service linked to this gateway; fall
            # back to the registry primary if none is explicitly linked.
            utility = await db.get_utility_service_for_gateway(short_id)
            pricing_svc = (
                pricing_registry.get_service(utility["id"]) if utility else None
            ) or pricing_registry.get_primary_service()
            if pricing_svc is None:
                logger.debug(f"MesoScheduler[{short_id}]: no pricing service — skip")
                return False

            snap = pricing_svc.get_snapshot()
            if snap is None:
                logger.debug(f"MesoScheduler[{short_id}]: no snapshot cached yet — skip")
                return False

            # Live SoC from the gateway registry (same source
            # PricingService.tick uses). Registry keys by short_id.
            soc_pct: Optional[float] = None
            try:
                reg = smart_dispatch_engine._gateway_registry if smart_dispatch_engine else None
                if reg:
                    gw_svc = reg.get_gateway(short_id)
                    if gw_svc and gw_svc.status and gw_svc.status.last_data:
                        soc_pct = gw_svc.status.last_data.get("battery_soc")
            except Exception:
                pass

            cfg = {}
            try:
                cfg = await db.get_smart_dispatch_config(short_id) or {}
            except Exception:
                pass

            action, target_soc = await meso_planner.solve(
                smart_dispatch_engine, short_id, snap, soc_pct, cfg, force=True
            )
            logger.info(
                f"MesoScheduler[{short_id}]: solved → action={action} "
                f"target_soc={target_soc:.1f}%"
            )
            return True
        except Exception as exc:
            logger.error(f"MesoScheduler[{short_id}]: solve failed — {exc!r}")
            return False

    @staticmethod
    async def run_all() -> dict[str, bool]:
        """Enumerate every registered gateway and drive Meso on each.
        Returns `{short_id: success}` for scheduler / API introspection.
        Mirrors `MacroDiscovery.run_all` shape."""
        from src.services import db

        results: dict[str, bool] = {}
        try:
            gateways = await db.get_all_gateways()
        except Exception as exc:
            logger.error(f"MesoScheduler.run_all: could not enumerate gateways — {exc!r}")
            return results

        for gw in gateways or []:
            short_id = gw.get("short_id")
            if not short_id:
                continue
            results[short_id] = await MesoScheduler._solve_for_gateway(short_id)

        ok = sum(1 for v in results.values() if v)
        logger.info(f"MesoScheduler.run_all: {ok}/{len(results)} gateways solved")
        return results
