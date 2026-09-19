"""Shared audit-query primitives (Batch O).

Extracted from scripts/audit_notifications.py so both the CLI tool AND
the REST API endpoints can hit the same query logic. Any anomaly rule
change (Batch O.2 event-stale broadening etc.) lives in one place.

All functions take an open connection (aiosqlite for the API path,
sqlite3.Connection for the CLI path — SELECT-only, same syntax).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


# ── Range parsing ──────────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"^(\d+)\s*([hmds])$")


def _range_pair(s: str) -> tuple[str, str]:
    """Return (numeric_n, sqlite_unit) so callers can compose their own SQL."""
    m = _RANGE_RE.match((s or "").strip().lower())
    if not m:
        raise ValueError(f"bad range {s!r} — expected e.g. '72h', '24h', '7d', '30m'")
    n, unit = m.group(1), m.group(2)
    unit_map = {"h": "hours", "d": "days", "m": "minutes", "s": "seconds"}
    return (n, unit_map[unit])


# ── Anomaly classifier (shared with the SD engine's Batch O.2 gate) ───────

@dataclass
class Anomaly:
    kind: str
    reason: str


_FLOW = 0.200


def classify_anomaly(event: str, soc, batt, grid) -> Optional[Anomaly]:
    """Apply the Batch O.2 event-stale rules to a notification's live
    context. Returns None if the notification looks correct; otherwise
    an Anomaly with a machine-readable kind + human reason.

    Mirrors SmartDispatchEngine._is_event_stale — keep the two in sync."""
    if event not in ("force_charge", "spike", "export_bonus", "negative_export"):
        return None
    if event in ("force_charge", "spike"):
        if isinstance(soc, (int, float)) and soc >= 90:
            return Anomaly("battery-full", f"soc={soc:.1f}% ≥ 90% but charge-prompt fired")
        if isinstance(batt, (int, float)) and batt < -_FLOW:
            return Anomaly("already-charging", f"battery_kw={batt:+.2f}kW (charging) but charge-prompt fired")
        if isinstance(grid, (int, float)) and grid < -_FLOW:
            return Anomaly("exporting-vs-charge", f"grid_kw={grid:+.2f}kW (exporting) but charge-prompt fired")
    else:
        if isinstance(soc, (int, float)) and soc <= 20:
            return Anomaly("battery-empty", f"soc={soc:.1f}% ≤ 20% but export-prompt fired")
        if isinstance(grid, (int, float)) and grid <= -_FLOW:
            return Anomaly("already-exporting", f"grid_kw={grid:+.2f}kW (exporting) — redundant export prompt")
        if isinstance(batt, (int, float)) and batt < -_FLOW:
            return Anomaly("charging-vs-export", f"battery_kw={batt:+.2f}kW (charging) but export-prompt fired")
    return None


# ── Async query helpers (for FastAPI/aiosqlite path) ───────────────────────

async def audit_notifications_async(
    conn,
    *,
    last: str = "72h",
    event: Optional[str] = None,
    direction: Optional[str] = None,
    anomalies_only: bool = False,
) -> list[dict]:
    """Async version: returns list of dicts with joined battery + eval
    context and anomaly classification per notification row."""
    n, unit = _range_pair(last)
    where = [f"timestamp >= datetime('now', '-{n} {unit}')"]
    params: list = []
    if event:
        where.append("event = ?")
        params.append(event)
    if direction:
        where.append("direction = ?")
        params.append(direction)
    sql = (
        "SELECT id, timestamp, direction, event, details, details_json "
        "FROM automation_notification_log "
        f"WHERE {' AND '.join(where)} ORDER BY timestamp DESC"
    )
    async with conn.execute(sql, params) as cur:
        raw = [dict(r) for r in await cur.fetchall()]

    out: list[dict] = []
    for r in raw:
        row: dict[str, Any] = {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "direction": r["direction"],
            "event": r["event"],
            "details": r["details"],
        }
        ctx: dict = {}
        if r["direction"] == "SENT":
            ctx = await _correlate_async(conn, r["timestamp"])
            for k in ("soc", "batt_kw", "grid_kw", "imp_c", "exp_c", "mode", "run_status", "rule"):
                v = ctx.get(k)
                row[k] = round(v, 2) if isinstance(v, float) else v
            a = classify_anomaly(r["event"], ctx.get("soc"), ctx.get("batt_kw"), ctx.get("grid_kw"))
            row["anomaly"] = a.kind if a else None
            row["anomaly_reason"] = a.reason if a else None
        else:
            for k in ("soc", "batt_kw", "grid_kw", "imp_c", "exp_c", "mode", "run_status", "rule"):
                row[k] = None
            row["anomaly"] = None
            row["anomaly_reason"] = None
        if anomalies_only and not row["anomaly"]:
            continue
        out.append(row)
    return out


async def _correlate_async(conn, ts: str) -> dict:
    async with conn.execute(
        "SELECT data_json FROM gateway_metrics WHERE timestamp "
        "BETWEEN datetime(?, '-60 seconds') AND datetime(?, '+60 seconds') "
        "ORDER BY ABS(strftime('%s',timestamp)-strftime('%s',?)) LIMIT 1",
        (ts, ts, ts),
    ) as cur:
        gm = await cur.fetchone()
    async with conn.execute(
        "SELECT action, rule_name, soc_pct, import_c_kwh, export_c_kwh, trigger_category "
        "FROM pricing_eval_log WHERE ts BETWEEN datetime(?, '-120 seconds') AND datetime(?, '+120 seconds') "
        "ORDER BY ABS(strftime('%s',ts)-strftime('%s',?)) LIMIT 1",
        (ts, ts, ts),
    ) as cur:
        ae = await cur.fetchone()
    d = json.loads(gm["data_json"]) if gm else {}
    return {
        "soc":        d.get("battery_soc"),
        "batt_kw":    d.get("battery_kw"),
        "grid_kw":    d.get("grid_kw"),
        "mode":       d.get("operating_mode"),
        "run_status": d.get("run_status_desc"),
        "imp_c":      ae["import_c_kwh"] if ae else None,
        "exp_c":      ae["export_c_kwh"] if ae else None,
        "rule":       ae["rule_name"] if ae else None,
        "action":     ae["action"] if ae else None,
    }


async def stale_window_async(conn, *, last: str = "24h") -> list[dict]:
    """gateway_metrics rows matching the Batch I stale-window signature."""
    n, unit = _range_pair(last)
    async with conn.execute(
        f"SELECT id, timestamp, data_json FROM gateway_metrics "
        f"WHERE timestamp >= datetime('now', '-{n} {unit}') ORDER BY timestamp DESC"
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data_json"])
        except Exception:
            continue
        soc = d.get("battery_soc")
        bkw = d.get("battery_kw")
        gkw = d.get("grid_kw")
        rst = d.get("run_status_desc") or ""
        if soc == 0 and bkw == 0 and gkw == 0 and (rst == "Standby" or str(d.get("run_status")) == "0"):
            out.append({
                "id": r["id"], "timestamp": r["timestamp"],
                "mode": d.get("operating_mode") or "", "run_status": rst,
                "soc": soc, "batt_kw": bkw, "grid_kw": gkw,
                "suspected_stale": True,
            })
    return out


async def cooldowns_async(conn, *, rule: Optional[str] = None, last: Optional[str] = None) -> list[dict]:
    where = []
    params: list = []
    if rule:
        where.append("rule_id = ?")
        params.append(rule)
    if last:
        n, unit = _range_pair(last)
        where.append(f"created_at >= datetime('now', '-{n} {unit}')")
    sql = "SELECT gateway_serial, rule_id, expires_at, ignored_count, created_at FROM notification_cooldown"
    if where:
        sql += f" WHERE {' AND '.join(where)}"
    sql += " ORDER BY created_at DESC"
    async with conn.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def shadow_decisions_async(
    conn, *, last: str = "24h", reason: Optional[str] = None,
) -> list[dict]:
    """Batch P (2026-07-30): return SD decisions where the engine deferred
    to an external controller (VPP / Modbus / Manual). Populated whenever
    pricing_eval_log.shadow_reason IS NOT NULL. This is the audit-trail
    complement to Batches G/M's SUPPRESSED entries — those log what SD
    wanted to NOTIFY but didn't; this logs what SD wanted to DECIDE but
    didn't act on."""
    n, unit = _range_pair(last)
    where = [
        "shadow_reason IS NOT NULL",
        f"ts >= datetime('now', '-{n} {unit}')",
    ]
    params: list = []
    if reason:
        where.append("shadow_reason = ?")
        params.append(reason)
    sql = (
        "SELECT ts, gateway_id, trigger_category, action, preset_name, rule_name, "
        "       reason, soc_pct, import_c_kwh, export_c_kwh, execution_status, shadow_reason "
        "FROM pricing_eval_log "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC"
    )
    async with conn.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def multi_emit_async(conn, *, last: str = "7d", min_count: int = 2) -> list[dict]:
    n, unit = _range_pair(last)
    async with conn.execute(
        f"SELECT timestamp, COUNT(*) as n FROM automation_notification_log "
        f"WHERE timestamp >= datetime('now', '-{n} {unit}') AND direction='SENT' "
        f"GROUP BY timestamp HAVING n >= ? ORDER BY timestamp DESC",
        (min_count,),
    ) as cur:
        clusters = [dict(r) for r in await cur.fetchall()]
    out = []
    for c in clusters:
        async with conn.execute(
            "SELECT event FROM automation_notification_log "
            "WHERE timestamp = ? AND direction='SENT' ORDER BY id",
            (c["timestamp"],),
        ) as cur:
            events = [r["event"] for r in await cur.fetchall()]
        out.append({
            "timestamp": c["timestamp"],
            "count": c["n"],
            "events": events,
        })
    return out
