#!/usr/bin/env python3
"""fhai-audit — diagnostic queries against the live SQLite for notification /
stale-window / cooldown / correlation analysis.

Extracted from ad-hoc audit sessions (2026-07-20 through 2026-07-29) that
kept surfacing bugs by joining `automation_notification_log`,
`gateway_metrics`, `amber_eval_log`, `pending_approvals`, and
`notification_cooldown`. See Batch O.

Usage:

    python scripts/audit_notifications.py notifications --last 72h
    python scripts/audit_notifications.py notifications --event export_bonus
    python scripts/audit_notifications.py notifications --anomalies-only
    python scripts/audit_notifications.py stale-window --last 24h
    python scripts/audit_notifications.py cooldowns
    python scripts/audit_notifications.py multi-emit --last 7d

Outputs (default = table). Add `--json` or `--csv` for machine-readable.
Add `--anomalies-only` to filter suspicious rows AND return non-zero exit
code when any anomaly is present (for CI / cron / regression detection).

DB path resolution order:
    1. --db  CLI arg
    2. FHAI_DB env var
    3. /data/config.db  (in-container default)
    4. ./data/config.db (repo checkout)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


# ── DB resolution ──────────────────────────────────────────────────────────

def _resolve_db(cli_arg: Optional[str]) -> Path:
    candidates = [
        cli_arg,
        os.environ.get("FHAI_DB"),
        "/data/config.db",
        "./data/config.db",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    raise SystemExit(
        f"fhai-audit: cannot find config.db. Tried: {[c for c in candidates if c]!r}. "
        f"Set FHAI_DB=/path/to/config.db or pass --db."
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ── Range parsing (--last 72h, --last 7d, --last 30m) ─────────────────────

_RANGE_RE = re.compile(r"^(\d+)\s*([hmds])$")


def _parse_range(s: str) -> str:
    """Return a SQLite datetime-modifier fragment, e.g. '-72 hours'."""
    m = _RANGE_RE.match(s.strip().lower())
    if not m:
        raise SystemExit(f"fhai-audit: bad --last value {s!r} — expected e.g. '72h', '24h', '7d', '30m'")
    n, unit = int(m.group(1)), m.group(2)
    unit_map = {"h": "hours", "d": "days", "m": "minutes", "s": "seconds"}
    return f"-{n} {unit_map[unit]}"


# ── Output ─────────────────────────────────────────────────────────────────

def _emit(rows: list[dict], fmt: str, cols: Optional[list[str]] = None) -> None:
    if not rows:
        if fmt == "json":
            print("[]")
        elif fmt == "csv":
            pass  # empty
        else:
            print("(no results)")
        return
    if cols is None:
        cols = list(rows[0].keys())
    if fmt == "json":
        print(json.dumps(rows, indent=2, default=str))
    elif fmt == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    else:
        widths = {c: max(len(c), max((len(str(r.get(c, "") or "")) for r in rows), default=0)) for c in cols}
        widths = {c: min(widths[c], 40) for c in widths}
        header = "  ".join(c.ljust(widths[c]) for c in cols)
        print(header)
        print("─" * len(header))
        for r in rows:
            line = "  ".join((str(r.get(c, "") or "")[:widths[c]]).ljust(widths[c]) for c in cols)
            print(line)


# ── Queries ────────────────────────────────────────────────────────────────

@dataclass
class Anomaly:
    kind: str
    reason: str


def _correlate_notif(conn: sqlite3.Connection, ts: str) -> dict:
    """Return {soc, batt_kw, grid_kw, mode, run_status, imp_c, exp_c, rule}
    joined against the nearest gateway_metrics + amber_eval_log rows."""
    gm = conn.execute(
        "SELECT data_json FROM gateway_metrics WHERE timestamp "
        "BETWEEN datetime(?, '-60 seconds') AND datetime(?, '+60 seconds') "
        "ORDER BY ABS(strftime('%s',timestamp)-strftime('%s',?)) LIMIT 1",
        (ts, ts, ts)
    ).fetchone()
    ae = conn.execute(
        "SELECT action, rule_name, soc_pct, import_c_kwh, export_c_kwh, trigger_category "
        "FROM amber_eval_log WHERE ts BETWEEN datetime(?, '-120 seconds') AND datetime(?, '+120 seconds') "
        "ORDER BY ABS(strftime('%s',ts)-strftime('%s',?)) LIMIT 1",
        (ts, ts, ts)
    ).fetchone()
    d = json.loads(gm["data_json"]) if gm else {}
    return {
        "soc":       d.get("battery_soc"),
        "batt_kw":   d.get("battery_kw"),
        "grid_kw":   d.get("grid_kw"),
        "mode":      d.get("operating_mode"),
        "run_status": d.get("run_status_desc"),
        "imp_c":     ae["import_c_kwh"] if ae else None,
        "exp_c":     ae["export_c_kwh"] if ae else None,
        "rule":      ae["rule_name"] if ae else None,
        "action":    ae["action"] if ae else None,
    }


def _classify_anomaly(event: str, ctx: dict) -> Optional[Anomaly]:
    """Apply Batch O.2 event-stale rules to a notification's live context;
    returns Anomaly if the send shouldn't have happened."""
    if event not in ("force_charge", "spike", "export_bonus", "negative_export"):
        return None
    soc = ctx.get("soc")
    batt = ctx.get("batt_kw")
    grid = ctx.get("grid_kw")
    FLOW = 0.200
    if event in ("force_charge", "spike"):
        if isinstance(soc, (int, float)) and soc >= 90:
            return Anomaly("battery-full", f"soc={soc:.1f}% ≥ 90% but charge-prompt fired")
        if isinstance(batt, (int, float)) and batt < -FLOW:
            return Anomaly("already-charging", f"battery_kw={batt:+.2f}kW (charging) but charge-prompt fired")
        if isinstance(grid, (int, float)) and grid < -FLOW:
            return Anomaly("exporting-vs-charge", f"grid_kw={grid:+.2f}kW (exporting) but charge-prompt fired")
    else:  # export_bonus / negative_export
        if isinstance(soc, (int, float)) and soc <= 20:
            return Anomaly("battery-empty", f"soc={soc:.1f}% ≤ 20% but export-prompt fired")
        if isinstance(grid, (int, float)) and grid <= -FLOW:
            return Anomaly("already-exporting", f"grid_kw={grid:+.2f}kW (exporting) — redundant export prompt")
        if isinstance(batt, (int, float)) and batt < -FLOW:
            return Anomaly("charging-vs-export", f"battery_kw={batt:+.2f}kW (charging) but export-prompt fired")
    return None


