"""
Phase 8 — Diagnostics APIs: health check, startup logs, API performance metrics.

Routes:
  GET /api/health/detail    — full health check (gateway + MQTT + DB status)
  GET /api/logs/startup     — paginated startup log from DB
  GET /api/metrics          — Cloud API performance summary per gateway
  GET /api/metrics/raw      — raw api_performance rows (limited)
"""
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from src.app_state import get_app_state
from src.services import db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["diagnostics"])


# ── Detailed health check ─────────────────────────────────────

@router.get("/health/detail")
async def health_detail():
    """
    Full health check: per-gateway status, MQTT broker, DB file size, app uptime.
    Used by the Health tab.
    """
    state = get_app_state()
    registry = state.get("registry")
    publisher = state.get("publisher")
    config = state.get("config")
    db_path_str = state.get("db_path", "")

    # DB size
    db_size_bytes = 0
    try:
        db_size_bytes = Path(db_path_str).stat().st_size if db_path_str else 0
    except OSError:
        pass

    # Gateways
    gw_statuses = registry.get_all_status() if registry else []
    running = sum(1 for g in gw_statuses if g.get("poll_status") in ("ok", "starting", "retrying"))
    errored = sum(1 for g in gw_statuses if g.get("poll_status") == "error")

    return {
        "app": {
            "version": state.get("version", "unknown"),
            "env": state.get("env", "unknown"),
        },
        "gateways": {
            "total": len(gw_statuses),
            "running": running,
            "errored": errored,
            "statuses": gw_statuses,
        },
        "mqtt": {
            "connected": publisher.connected if publisher else False,
            "broker": f"{publisher.host}:{publisher.port}" if publisher else "not_configured",
            "messages_published": publisher.messages_published if publisher else 0,
            "reconnect_count": publisher.reconnect_count if publisher else 0,
            "queue_depth": publisher.queue_depth if publisher else 0,
            "last_error": publisher.last_error if publisher else None,
        },
        "database": {
            "path": db_path_str,
            "size_bytes": db_size_bytes,
            "size_kb": round(db_size_bytes / 1024, 1),
        },
        "config": config.safe_dict() if config else {},
    }


# ── Startup log ───────────────────────────────────────────────

@router.get("/logs/startup")
async def startup_logs(limit: int = Query(default=50, le=200)):
    """Return the most recent startup log entries (all phases)."""
    rows = await db.get_startup_logs(limit=limit)
    return {"count": len(rows), "entries": rows}


@router.get("/logs/recent")
async def recent_logs(
    limit: int = Query(default=200, le=500),
    level: str = Query(default=None),
):
    """Return recent runtime log records from the in-memory buffer.
    
    Captures gateway poll errors, MQTT events, and other runtime activity.
    ?level=ERROR filters to errors only.
    """
    from src.services.log_buffer import get_recent
    records = get_recent(limit=limit, level=level)
    return {"count": len(records), "entries": records}


@router.get("/logs/file")
async def file_logs(
    limit: int = Query(default=1000, le=5000),
    offset: int = Query(default=0, ge=0, description="Skip this many matching entries — for paging"),
    level: str = Query(default=None),
    source: str = Query(default=None),
    search: str = Query(default=None),
    since_hours: int = Query(default=None, description="Only return entries from the last N hours (0 or absent = all)"),
):
    """Parse the persistent application log file, returning filtered entries and stat totals.

    Paged. The UI used to ask for 5000 entries and render every one, which is
    why the Logs tab took seconds to appear on an instance with a real log. The
    summary counts still cover the whole file — they are the point of the totals
    cards — but only one page of rows is serialised and sent.

    `matched` is the count after filters and before paging, so the client can
    say "page 3 of 40" without fetching the other 39.
    """
    import re
    from datetime import datetime, timedelta
    from src.config.manager import AppConfig
    cfg = AppConfig.load()
    log_path = cfg.data_dir / cfg.log_filename

    records = []
    matched = 0
    summary = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}

    if not log_path.exists():
        return {"summary": summary, "entries": records, "matched": 0, "offset": offset, "limit": limit}

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    level_filter = level.upper() if level else None
    cutoff = (datetime.now() - timedelta(hours=since_hours)) if since_hours else None

    # Process newest first
    for line in reversed(lines):
        parts = line.split(" | ", 3)
        if len(parts) < 4:
            continue

        ts, lvl, src, msg = parts[0], parts[1], parts[2], parts[3]

        # Accumulate total summary ignoring filters
        if lvl in summary:
            summary[lvl] += 1

        # Counted before paging: `matched` must reflect the filter, not the page.

        # Time-window filter — parse timestamp, skip if older than cutoff
        if cutoff:
            try:
                ts_clean = ts.strip().replace("T", " ")[:19]
                row_dt = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
                if row_dt < cutoff:
                    continue
            except ValueError:
                pass  # unparseable timestamp — include the row

        if level_filter and lvl != level_filter:
            continue
        if source and source.lower() not in src.lower():
            continue
        if search and search.lower() not in msg.lower():
            continue

        matched += 1
        if matched <= offset or len(records) >= limit:
            continue

        records.append({
            "ts": ts,
            "level": lvl,
            "source": src,
            "message": msg
        })

    return {
        "summary": summary,
        "entries": records,
        "matched": matched,
        "offset": offset,
        "limit": limit,
    }


