"""Macro Discovery loop — daily capability & tariff snapshot.

Phase 2.A (v0.2.4, 2026-08-05) — first of three temporal loops in the
target HEMS architecture (Macro / Meso / Micro). Runs daily @03:00
site-local per gateway and captures slow-changing facts that Meso and
Micro shouldn't re-fetch every tick:

- Gateway DNA: max grid feed, grid max import, phase configuration,
  service amps, model, three-phase group membership.
- Utility service tariff: current provider, whether TOU-scheduled or
  wholesale-priced, seasonal calendar (via `get_utility_service_for_gateway`).
- Smart Dispatch config thresholds: min_soc, max_soc, max_charge_price,
  export_bonus_threshold, notification toggles.

Result persists to `sd_macro_snapshot` (schema v52) as a JSON blob per
gateway. Non-actionable — the future Meso planner reads the latest
snapshot as one input to its 24 h dispatch plan.

Registered as an APScheduler cron job by `AutomationEngine` in
`scheduler_core.py`. See `MacroDiscovery.run_all()` for the top-level
callable; `MacroDiscovery.run(gateway_serial)` for the per-gateway
worker. Both are idempotent — re-running just writes a fresh row."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.services import db

logger = logging.getLogger(__name__)


class MacroDiscovery:
    """Enumerate registered gateways and write a per-gateway capability +
    tariff snapshot. Stateless — safe to invoke on-demand from the API
    for testing or manual re-runs."""

    @staticmethod
    async def _build_snapshot(gateway_serial: str) -> dict[str, Any]:
        """Assemble the snapshot dict for a single gateway. All lookups
        are defensive — a missing sub-source becomes `null` in the blob
        rather than failing the whole snapshot."""
        snap: dict[str, Any] = {
            "gateway_serial": gateway_serial,
            "schema_version": 1,
        }

        # ── Gateway DNA (physical capabilities) ────────────────────────────
        try:
            gw = await db.get_gateway_by_full_serial(gateway_serial)
            if gw:
                # profile_json holds the discovery-time device model + capabilities
                profile_raw = gw.get("profile_json") or "{}"
                try:
                    profile = json.loads(profile_raw) if isinstance(profile_raw, str) else profile_raw
                except (ValueError, TypeError):
                    profile = {}
                snap["dna"] = {
                    "model":                gw.get("model"),
                    "grid_type":            gw.get("grid_type"),
                    "gateway_phase":        gw.get("gateway_phase"),
                    "three_phase_group_id": gw.get("three_phase_group_id"),
                    "service_amps":         gw.get("service_amps"),
                    "site_id":              gw.get("site_id"),
                    "group_id":             gw.get("group_id"),
                    "group_name":           gw.get("group_name"),
                    "profile":              profile,
                }
            else:
                snap["dna"] = None
        except Exception as exc:
            logger.warning(f"MacroDiscovery[{gateway_serial}]: DNA lookup failed — {exc!r}")
            snap["dna"] = None

        # ── Utility service (tariff + provider identity) ────────────────────
        # Resolve via short_id (the gateway's short_id, which is what
        # `get_utility_service_for_gateway` expects).
        try:
            short_id = None
            if snap.get("dna") is None:
                # get_gateway_by_full_serial failed above; try short_id lookup
                pass
            elif gw:
                short_id = gw.get("short_id")
            if short_id:
                utility = await db.get_utility_service_for_gateway(short_id)
                snap["utility_service"] = utility  # None if unlinked
            else:
                snap["utility_service"] = None
        except Exception as exc:
            logger.warning(f"MacroDiscovery[{gateway_serial}]: utility lookup failed — {exc!r}")
            snap["utility_service"] = None

        # ── Smart Dispatch config thresholds ────────────────────────────────
        try:
            cfg = await db.get_smart_dispatch_config(gateway_serial)
            # Only pin the slow-changing knobs; volatile ones (last_full_generation_time
            # etc.) get read live by the Meso planner.
            snap["sd_config"] = {
                "min_soc":                 cfg.get("min_soc"),
                "max_soc":                 cfg.get("max_soc"),
                "max_charge_price":        cfg.get("max_charge_price"),
                "min_export_price":        cfg.get("min_export_price"),
                "export_bonus_threshold":  cfg.get("export_bonus_threshold"),
                "price_spike_threshold":   cfg.get("price_spike_threshold"),
                "daily_earnings_target":   cfg.get("daily_earnings_target"),
                "monthly_earnings_target": cfg.get("monthly_earnings_target"),
                "notification_mode":       cfg.get("notification_mode"),
                "strategy_mode":           cfg.get("strategy_mode"),
                "allow_auto_offgrid":      cfg.get("allow_auto_offgrid"),
                "lookahead_minutes":       cfg.get("lookahead_minutes"),
                "actionable_rate_limit":   cfg.get("actionable_rate_limit"),
            }
        except Exception as exc:
            logger.warning(f"MacroDiscovery[{gateway_serial}]: sd_config lookup failed — {exc!r}")
            snap["sd_config"] = None

        return snap

    @staticmethod
    async def run(gateway_serial: str) -> Optional[dict]:
        """Build and persist a Macro snapshot for one gateway. Returns the
        snapshot dict on success, None on outer failure (which is logged
        but not raised — Macro is non-actionable and must not block the
        scheduler even if one gateway is misbehaving)."""
        try:
            snap = await MacroDiscovery._build_snapshot(gateway_serial)
            await db.save_macro_snapshot(gateway_serial, json.dumps(snap))
            logger.info(
                f"MacroDiscovery[{gateway_serial}]: snapshot written "
                f"(dna={'yes' if snap.get('dna') else 'no'}, "
                f"utility={'yes' if snap.get('utility_service') else 'no'}, "
                f"cfg={'yes' if snap.get('sd_config') else 'no'})"
            )
            return snap
        except Exception as exc:
            logger.error(f"MacroDiscovery[{gateway_serial}]: run failed — {exc!r}")
            return None

    @staticmethod
    async def run_all() -> dict[str, bool]:
        """Enumerate every registered gateway and run Macro discovery on
        each. Returns a `{gateway_serial: success}` map for the caller
        (typically an APScheduler job or REST endpoint) to log or expose."""
        results: dict[str, bool] = {}
        try:
            gateways = await db.get_all_gateways()
        except Exception as exc:
            logger.error(f"MacroDiscovery.run_all: could not enumerate gateways — {exc!r}")
            return results

        for gw in gateways or []:
            serial = gw.get("full_serial")
            if not serial:
                continue
            snap = await MacroDiscovery.run(serial)
            results[serial] = snap is not None

        # Opportunistic prune — cheap and self-healing.
        try:
            deleted = await db.prune_macro_snapshots(keep_last_n=30)
            if deleted:
                logger.debug(f"MacroDiscovery.run_all: pruned {deleted} old snapshot rows")
        except Exception as prune_exc:
            logger.debug(f"MacroDiscovery.run_all: prune failed (non-fatal) — {prune_exc!r}")

        ok = sum(1 for v in results.values() if v)
        total = len(results)
        logger.info(f"MacroDiscovery.run_all: {ok}/{total} gateways snapshotted")
        return results