def cmd_notifications(conn: sqlite3.Connection, args) -> int:
    win = _parse_range(args.last)
    where = ["timestamp >= datetime('now', ?)"]
    params: list = [win]
    if args.event:
        where.append("event = ?")
        params.append(args.event)
    if args.direction:
        where.append("direction = ?")
        params.append(args.direction)
    sql = "SELECT id, timestamp, direction, event, details, details_json FROM automation_notification_log " \
          f"WHERE {' AND '.join(where)} ORDER BY timestamp DESC"
    cur = conn.execute(sql, params)
    raw = [dict(r) for r in cur]
    out: list[dict] = []
    any_anomaly = False
    for r in raw:
        row = {
            "id": r["id"], "timestamp": r["timestamp"], "dir": r["direction"],
            "event": r["event"],
        }
        ctx = _correlate_notif(conn, r["timestamp"]) if r["direction"] == "SENT" else {}
        for k in ("soc", "batt_kw", "grid_kw", "imp_c", "exp_c", "mode", "rule"):
            v = ctx.get(k)
            if isinstance(v, float):
                row[k] = round(v, 2)
            else:
                row[k] = v
        anomaly = _classify_anomaly(r["event"], ctx) if r["direction"] == "SENT" else None
        row["anomaly"] = anomaly.kind if anomaly else ""
        row["reason"] = anomaly.reason if anomaly else ""
        row["details"] = (r["details"] or "")[:80]
        if anomaly:
            any_anomaly = True
        if args.anomalies_only and not anomaly:
            continue
        out.append(row)
    cols = ["timestamp", "dir", "event", "soc", "batt_kw", "grid_kw",
            "imp_c", "exp_c", "mode", "rule", "anomaly", "reason"]
    if args.format == "json":
        cols = None  # dump everything including details/id
    _emit(out, args.format, cols)
    # Exit code for CI/cron
    if args.anomalies_only and any_anomaly:
        return 2
    return 0


def cmd_stale_window(conn: sqlite3.Connection, args) -> int:
    """Sample gateway_metrics rows matching the Batch I stale-window signature."""
    win = _parse_range(args.last)
    cur = conn.execute(
        "SELECT id, timestamp, data_json FROM gateway_metrics "
        "WHERE timestamp >= datetime('now', ?) ORDER BY timestamp DESC",
        (win,),
    )
    out = []
    for r in cur:
        try:
            d = json.loads(r["data_json"])
        except Exception:
            continue
        soc = d.get("battery_soc")
        bkw = d.get("battery_kw")
        gkw = d.get("grid_kw")
        opm = d.get("operating_mode") or ""
        rst = d.get("run_status_desc") or ""
        if soc == 0 and bkw == 0 and gkw == 0 and (rst == "Standby" or str(d.get("run_status")) == "0"):
            out.append({
                "id": r["id"], "timestamp": r["timestamp"],
                "mode": opm, "run_status": rst, "soc": soc,
                "batt_kw": bkw, "grid_kw": gkw,
                "suspected_stale": "yes",
            })
    _emit(out, args.format, ["timestamp", "mode", "run_status", "soc", "batt_kw", "grid_kw", "suspected_stale"])
    return 0 if not out else (2 if args.anomalies_only else 0)