@router.get("/logs")
async def logs(limit: int = 20):
    """Alias — returns startup log entries."""
    rows = await db.get_startup_logs(limit=limit)
    return rows


# ── API performance metrics ───────────────────────────────────

@router.get("/metrics/api")
async def api_performance(
    short_id: str = Query(default=None, description="Filter by gateway short_id"),
    limit: int = Query(default=100, le=500),
):
    """Return raw Cloud API call records with basic per-gateway summary."""
    rows = await db.get_api_performance(short_id=short_id, limit=limit)
    # Compute summary
    if rows:
        latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
        summary = {
            "total_calls": len(rows),
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "min_latency_ms": min(latencies) if latencies else None,
            "error_count": sum(1 for r in rows if r.get("status") != "ok"),
        }
    else:
        summary = {
            "total_calls": 0, "avg_latency_ms": None,
            "max_latency_ms": None, "min_latency_ms": None, "error_count": 0,
        }
    return {"summary": summary, "rows": rows}


@router.get("/metrics")
async def metrics(limit: int = 20):
    """Return recent gateway telemetry metrics."""
    rows = await db.get_recent_metrics(limit=limit)
    return rows

@router.get("/metrics/cloud")
async def cloud_metrics(short_id: str = Query(description="Gateway short_id")):
    """Return live franklinwh-cloud library native metrics."""
    from fastapi import HTTPException
    state = get_app_state()
    registry = state.get("registry")
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")
        
    gw = registry.get_gateway(short_id)
    if not gw or not gw._client:
        raise HTTPException(status_code=404, detail="Gateway client offline")
        
    c_metrics = gw._client.get_metrics()
    edge = gw._client.edge_tracker.snapshot()
    
    return {
        "uptime": {
            "total_requests": c_metrics.get("total_api_calls", 0),
            "uptime_seconds": c_metrics.get("uptime_s", 0),
            "token_refreshes": c_metrics.get("token_refresh_count", 0),
            "retries": c_metrics.get("retry_count", 0)
        },
        "timing": {
            "avg_ms": c_metrics.get("avg_response_time_s", 0) * 1000,
            "min_ms": c_metrics.get("min_response_time_s", 0) * 1000,
            "max_ms": c_metrics.get("max_response_time_s", 0) * 1000
        },
        "endpoints": c_metrics.get("calls_by_endpoint", {}),
        "methods": c_metrics.get("calls_by_method", {}),
        "errors": c_metrics.get("errors_by_type", {}),
        "edge": {
            "last_pop": edge.get("current_pop"),
            "requests": edge.get("total_cf_requests", 0),
            "cache_hits": edge.get("cache_hits", 0),
            "transitions": edge.get("edge_transitions", 0),
            "pop_counts": edge.get("pop_distribution", {})
        }
    }


