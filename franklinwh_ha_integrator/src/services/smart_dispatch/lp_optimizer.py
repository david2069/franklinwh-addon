"""LP optimizer — v0.5.1 (Phase 3.B, 2026-08-06).

Real linear-programming dispatch planner behind the `PlanOptimizer`
protocol (from v0.2.5 seam). Replaces the v0.5.0 delegate.

## Model

Pure price-arbitrage LP. Decision horizon = up to N forecast slots
(capped at 48 = 24 h at 30-min granularity, but honours whatever the
snap actually supplies — Amber typically gives ~48, ComEd ~24).

Decision variables per slot t ∈ [0, N-1]:
- `charge_kwh[t]` ∈ [0, max_charge_per_slot] — energy drawn from grid
- `dchg_kwh[t]`   ∈ [0, max_dchg_per_slot]   — energy exported to grid
- `soc_kwh[t]`    ∈ [min_soc_kwh, max_soc_kwh] — battery energy after slot t

Constraints:
- Battery balance recurrence:
    `soc_kwh[t] = soc_kwh[t-1] + charge_kwh[t] * eff_c - dchg_kwh[t] / eff_d`
  where `soc_kwh[-1] = initial_soc_kwh` (converted from soc_pct).
- SoC bounds enforced by variable declaration.
- Charge/discharge power caps enforced by variable declaration.

Objective (minimise total grid cost — cents):
    minimise Σₜ `charge_kwh[t] * import_c_kwh[t] - dchg_kwh[t] * export_c_kwh[t]`

Positive import prices penalise charging; negative import prices
(offpeak windows where the network pays to consume) reward it. Same
for exports — positive export price rewards discharge, negative
(solarSponge penalty) penalises it.

## Complementarity

The battery can't physically charge AND discharge in the same slot,
but pure LP has no way to express that (needs a binary decision).
Without the constraint, if `import_price[t] < 0` (offpeak / solar-
sponge pay-you-to-consume windows) AND `export_price[t] > 0` in the
same slot, the LP would happily charge AND discharge in parallel to
double-dip the arbitrage — economically profitable, physically
impossible.

Fix: promote to MIP with `is_charging[t] ∈ {0, 1}` binary indicator
and big-M linkage — `charge[t] ≤ max_kwh_per_slot * is_charging[t]`
and `dchg[t] ≤ max_kwh_per_slot * (1 - is_charging[t])`. CBC handles
this in <200 ms for a 48-slot horizon.

## Fallback

Never raises. On solver failure (infeasible, timeout, ImportError,
anything), logs a WARNING and delegates to `engine.synthesize_and_optimize`
(greedy). Meso must never block the engine.

## Extraction

Returns `(action: str, projected_soc_pct: float)` for slot 0 — same
tuple contract as GreedyOptimizer, so callers upgrade painlessly.
Action = "GRID_CHARGE" | "GRID_EXPORT" | "HOLD" per the sign of
slot-0 decisions."""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.services.pricing.base import PriceSnapshot

logger = logging.getLogger(__name__)


# Model defaults — matched to synthesize_and_optimize (greedy) so
# outputs are comparable when the same forecast is fed to both.
_DEFAULT_BATTERY_KWH        = 13.6    # aPower default; overridden per-gateway
_DEFAULT_MAX_POWER_KW       = 5.0     # aPower max charge/discharge rate
_DEFAULT_SLOT_HOURS         = 0.5     # 30-min slot granularity
_DEFAULT_EFF_ONE_WAY        = 0.95    # per-leg efficiency (round-trip ≈ 0.90)
_DEFAULT_MIN_SOC_PCT        = 20.0    # engine cfg default
_DEFAULT_MAX_SOC_PCT        = 90.0
_MAX_HORIZON_SLOTS          = 48      # 24 h @ 30-min granularity
_ACTION_THRESHOLD_KWH       = 0.05    # anything smaller rounds to HOLD