def cmd_cooldowns(conn: sqlite3.Connection, args) -> int:
    where = []
    params: list = []
    if args.rule:
        where.append("rule_id = ?")
        params.append(args.rule)
    if args.last:
        where.append("created_at >= datetime('now', ?)")
        params.append(_parse_range(args.last))
    sql = "SELECT gateway_serial, rule_id, expires_at, ignored_count, created_at " \
          "FROM notification_cooldown"
    if where:
        sql += f" WHERE {' AND '.join(where)}"
    sql += " ORDER BY created_at DESC"
    rows = [dict(r) for r in conn.execute(sql, params)]
    _emit(rows, args.format, ["created_at", "gateway_serial", "rule_id", "expires_at", "ignored_count"])
    return 0


def cmd_shadow(conn: sqlite3.Connection, args) -> int:
    """Batch P (2026-07-30): SD decisions where the engine deferred to
    an external controller (VPP / Modbus / Manual)."""
    win = _parse_range(args.last)
    where = ["shadow_reason IS NOT NULL", "ts >= datetime('now', ?)"]
    params: list = [win]
    if args.reason:
        where.append("shadow_reason = ?")
        params.append(args.reason)
    sql = (
        "SELECT ts, action, preset_name, rule_name, soc_pct, import_c_kwh, "
        "       export_c_kwh, execution_status, shadow_reason "
        "FROM amber_eval_log "
        f"WHERE {' AND '.join(where)} ORDER BY ts DESC"
    )
    rows = [dict(r) for r in conn.execute(sql, params)]
    _emit(rows, args.format, [
        "ts", "action", "rule_name", "soc_pct", "import_c_kwh",
        "export_c_kwh", "shadow_reason",
    ])
    return 0 if not rows else (2 if args.anomalies_only else 0)


def cmd_multi_emit(conn: sqlite3.Connection, args) -> int:
    """Find timestamps where multiple SENT notifications share the same second."""
    win = _parse_range(args.last)
    cur = conn.execute(
        "SELECT timestamp, COUNT(*) as n FROM automation_notification_log "
        "WHERE timestamp >= datetime('now', ?) AND direction='SENT' "
        "GROUP BY timestamp HAVING n >= ? ORDER BY timestamp DESC",
        (win, args.min),
    )
    clusters = [dict(r) for r in cur]
    out = []
    for c in clusters:
        events = conn.execute(
            "SELECT event FROM automation_notification_log "
            "WHERE timestamp = ? AND direction='SENT' ORDER BY id",
            (c["timestamp"],),
        ).fetchall()
        out.append({
            "timestamp": c["timestamp"], "count": c["n"],
            "events": ", ".join(r["event"] for r in events),
        })
    _emit(out, args.format, ["timestamp", "count", "events"])
    if args.anomalies_only and out:
        return 2
    return 0


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="fhai-audit",
        description="Diagnostic queries against the FHAI SQLite for notification / stale-window / cooldown analysis.")
    p.add_argument("--db", help="Path to config.db (else FHAI_DB env, else auto-detect)")
    p.add_argument("--format", choices=("table", "json", "csv"), default="table")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_n = sub.add_parser("notifications", help="Correlated notification audit")
    p_n.add_argument("--last", default="72h", help="Time window (72h / 7d / 30m — default 72h)")
    p_n.add_argument("--event", help="Filter by event type (force_charge, export_bonus, ...)")
    p_n.add_argument("--direction", help="Filter by direction (SENT / RECEIVED / SUPPRESSED / ERROR)")
    p_n.add_argument("--anomalies-only", action="store_true",
                     help="Only rows that look wrong (charge while full, export while charging, etc). "
                          "Non-zero exit if any found — usable in cron/CI.")

    p_s = sub.add_parser("stale-window", help="gateway_metrics rows matching Batch I stale-window signature")
    p_s.add_argument("--last", default="24h")
    p_s.add_argument("--anomalies-only", action="store_true")

    p_c = sub.add_parser("cooldowns", help="notification_cooldown rows (evidence of expired-and-ignored pendings)")
    p_c.add_argument("--rule", help="Filter by rule_id (spike, export_bonus, force_charge, ...)")
    p_c.add_argument("--last", help="Time window (7d / 24h — default all-time)")

    p_sh = sub.add_parser("shadow", help="SD decisions deferred due to external control (VPP/Modbus/Manual)")
    p_sh.add_argument("--last", default="24h")
    p_sh.add_argument("--reason", help="Filter by shadow_reason (e.g. vpp_active)")
    p_sh.add_argument("--anomalies-only", action="store_true",
                      help="Non-zero exit if any shadow rows found (cron/CI)")

    p_m = sub.add_parser("multi-emit", help="Timestamps where 2+ SENT notifications share the same second")
    p_m.add_argument("--last", default="7d")
    p_m.add_argument("--min", type=int, default=2, help="Minimum events per cluster (default 2)")
    p_m.add_argument("--anomalies-only", action="store_true")

    args = p.parse_args(argv)
    conn = _connect(_resolve_db(args.db))

    return {
        "notifications": cmd_notifications,
        "stale-window":  cmd_stale_window,
        "cooldowns":     cmd_cooldowns,
        "shadow":        cmd_shadow,
        "multi-emit":    cmd_multi_emit,
    }[args.cmd](conn, args)


if __name__ == "__main__":
    sys.exit(main())