@router.get("/metrics/cloud/history")
async def cloud_metrics_history(
    short_id: str = Query(..., description="Gateway short_id"),
    since_days: int = Query(default=1, description="1=today, 7=7d, 30=30d, 0=all history"),
):
    """Aggregate raw Cloud API library snapshots to construct a persistent timeline safe across restarts."""
    from datetime import datetime, timedelta
    # For all-time queries the 10k default silently drops weeks of history.
    # Pass a much larger limit; SQLite handles this efficiently via the indexed timestamp column.
    row_limit = 200000 if since_days == 0 else 10000
    rows = await db.get_api_edge_metrics(short_id=short_id, limit=row_limit)

    cutoff = (datetime.now() - timedelta(days=since_days)) if since_days > 0 else None
    filtered = []

    for row in rows:
        if cutoff:
            try:
                row_dt = datetime.strptime(row.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
                if row_dt < cutoff:
                    continue
            except ValueError:
                pass
        filtered.append(row)

    # Walk oldest to newest
    filtered = list(reversed(filtered))

    totals = {}
    prevs = {}

    def _process_dict(current_dict: dict, prefix=""):
        for k, v in current_dict.items():
            if isinstance(v, dict):
                _process_dict(v, prefix + k + ".")
            elif isinstance(v, (int, float)):
                full_key = prefix + k
                prev = prevs.get(full_key, 0)
                # Differential addition — if the current snapshot is lower than previous, a reboot occurred
                delta = (v - prev) if (v >= prev) else v
                totals[full_key] = totals.get(full_key, 0) + delta
                prevs[full_key] = v

    for row in filtered:
        try:
            metrics_obj = json.loads(row.get("metrics_json", "{}"))
            edge_obj = json.loads(row.get("edge_json", "{}"))
            
            # Formulate the hybrid dict
            combined = {
                "calls_by_endpoint": metrics_obj.get("calls_by_endpoint", {}),
                "calls_by_method": metrics_obj.get("calls_by_method", {}),
                # calls_by_python_method: new field from library v0.5+ (track_python_methods=True).
                # Defensive default {} required — old SQLite rows predate this field entirely.
                "calls_by_python_method": metrics_obj.get("calls_by_python_method", {}),
                "errors_by_type": metrics_obj.get("errors_by_type", {}),
                "total_api_calls": metrics_obj.get("total_api_calls", 0),
                "token_refresh_count": metrics_obj.get("token_refresh_count", 0),
                "retry_count": metrics_obj.get("retry_count", 0),
                "edge": {
                    "total_cf_requests": edge_obj.get("total_cf_requests", 0),
                    "cache_hits": edge_obj.get("cache_hits", 0),
                    "edge_transitions": edge_obj.get("edge_transitions", 0),
                    "pop_counts": edge_obj.get("pop_distribution", {})
                }
            }
            _process_dict(combined)
        except Exception as e:
            logger.warning(f"Failed to parse or aggregate snapshot row: {e}")

    # Reconstruct nested dict for UI consumption
    response = {
        "methods": {},           # HTTP verb counts — calls_by_method (GET/POST)
        "python_methods": {},    # Python wrapper counts — calls_by_python_method (get_stats, set_mode, …)
        "endpoints": {},
        "errors": {},
        "totals": {
            "api_calls": totals.get("total_api_calls", 0),
            "edge_requests": totals.get("edge.total_cf_requests", 0),
            "cache_hits": totals.get("edge.cache_hits", 0),
            "token_refreshes": totals.get("token_refresh_count", 0),
            "retries": totals.get("retry_count", 0),
            "snapshots_processed": len(filtered),
            "timeframe_days": since_days
        },
        "edge": {
            "requests": totals.get("edge.total_cf_requests", 0),
            "cache_hits": totals.get("edge.cache_hits", 0),
            "transitions": totals.get("edge.edge_transitions", 0),
            "pop_counts": {},
            "last_pop": "—"
        }
    }

    # Grab the last known PoP from the chronologically last valid snippet
    if filtered:
        try:
            last_edge = json.loads(filtered[-1].get("edge_json", "{}"))
            response["edge"]["last_pop"] = last_edge.get("current_pop", "—")
        except: pass
    
    for k, v in totals.items():
        if k.startswith("calls_by_method."):
            response["methods"][k.replace("calls_by_method.", "")] = v
        elif k.startswith("calls_by_python_method."):
            response["python_methods"][k.replace("calls_by_python_method.", "")] = v
        elif k.startswith("calls_by_endpoint."):
            response["endpoints"][k.replace("calls_by_endpoint.", "")] = v
        elif k.startswith("errors_by_type."):
            response["errors"][k.replace("errors_by_type.", "")] = v
        elif k.startswith("edge.pop_counts."):
            response["edge"]["pop_counts"][k.replace("edge.pop_counts.", "")] = v

    return response


@router.get("/metrics/edge")
async def edge_metrics(
    short_id: str = Query(default=None, description="Filter by gateway short_id"),
    limit: int = Query(default=50, le=200),
):
    """Return historical CloudFront edge transitions and API cache performance metrics."""
    rows = await db.get_api_edge_metrics(short_id=short_id, limit=limit)
    return {"count": len(rows), "entries": rows}


@router.get("/metrics/edge-pops")
async def edge_pop_distribution(
    short_id: str = Query(..., description="Gateway short_id"),
    since_days: int = Query(default=1, description="1=today, 7=7d, 30=30d, 0=all history"),
):
    """Aggregate per-PoP request counts from historical edge snapshots.

    Reads api_edge_metrics rows (stored as edge_json blobs), filters by time window,
    and sums pop_distribution across all matching rows. Returns data suitable for
    Leaflet circle-marker visualisation.
    """
    from datetime import datetime, timedelta
    row_limit = 200000 if since_days == 0 else 10000
    rows = await db.get_api_edge_metrics(short_id=short_id, limit=row_limit)

    cutoff = (datetime.now() - timedelta(days=since_days)) if since_days > 0 else None
    pop_totals: dict = {}
    last_pop = None
    row_count = 0

    # For transition tracking we need chronological order.
    # DB returns newest-first (DESC), so collect filtered rows then reverse.
    filtered: list[dict] = []

    for row in rows:
        if cutoff:
            try:
                row_dt = datetime.strptime(row.get("timestamp", "")[:19], "%Y-%m-%d %H:%M:%S")
                if row_dt < cutoff:
                    continue
            except ValueError:
                pass

        edge = json.loads(row.get("edge_json", "{}"))
        for pop_code, count in (edge.get("pop_distribution") or {}).items():
            pop_totals[pop_code] = pop_totals.get(pop_code, 0) + int(count)
        if edge.get("current_pop"):
            last_pop = edge["current_pop"]
        filtered.append({"timestamp": row.get("timestamp", ""), "current_pop": edge.get("current_pop")})
        row_count += 1

    # Reconstruct transitions: walk rows oldest→newest, detect current_pop changes
    transitions: list[dict] = []
    prev_pop: str | None = None
    prev_ts: datetime | None = None

    for r in reversed(filtered):
        curr_pop = r.get("current_pop")
        if not curr_pop:
            continue
        try:
            curr_ts = datetime.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        if prev_pop and curr_pop != prev_pop:
            duration_m = round((curr_ts - prev_ts).total_seconds() / 60, 1) if prev_ts else None
            transitions.append({
                "from":     prev_pop,
                "to":       curr_pop,
                "at":       curr_ts.strftime("%Y-%m-%d %H:%M"),
                "duration": duration_m,   # minutes at prev_pop before switch
            })

        prev_pop = curr_pop
        prev_ts = curr_ts

    # Most recent first, capped at 50
    transitions = list(reversed(transitions))[:50]

    return {
        "pop_counts":    pop_totals,
        "current_pop":   last_pop,
        "total_requests": sum(pop_totals.values()),
        "since_days":    since_days,
        "rows_scanned":  row_count,
        "transitions":   transitions,
    }



# ── Logging Settings ──────────────────────────────────────────

class LogSettingsRequest(BaseModel):
    log_level: str | None = None
    log_max_mb: int | None = None
    log_backups: int | None = None
    log_filename: str | None = None


@router.get("/logs/settings")
async def get_log_settings():
    """Return the current system log rotation parameters."""
    from src.config.manager import AppConfig
    cfg = AppConfig.load()
    
    return {
        "log_level": await db.get_config_value("log_level", cfg.log_level),
        "log_max_mb": int(await db.get_config_value("log_max_mb", cfg.log_max_mb)),
        "log_backups": int(await db.get_config_value("log_backups", cfg.log_backups)),
        "log_filename": await db.get_config_value("log_filename", cfg.log_filename),
    }


@router.patch("/logs/settings")
async def update_log_settings(req: LogSettingsRequest):
    """Store log settings in SQLite and apply log level dynamically."""
    if req.log_level:
        await db.set_config_value("log_level", req.log_level.upper())
        # Apply dynamically
        lvl = getattr(logging, req.log_level.upper(), logging.INFO)
        logging.getLogger().setLevel(lvl)
        if lvl != logging.DEBUG:
            logging.getLogger("franklinwh_cloud").setLevel(logging.INFO)
            
    if req.log_max_mb:
        await db.set_config_value("log_max_mb", str(req.log_max_mb))
    if req.log_backups:
        await db.set_config_value("log_backups", str(req.log_backups))
    if req.log_filename:
        await db.set_config_value("log_filename", req.log_filename)
        
    return {"status": "ok", "message": "Settings updated (Log changes may require a restart to apply rotation tweaks)"}


# ── Cloud Historical Metrics ──────────────────────────────────

@router.get("/metrics/timeline")
async def metrics_timeline(
    short_id: str = Query(..., description="Gateway short_id"),
    last: str = Query("24h", description="Window: 1h / 6h / 24h / 7d"),
):
    """Batch Q (2026-07-30): per-poll battery timeline for the Reporting
    tab's Power History panel (Bridge-inspired). Returns compact rows
    over the requested window:

      {ts, soc, batt_kw, grid_kw, home_kw, solar_kw,
       operating_mode, runtime_mode, run_status, is_stale}

    Chart-friendly: consumer plots battery/grid/home/solar as line
    series and soc on a secondary axis. Mode transitions render as
    annotation bands (via chartjs-plugin-annotation).
    """
    import re
    import json as _json
    from fastapi import HTTPException
    m = re.match(r"^(\d+)\s*([hmds])$", (last or "").strip().lower())
    if not m:
        raise HTTPException(status_code=400, detail=f"bad 'last' — expected e.g. '24h', '6h', '7d': got {last!r}")
    n, unit = int(m.group(1)), m.group(2)
    unit_map = {"h": "hours", "d": "days", "m": "minutes", "s": "seconds"}
    win = f"-{n} {unit_map[unit]}"

    async with db.get_db() as conn:
        async with conn.execute(
            "SELECT timestamp, data_json FROM gateway_metrics "
            "WHERE short_id = ? AND timestamp >= datetime('now', ?) "
            "ORDER BY timestamp",
            (short_id, win),
        ) as cur:
            raw = await cur.fetchall()

    out: list[dict] = []
    mode_transitions: list[dict] = []
    prev_runtime = None
    for r in raw:
        try:
            d = _json.loads(r["data_json"])
        except Exception:
            continue
        soc = d.get("battery_soc")
        rm = (d.get("mode") or {}).get("runtime_mode") or d.get("operating_mode") or ""
        row = {
            "ts":              r["timestamp"],
            "soc":             round(float(soc), 2) if isinstance(soc, (int, float)) else None,
            "batt_kw":         d.get("battery_kw"),
            "grid_kw":         d.get("grid_kw"),
            "home_kw":         d.get("home_load"),
            "solar_kw":        d.get("solar_kw"),
            "operating_mode":  d.get("operating_mode"),
            "runtime_mode":    rm,
            "run_status":      d.get("run_status_desc"),
            "is_stale":        bool(d.get("_fhai_suspect_stale_window")),
        }
        out.append(row)
        # Track mode transitions for annotation overlay
        if prev_runtime is not None and rm != prev_runtime:
            mode_transitions.append({
                "ts": r["timestamp"],
                "from": prev_runtime,
                "to":   rm,
            })
        prev_runtime = rm

    return {
        "ok": True,
        "short_id": short_id,
        "window": last,
        "count": len(out),
        "rows": out,
        "mode_transitions": mode_transitions,
    }


@router.get("/metrics/historical")
async def historical_metrics(
    short_id: str = Query(..., description="Gateway short_id"),
    report_type: int = Query(..., alias="type"),
    timeperiod: str = Query(...)
):
    """Bridge for the SDK get_power_details method (time series reporting)."""
    from fastapi import HTTPException
    state = get_app_state()
    registry = state.get("registry")
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")
        
    gw = registry.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail="Gateway not found in registry")
        
    try:
        client = await gw._get_or_create_client()
        if report_type == 1:
            data = await client.get_power_by_day(dayTime=timeperiod)
        else:
            data = await client.get_power_details(type=report_type, timeperiod=timeperiod)
        return {"metrics": data}
    except Exception as e:
        logger.error(f"Error fetching historical metrics for {short_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

