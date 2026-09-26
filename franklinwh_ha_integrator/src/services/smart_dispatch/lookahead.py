"""T-30min lookahead pre-notification scanner.

Extracted from `smart_dispatch/__init__.py` in v0.2.3 (Phase 1 pilot,
2026-08-02) as the first slice of the SmartDispatch engine revamp. The
function is behaviourally identical to the prior in-class method — it
still reaches into the engine instance for `_is_event_stale` and shares
the same `db` / `send_ha_notification` collaborators — but living in
its own module lets it be tested, replaced, or wrapped without touching
the 4000-line engine file.

Contract:
- Called once per tick from `SmartDispatchEngine.evaluate_and_log`.
- Reads forecast periods off `snap.forecast`, matches user-enabled
  triggers against configured thresholds.
- Dedup is persisted via `sd_lookahead_dedup` (schema v51, Batch S) so
  restart no longer replays notifications for windows already sent.
- Rate-gated to at most one scan per 10 min per gateway (also
  persisted).

If Phase 2's Meso planner needs to preempt these notifications, this
is where the entry point lives — the caller only knows the coroutine
signature."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from src.services import db
from src.services.notification_sender import send_ha_notification


async def maybe_send(
    engine: Any,
    snap: Any,
    gateway_serial: str,
    cfg: dict,
    notif_settings: dict,
    logger: Any,
) -> None:
    """Run the lookahead scan for one gateway. See module docstring for
    contract. `engine` is the calling SmartDispatchEngine — used only
    for its `_is_event_stale` heuristic; the eventual Phase 1 finish
    will make that a plain function on `engine.state` too."""
    now = time.time()

    # Rate gate — at most one scan per 10 min per gateway. Live eval
    # ticks every ~30s; the forecast doesn't change that fast. Persisted
    # via sd_lookahead_dedup (kind='__scan__') — see schema v51.
    last_check = await db.get_lookahead_sent_at(gateway_serial, "__scan__", "")
    if now - last_check < 600:
        return
    await db.mark_lookahead_sent(gateway_serial, "__scan__", "", now)

    try:
        cutoff = datetime.now(timezone.utc) + timedelta(minutes=30)
    except Exception:
        return

    spike_threshold     = float(cfg.get("price_spike_threshold", 30.0))
    export_bonus_thresh = float(cfg.get("export_bonus_threshold", 5.0))
    max_charge_price    = float(cfg.get("max_charge_price", 0.0))
    export_penalty_pos  = bool(snap.export_penalty_is_positive)

    notify_spike        = bool(cfg.get("notify_on_spike", 1))
    notify_export_bonus = bool(cfg.get("notify_on_export_bonus", 1))
    notify_force_charge = bool(cfg.get("notify_on_force_charge", 1))

    # Precompute event-stale once per tick. If the battery is already
    # at max SoC or already charging, the lookahead force_charge push
    # is misleading; same for export events at min SoC.
    _spike_stale,  _spike_reason  = engine._is_event_stale("spike",        gateway_serial, cfg)
    _export_stale, _export_reason = engine._is_event_stale("export_bonus", gateway_serial, cfg)
    _fc_stale,     _fc_reason     = engine._is_event_stale("force_charge", gateway_serial, cfg)

    # Log SUPPRESSED once per stale kind per tick (not per period).
    for _staleness, _kind_ev, _reason in (
        (_spike_stale,  "spike",        _spike_reason),
        (_export_stale, "export_bonus", _export_reason),
        (_fc_stale,     "force_charge", _fc_reason),
    ):
        if _staleness:
            asyncio.ensure_future(db.add_notification_log(
                "SUPPRESSED", _kind_ev,
                f"Lookahead {_kind_ev!r} suppressed by event-stale: {_reason}."
            ))

    async def _register_pending(
        rule_id_sentinel: str,
        rule_name: str,
        action_signal: str,
        summary: str,
        when_iso: str,
    ) -> str:
        """Create a pending_approval row so the FWH_APPROVE_V2 /
        FWH_FORCE_CHARGE_V2 callback can bridge to execute_sd_signal_list
        (Batch L, 2026-07-21). Rule-id sentinel `__lookahead_*` is used
        by downstream consumers to identify lookahead-origin approvals."""
        req_id = str(uuid.uuid4())
        try:
            await db.set_pending_approval(
                gateway_serial=gateway_serial,
                request_id=req_id,
                rule_id=rule_id_sentinel,
                rule_name=rule_name,
                action=action_signal,
                dispatch_summary=summary,
                ttl_secs=1800,
                action_context={
                    "lookahead": True,
                    "when":      when_iso,
                    "trigger_category": rule_id_sentinel.strip("_").replace("lookahead_", ""),
                    "ev_key":    rule_id_sentinel.strip("_").replace("lookahead_", ""),
                },
            )
        except Exception as _exc:
            logger.warning(
                f"[{gateway_serial}] lookahead: failed to register pending "
                f"for rule={rule_id_sentinel!r} — {_exc}"
            )
        return req_id

    for period in (snap.forecast or [])[:12]:
        try:
            p_start = period.start
            if p_start.tzinfo is None:
                p_start = p_start.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if p_start > cutoff:
            break
        if p_start < datetime.now(timezone.utc):
            continue

        imp_c = float(period.import_c_kwh or 0.0)
        exp_c = float(period.export_c_kwh) if period.export_c_kwh is not None else None
        hour_bucket = p_start.isoformat()[:13]

        # Kind 1: price spike (import_c above threshold)
        if notify_spike and imp_c >= spike_threshold and not _spike_stale:
            _last_sent = await db.get_lookahead_sent_at(gateway_serial, "spike", hour_bucket)
            if _last_sent < now - 3600:
                await db.mark_lookahead_sent(gateway_serial, "spike", hour_bucket, now)
                _rid = await _register_pending(
                    rule_id_sentinel="__lookahead_spike__",
                    rule_name="Lookahead — Price Spike",
                    action_signal="SPIKE_PROTECT",
                    summary=f"Upcoming spike: {imp_c:.1f}¢ import expected at {p_start.strftime('%H:%M')}",
                    when_iso=p_start.isoformat(),
                )
                asyncio.ensure_future(send_ha_notification("price_spike", {
                    "action":            "SPIKE_PROTECT",
                    "preset_name":       None,
                    "import_c_kwh":      imp_c,
                    "export_c_kwh":      exp_c,
                    "tariff_type":       getattr(period, "tariff_type", "PEAK"),
                    "dispatch_summary":  f"Upcoming spike: {imp_c:.1f}¢ import expected at {p_start.strftime('%H:%M')}",
                    "requires_approval": True,
                    "_lookahead":        True,
                    "_when":             p_start.isoformat(),
                    "gateway_serial":    gateway_serial,
                    "gateway_id":        gateway_serial,
                    "rule_id":           "__lookahead_spike__",
                }, notif_settings, request_id=_rid))

        # Kind 2: export bonus (paid-to-export window)
        if notify_export_bonus and exp_c is not None and not _export_stale:
            is_bonus = (exp_c <= -export_bonus_thresh) if not export_penalty_pos else (exp_c >= export_bonus_thresh)
            if is_bonus:
                _last_sent = await db.get_lookahead_sent_at(gateway_serial, "export_bonus", hour_bucket)
                if _last_sent < now - 3600:
                    await db.mark_lookahead_sent(gateway_serial, "export_bonus", hour_bucket, now)
                    _rid = await _register_pending(
                        rule_id_sentinel="__lookahead_export_bonus__",
                        rule_name="Lookahead — Export Bonus",
                        action_signal="GRID_EXPORT",
                        summary=f"Upcoming export window: {exp_c:.1f}¢/kWh feed-in at {p_start.strftime('%H:%M')}",
                        when_iso=p_start.isoformat(),
                    )
                    asyncio.ensure_future(send_ha_notification("export_bonus", {
                        "action":            "GRID_EXPORT",
                        "preset_name":       None,
                        "import_c_kwh":      imp_c,
                        "export_c_kwh":      exp_c,
                        "tariff_type":       getattr(period, "tariff_type", "PEAK"),
                        "dispatch_summary":  f"Upcoming export window: {exp_c:.1f}¢/kWh feed-in at {p_start.strftime('%H:%M')}",
                        "requires_approval": True,
                        "_lookahead":        True,
                        "_when":             p_start.isoformat(),
                        "gateway_serial":    gateway_serial,
                        "gateway_id":        gateway_serial,
                        "rule_id":           "__lookahead_export_bonus__",
                    }, notif_settings, request_id=_rid))

        # Kind 3: force charge (cheap import window)
        if notify_force_charge and max_charge_price > 0 and imp_c <= max_charge_price and not _fc_stale:
            _last_sent = await db.get_lookahead_sent_at(gateway_serial, "force_charge", hour_bucket)
            if _last_sent < now - 3600:
                await db.mark_lookahead_sent(gateway_serial, "force_charge", hour_bucket, now)
                _rid = await _register_pending(
                    rule_id_sentinel="__lookahead_force_charge__",
                    rule_name="Lookahead — Force Charge",
                    action_signal="GRID_CHARGE",
                    summary=f"Cheap charging window: {imp_c:.1f}¢ import at {p_start.strftime('%H:%M')}",
                    when_iso=p_start.isoformat(),
                )
                asyncio.ensure_future(send_ha_notification("force_charge", {
                    "action":            "GRID_CHARGE",
                    "preset_name":       None,
                    "import_c_kwh":      imp_c,
                    "export_c_kwh":      exp_c,
                    "tariff_type":       getattr(period, "tariff_type", "OFFPEAK"),
                    "dispatch_summary":  f"Cheap charging window: {imp_c:.1f}¢ import at {p_start.strftime('%H:%M')}",
                    "requires_approval": True,
                    "_lookahead":        True,
                    "_when":             p_start.isoformat(),
                    "gateway_serial":    gateway_serial,
                    "gateway_id":        gateway_serial,
                    "rule_id":           "__lookahead_force_charge__",
                }, notif_settings, request_id=_rid))

    # Prune dedup rows older than 24h. Opportunistic — one DELETE per scan.
    try:
        await db.prune_lookahead_sent(older_than_secs=86400)
    except Exception as _prune_exc:
        logger.debug(f"sd_lookahead_dedup prune failed (non-fatal): {_prune_exc}")