class LPOptimizer:
    """Real LP dispatch planner. See module docstring for the model."""

    async def solve(
        self,
        engine: Any,
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
        force: bool = False,
    ) -> tuple[str, float]:
        # Fallback wrapper — any error path returns the greedy answer so
        # Meso continues to serve. Errors are WARNING (not INFO) because
        # a live LP failure means we're operating on the greedy fallback
        # and an operator likely wants to know.
        try:
            result = await self._solve_lp(gateway_id, snap, soc_pct, cfg)
            if result is not None:
                return result
        except Exception as exc:
            logger.warning(
                f"LPOptimizer[{gateway_id}]: solver failure → falling back to greedy: {exc!r}"
            )
        return await engine.synthesize_and_optimize(gateway_id, snap, soc_pct, cfg, force=force)

    @staticmethod
    async def _solve_lp(
        gateway_id: str,
        snap: PriceSnapshot,
        soc_pct: Optional[float],
        cfg: dict,
    ) -> Optional[tuple[str, float]]:
        """Actual LP solve. Returns None if pre-conditions aren't met
        (no forecast, unusable soc, etc.) so the caller falls back."""
        forecast = list(snap.forecast or [])
        if not forecast:
            return None
        if soc_pct is None:
            return None
        # Trim horizon; guard against absurdly long or empty forecasts.
        forecast = forecast[:_MAX_HORIZON_SLOTS]
        N = len(forecast)
        if N < 1:
            return None

        # Import inside the try so pulp missing → clean fallback via the
        # outer wrapper's except.
        import pulp

        # ── Model parameters ────────────────────────────────────────────
        battery_kwh   = float(cfg.get("battery_kwh") or _DEFAULT_BATTERY_KWH)
        max_power_kw  = float(cfg.get("max_power_kw") or _DEFAULT_MAX_POWER_KW)
        slot_hours    = float(cfg.get("slot_hours") or _DEFAULT_SLOT_HOURS)
        eff_one_way   = float(cfg.get("battery_eff_one_way") or _DEFAULT_EFF_ONE_WAY)
        min_soc_pct   = float(cfg.get("min_soc") if cfg.get("min_soc") is not None else _DEFAULT_MIN_SOC_PCT)
        max_soc_pct   = float(cfg.get("max_soc") if cfg.get("max_soc") is not None else _DEFAULT_MAX_SOC_PCT)

        max_kwh_per_slot = max_power_kw * slot_hours
        min_soc_kwh      = max(0.0, min_soc_pct * battery_kwh / 100.0)
        max_soc_kwh      = min(battery_kwh, max_soc_pct * battery_kwh / 100.0)
        initial_soc_kwh  = max(min_soc_kwh, min(max_soc_kwh, soc_pct * battery_kwh / 100.0))

        # ── LP problem ──────────────────────────────────────────────────
        prob = pulp.LpProblem(f"sd_dispatch_{gateway_id}", pulp.LpMinimize)

        charge = [
            pulp.LpVariable(f"charge_{t}", lowBound=0, upBound=max_kwh_per_slot)
            for t in range(N)
        ]
        dchg = [
            pulp.LpVariable(f"dchg_{t}", lowBound=0, upBound=max_kwh_per_slot)
            for t in range(N)
        ]
        soc = [
            pulp.LpVariable(f"soc_{t}", lowBound=min_soc_kwh, upBound=max_soc_kwh)
            for t in range(N)
        ]
        # Complementarity: binary indicator per slot. 1 = charging, 0 = discharging.
        # See module docstring — without this the LP would double-dip when
        # import_price < 0 AND export_price > 0 in the same slot.
        is_charging = [
            pulp.LpVariable(f"is_charging_{t}", cat=pulp.LpBinary)
            for t in range(N)
        ]

        # Battery balance recurrence + complementarity linkage.
        _M = max_kwh_per_slot  # big-M — tight since we already bound charge/dchg
        for t in range(N):
            prev_soc = initial_soc_kwh if t == 0 else soc[t - 1]
            prob += (
                soc[t] == prev_soc + charge[t] * eff_one_way - dchg[t] * (1.0 / eff_one_way),
                f"balance_{t}",
            )
            prob += (charge[t] <= _M * is_charging[t], f"charge_only_when_ic_{t}")
            prob += (dchg[t]   <= _M * (1 - is_charging[t]), f"dchg_only_when_not_ic_{t}")

        # Objective — minimise total grid cost (cents). Missing export
        # price → treat as 0 (no revenue for that slot).
        cost_terms = []
        for t, period in enumerate(forecast):
            imp_c = float(period.import_c_kwh or 0.0)
            exp_c = float(period.export_c_kwh) if period.export_c_kwh is not None else 0.0
            cost_terms.append(charge[t] * imp_c - dchg[t] * exp_c)
        prob += pulp.lpSum(cost_terms), "grid_cost_cents"

        # ── Solve ───────────────────────────────────────────────────────
        # PULP_CBC_CMD with msg=False so solver stdout doesn't leak into
        # the app log. Time-limit as a safety cap; a 48-slot LP with 3
        # var families should solve in <100 ms on typical hardware.
        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=5)
        status = prob.solve(solver)

        if pulp.LpStatus.get(status) not in ("Optimal", "Not Solved"):
            logger.warning(
                f"LPOptimizer[{gateway_id}]: solver status={pulp.LpStatus.get(status)!r} "
                f"— treating as failure so caller falls back"
            )
            return None

        # Extract slot-0 decision
        c0 = pulp.value(charge[0]) or 0.0
        d0 = pulp.value(dchg[0]) or 0.0
        soc0_kwh = pulp.value(soc[0]) or initial_soc_kwh
        projected_soc_pct = round((soc0_kwh / battery_kwh) * 100.0, 1)

        # Round micro-magnitudes to zero — pure LP may return tiny
        # non-zero on both (see complementarity note in module docstring).
        if c0 < _ACTION_THRESHOLD_KWH and d0 < _ACTION_THRESHOLD_KWH:
            action = "HOLD"
        elif c0 > d0:
            action = "GRID_CHARGE"
        else:
            action = "GRID_EXPORT"

        # One-line summary at DEBUG so operators can inspect without
        # log noise — real fires only surface in pricing_eval_log.
        logger.debug(
            f"LPOptimizer[{gateway_id}]: N={N} slots, initial_soc={soc_pct:.1f}%, "
            f"slot_0 charge={c0:.3f} kWh, dchg={d0:.3f} kWh → action={action} "
            f"projected_soc={projected_soc_pct}% "
            f"total_cost_cents={pulp.value(prob.objective):.2f}"
        )
        return action, projected_soc_pct
