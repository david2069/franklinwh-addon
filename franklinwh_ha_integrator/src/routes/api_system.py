from src.app_state import get_app_state
import logging
import sys
import os
import importlib.metadata
import uuid
from collections import deque
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

# ── Server-push notification queue ────────────────────────────────────────────
# Lightweight in-memory deque. Background tasks push; frontend pops on poll.
# Max 50 entries — oldest are dropped automatically.
_notification_queue: deque = deque(maxlen=50)

def push_notification(type: str, **kwargs) -> None:
    """Push a server-side notification into the queue for the next frontend poll."""
    _notification_queue.append({"id": str(uuid.uuid4()), "type": type, "ts": datetime.utcnow().isoformat(), **kwargs})


@router.get("/system/notifications/pop")
async def pop_notifications(type: Optional[str] = None):
    """
    Drain pending server-side notifications (optionally filtered by type).
    Frontend polls this endpoint to collect background-task completion events.
    Returns a list and clears the matching entries atomically.
    """
    if type:
        matches = [n for n in _notification_queue if n.get("type") == type]
        for m in matches:
            try:
                _notification_queue.remove(m)
            except ValueError:
                pass
    else:
        matches = list(_notification_queue)
        _notification_queue.clear()
    return matches


class ToastLogRequest(BaseModel):
    level: str          # 'critical' | 'warn'
    msg: str
    detail: Optional[str] = None


@router.post("/system/toast-log")
async def toast_log(req: ToastLogRequest):
    """
    Server-side log entry for client-fired warn/critical toast notifications.
    Provides audit traceability for significant UI events per docs/notifications.md.

    Called automatically by addToast() in app.js — never call directly from Python.
    Level mapping: critical → ERROR, warn → WARNING.
    """
    safe_level = req.level.lower()
    if safe_level not in ("critical", "warn"):
        return {"ok": False, "reason": "Only warn/critical levels are logged server-side"}

    prefix = "TOAST-CRITICAL" if safe_level == "critical" else "TOAST-WARN"
    entry = f"[{prefix}] {req.msg}"
    if req.detail:
        entry += f" — {req.detail}"

    if safe_level == "critical":
        logger.error(entry)
    else:
        logger.warning(entry)

    return {"ok": True}



@router.get("/system/dependencies")
async def get_dependencies():
    """Inventory all active Python dependencies, versions, and installation dates."""
    deps = []
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"]
            if not name:
                continue

            version = dist.version
            info_path = getattr(dist, '_path', None)

            install_date = None
            if info_path and os.path.exists(info_path):
                mtime = os.path.getmtime(info_path)
                install_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

            deps.append({
                "name": name,
                "version": version,
                "summary": dist.metadata.get("Summary", ""),
                "installed_at": install_date or "Unknown",
                "homepage": dist.metadata.get("Home-page", "")
            })
        except Exception:
            continue

    deps.sort(key=lambda x: x["name"].lower())
    return {
        "python_version": sys.version.split(" ")[0],
        "dependencies": deps
    }


@router.get("/system/audit-logs")
async def get_audit_logs(limit: int = 100, offset: int = 0, event: str | None = None,
                         search: str | None = None):
    """Retrieve the recent admin audit log events.

    `event` and `search` exist because this table is dominated by routine
    scheduler activity — a rule firing every five minutes accounts for most of
    it — so an unfiltered window of the last N rows shows that and nothing
    else, however large N is.
    """
    from src.services.db import get_admin_audit_logs, count_admin_audit_logs
    return {
        "rows": await get_admin_audit_logs(limit=limit, offset=offset, category=event, search=search),
        "matched": await count_admin_audit_logs(category=event, search=search),
        "offset": offset,
        "limit": limit,
    }


@router.get("/system/security-audit-logs")
async def get_security_audit(limit: int = 200, offset: int = 0, search: str | None = None):
    """The security audit trail — logins, credential changes, MFA, TLS.

    Kept on its own endpoint, not folded into audit-logs: mixing it with
    scheduler activity is what made it impossible to find.
    """
    from src.services.db import get_security_audit_logs, count_security_audit_logs
    return {
        "rows": await get_security_audit_logs(limit=limit, offset=offset, search=search),
        "matched": await count_security_audit_logs(search=search),
        "offset": offset,
        "limit": limit,
    }


@router.get("/system/processes")
async def get_processes():
    """Returns asyncio task statuses and global Uvicorn process metrics."""
    import psutil
    import asyncio

    process = psutil.Process()
    mem_info = process.memory_info()
    cpu_percent = process.cpu_percent(interval=None)
    uptime = int(datetime.now().timestamp() - process.create_time())

    metrics = {
        "cpu_percent": round(cpu_percent, 1),
        "memory_mb": round(mem_info.rss / 1024 / 1024, 1),
        "uptime_seconds": uptime
    }

    tasks = []
    for t in asyncio.all_tasks():
        name = t.get_name()
        if "Task-" in name and len(name) < 15:
            continue

        tasks.append({
            "name": name,
            "status": "Done" if t.done() else ("Cancelled" if t.cancelled() else "Running"),
        })

    tasks.sort(key=lambda x: x["name"])

    return {
        "metrics": metrics,
        "tasks": tasks
    }


@router.post("/system/processes/{name}/restart")
async def restart_process(name: str):
    """Safely stop and restart a targeted background component."""
    from src.services.db import log_admin_audit

    state = get_app_state()
    registry = state.get("registry")
    publisher = state.get("publisher")
    listener = state.get("listener")

    restarted = False

    if name == "mqtt-publisher" and publisher:
        await publisher.stop()
        publisher.start()
        restarted = True
    elif name == "mqtt-command-listener" and listener:
        await listener.stop()
        listener.start()
        restarted = True
    elif name.startswith("gateway-"):
        short_id = name.split("-")[1]
        if registry and registry.is_running(short_id):
            restarted = await registry.restart_gateway(short_id)

    if not restarted:
        return {"ok": False, "error": f"Task {name!r} not found or un-restartable."}

    await log_admin_audit(event="task_restarted", source="ui", details=f"Manually restarted component: {name}")
    return {"ok": True, "message": f"Successfully restarted {name}"}


@router.post("/system/restart")
async def restart_system():
    """Immediately stop the application, relying on the external process manager to respawn it."""
    from src.services.db import log_admin_audit
    await log_admin_audit(event="system_restarted", source="ui", details="Triggered full application restart.")

    import asyncio
    import os
    async def delayed_exit():
        await asyncio.sleep(1)
        os._exit(1)

    asyncio.create_task(delayed_exit())
    return {"ok": True, "message": "Restart signal sent. The integration will reboot momentarily."}

@router.get("/system/instance_label")
async def get_instance_label():
    """Retrieve the global custom Application Instance UID (defaults to FHAI)."""
    from src.services.db import get_config_value
    label = await get_config_value("fhai_instance_label", "FHAI")
    return {"instance_label": label}

@router.post("/system/instance_label")
async def set_instance_label(payload: dict):
    """Set the Application Instance UID."""
    from src.services.db import set_config_value, log_admin_audit
    label = payload.get("instance_label", "FHAI").strip()
    if not label:
        label = "FHAI"
    await set_config_value("fhai_instance_label", label)
    await log_admin_audit(event="config_change", source="ui", details=f"Updated Instance Label to {label}")
    return {"ok": True, "instance_label": label}


# ── Feature Flags ─────────────────────────────────────────────────────────────

MODULE_TAB_MAP = {
    "weather": ["weather"],
    "solar": ["solar_setup"],
    # smart_dispatch canonical slug (BD-01 rename); legacy 'amber' alias
    # accepted by /api/system/features via _canonicalize_tab so existing
    # DB rows with suppressed_tabs="amber" keep suppressing the tab.
    "smart_dispatch": ["smart_dispatch", "amber"],
    "dynamic_pricing": ["dynamic_pricing", "pricing"],
    "automations": ["automations"],
    "metrics": ["metrics"],
    "security": ["security"]
}
ALL_MODULES = list(MODULE_TAB_MAP.keys())

# Tabs that may never be suppressed (GH #37).
#
# Suppressing `support` hides the Setup Features panel — the only UI for
# un-suppressing anything — so the setting locks itself in and recovery means
# editing the database by hand. `dashboard` is the landing tab: hiding it
# leaves a user staring at an empty shell with no obvious way forward.
#
# Same principle the Security tab already applies to roles and dashboards
# ("cannot be changed for the active account to prevent lockouts"), and the
# same reason the sibling Modbus Bridge marks its Dashboard and Settings
# modules CORE.
CORE_TABS = {"support", "dashboard"}


def _reject_core_tab_suppression(tabs: str | None) -> None:
    """Raise if a caller tries to suppress a tab that can never be hidden."""
    if not tabs:
        return
    requested = {t.strip().lower() for t in tabs.split(",") if t.strip()}
    blocked = requested & CORE_TABS
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot suppress core tab(s): {', '.join(sorted(blocked))}. "
                f"'support' hosts the panel that un-suppresses tabs, so hiding it "
                f"would require editing the database to undo."
            ),
        )

class FeaturesRequest(BaseModel):
    metrics_enabled: Optional[bool] = None
    metrics_retention_days: Optional[int] = None
    backup_enabled: Optional[bool] = None
    backup_retention_days: Optional[int] = None
    backup_interval_hours: Optional[int] = None
    prune_vacuum_enabled: Optional[bool] = None
    toast_auto_dismiss: Optional[bool] = None
    toast_dismiss_delay_seconds: Optional[int] = None
    automations_pin_enabled: Optional[bool] = None
    suppressed_tabs: Optional[str] = None
    enabled_modules: Optional[list[str]] = None


@router.get("/system/features")
async def get_features():
    """Return all feature flags and retention settings with env-aware defaults."""
    from src.services.db import get_config_value
    from src.config.environment import detect_environment

    env = detect_environment()

    # metrics_enabled: None in DB means "auto" — Docker=True, HA_Addon=False
    raw = await get_config_value("metrics_enabled", None)
    if raw is None:
        metrics_enabled = env != "ha_addon"
        metrics_auto = True
    else:
        metrics_enabled = str(raw).lower() in ("true", "1")
        metrics_auto = False

    enabled_modules = await get_config_value("enabled_modules", None)
    if enabled_modules is None:
        # Legacy compatibility check: deduce from suppressed_tabs
        suppressed = await get_config_value("suppressed_tabs", "")
        suppressed_set = {t.strip().lower() for t in suppressed.split(",") if t.strip()}
        if suppressed_set:
            enabled_modules = []
            for mod, tabs in MODULE_TAB_MAP.items():
                if not any(t.lower() in suppressed_set for t in tabs):
                    enabled_modules.append(mod)
        else:
            enabled_modules = ALL_MODULES

    # Persona gates (v0.6.0, GH #11) — server-side computed tab visibility
    # driven by the auto-detected persona matrix. Populated once persona
    # detection has run; before that, all gates are True (permissive) so
    # existing installs don't lose UI mid-upgrade.
    persona_gates = await _compute_persona_gates()

    return {
        "environment": env,
        "metrics_enabled": metrics_enabled,
        "metrics_enabled_auto": metrics_auto,
        "metrics_retention_days": int(await get_config_value("metrics_retention_days", 14)),
        "backup_enabled": str(await get_config_value("backup_enabled", "true")).lower() in ("true", "1"),
        "backup_retention_days": int(await get_config_value("backup_retention_days", 7)),
        "backup_interval_hours": int(await get_config_value("backup_interval_hours", 6)),
        "prune_vacuum_enabled": str(await get_config_value("prune_vacuum_enabled", "true")).lower() in ("true", "1"),
        "toast_auto_dismiss": str(await get_config_value("toast_auto_dismiss", "true")).lower() in ("true", "1"),
        "toast_dismiss_delay_seconds": int(await get_config_value("toast_dismiss_delay_seconds", 10)),
        "automations_pin_enabled": str(await get_config_value("automations_pin_enabled", "false")).lower() in ("true", "1"),
        "suppressed_tabs": await get_config_value("suppressed_tabs", ""),
        "enabled_modules": enabled_modules,
        "persona_gates": persona_gates,
        "last_backup_at": await get_config_value("last_backup_at", None),
        "last_prune_at": await get_config_value("last_prune_at", None),
        "last_prune_rows": await get_config_value("last_prune_rows", None),
    }


async def _compute_persona_gates() -> dict[str, bool]:
    """Compute per-tab persona gates. Every entry defaults to True — Phase B
    issues (#19, #20, #21, #23, ...) add per-tab logic here as they land.

    Bypass: if persona detection has not run yet (`persona.detected_at` is
    null), every gate is True. Existing installs and fresh installs before
    the first startup detect see the full UI, preventing silent tab loss.
    """
    from src.services.db import get_config_value
    from src.services import persona as _persona
    import aiosqlite
    from src.services import db as _db

    gates: dict[str, bool] = {"solar_setup": True, "dynamic_pricing": True}

    if await get_config_value("persona.detected_at", None) is None:
        return gates

    # Enumerate configured gateway serials from app_config (persona.gateway_type.{serial})
    serials: list[str] = []
    try:
        async with aiosqlite.connect(_db.get_db_path(), timeout=30.0) as conn:
            async with conn.execute(
                "SELECT key FROM app_config WHERE key LIKE 'persona.gateway_type.%'"
            ) as cur:
                async for row in cur:
                    serials.append(row[0].removeprefix("persona.gateway_type."))
    except Exception:
        return gates

    # Solar Setup — visible if ANY gateway has solar OR enphase, else hidden
    solar_anywhere = False
    for s in serials:
        if await _persona.is_solar_present(s):
            solar_anywhere = True
            break
        if bool(await _persona._read_with_override(f"persona.enphase_present.{s}", False)):
            solar_anywhere = True
            break
    gates["solar_setup"] = solar_anywhere

    # Pricing tab — visible when tariff_type in {dynamic, tou}, hidden for
    # flat-rate + off-grid (nothing meaningful to display). Wraps
    # persona.is_pricing_available() which reads with override precedence.
    # BD-11 / #19.
    gates["dynamic_pricing"] = await _persona.is_pricing_available()

    return gates


@router.post("/system/features")
async def update_features(req: FeaturesRequest):
    """Update one or more feature flags / retention settings."""
    from src.services.db import set_config_value, log_admin_audit
    changes = []

    if req.metrics_enabled is not None:
        await set_config_value("metrics_enabled", str(req.metrics_enabled).lower())
        changes.append(f"metrics_enabled={req.metrics_enabled}")
    if req.metrics_retention_days is not None:
        if not 1 <= req.metrics_retention_days <= 365:
            raise HTTPException(status_code=400, detail="metrics_retention_days must be 1-365")
        await set_config_value("metrics_retention_days", str(req.metrics_retention_days))
        changes.append(f"metrics_retention_days={req.metrics_retention_days}")
    if req.backup_enabled is not None:
        await set_config_value("backup_enabled", str(req.backup_enabled).lower())
        changes.append(f"backup_enabled={req.backup_enabled}")
    if req.backup_retention_days is not None:
        if not 1 <= req.backup_retention_days <= 90:
            raise HTTPException(status_code=400, detail="backup_retention_days must be 1-90")
        await set_config_value("backup_retention_days", str(req.backup_retention_days))
        changes.append(f"backup_retention_days={req.backup_retention_days}")
    if req.backup_interval_hours is not None:
        if not 1 <= req.backup_interval_hours <= 24:
            raise HTTPException(status_code=400, detail="backup_interval_hours must be 1-24")
        await set_config_value("backup_interval_hours", str(req.backup_interval_hours))
        changes.append(f"backup_interval_hours={req.backup_interval_hours}")
    if req.prune_vacuum_enabled is not None:
        await set_config_value("prune_vacuum_enabled", str(req.prune_vacuum_enabled).lower())
        changes.append(f"prune_vacuum_enabled={req.prune_vacuum_enabled}")
    if req.toast_auto_dismiss is not None:
        await set_config_value("toast_auto_dismiss", str(req.toast_auto_dismiss).lower())
        changes.append(f"toast_auto_dismiss={req.toast_auto_dismiss}")
    if req.toast_dismiss_delay_seconds is not None:
        if not 1 <= req.toast_dismiss_delay_seconds <= 300:
            raise HTTPException(status_code=400, detail="toast_dismiss_delay_seconds must be 1-300")
        await set_config_value("toast_dismiss_delay_seconds", str(req.toast_dismiss_delay_seconds))
        changes.append(f"toast_dismiss_delay_seconds={req.toast_dismiss_delay_seconds}")
    if req.automations_pin_enabled is not None:
        await set_config_value("automations_pin_enabled", str(req.automations_pin_enabled).lower())
        changes.append(f"automations_pin_enabled={req.automations_pin_enabled}")

    # Bidirectional enabled_modules / suppressed_tabs sync
    # Validate before ANY write, so a rejected request cannot leave
    # enabled_modules and suppressed_tabs half-applied and disagreeing.
    _reject_core_tab_suppression(req.suppressed_tabs)
    if req.enabled_modules is not None or req.suppressed_tabs is not None:
        if req.enabled_modules is not None and req.suppressed_tabs is not None:
            # Both provided (normal UI save), just save both exactly as they are
            await set_config_value("enabled_modules", req.enabled_modules)
            changes.append(f"enabled_modules={req.enabled_modules}")
            cleaned_tabs = ",".join(t.strip() for t in req.suppressed_tabs.split(",") if t.strip())
            await set_config_value("suppressed_tabs", cleaned_tabs)
            changes.append(f"suppressed_tabs={cleaned_tabs}")
        elif req.enabled_modules is not None:
            # Load existing suppressed_tabs to merge
            existing_suppressed = await get_config_value("suppressed_tabs", "")
            existing_set = {t.strip().lower() for t in existing_suppressed.split(",") if t.strip()}
            await set_config_value("enabled_modules", req.enabled_modules)
            changes.append(f"enabled_modules={req.enabled_modules}")

            # Determine which module tabs should be suppressed
            disabled_modules = [m for m in ALL_MODULES if m not in req.enabled_modules]
            module_suppressed = []
            for mod in disabled_modules:
                module_suppressed.extend(MODULE_TAB_MAP[mod])

            # Keep only non-optional-module tabs from existing_set + the disabled module tabs
            all_module_tabs = {t.lower() for tabs in MODULE_TAB_MAP.values() for t in tabs}
            non_module_suppressed = [t for t in existing_set if t not in all_module_tabs]
            
            final_suppressed = list(set(non_module_suppressed + module_suppressed))
            cleaned_tabs = ",".join(final_suppressed)
            await set_config_value("suppressed_tabs", cleaned_tabs)
            changes.append(f"suppressed_tabs={cleaned_tabs}")
        else:
            cleaned_tabs = ",".join(t.strip() for t in req.suppressed_tabs.split(",") if t.strip())
            await set_config_value("suppressed_tabs", cleaned_tabs)
            changes.append(f"suppressed_tabs={cleaned_tabs}")

            # Derive enabled_modules
            suppressed_set = {t.strip().lower() for t in cleaned_tabs.split(",") if t.strip()}
            enabled_modules = []
            for mod, tabs in MODULE_TAB_MAP.items():
                if not any(t.lower() in suppressed_set for t in tabs):
                    enabled_modules.append(mod)
            await set_config_value("enabled_modules", enabled_modules)
            changes.append(f"enabled_modules={enabled_modules}")

    if changes:
        await log_admin_audit("feature_flags_updated", "ui", ", ".join(changes))
    return {"ok": True, "changed": changes}


# ── Backup Management ─────────────────────────────────────────────────────────

@router.get("/system/backups")
async def list_backups():
    """List all backup archives with size and creation date."""
    from src.config.manager import AppConfig
    cfg = AppConfig.load()
    backup_dir = cfg.data_dir / "backups"

    archives = []
    if backup_dir.exists():
        for f in sorted(backup_dir.glob("backup_*.zip"), reverse=True):
            try:
                stat = f.stat()
                archives.append({
                    "filename": f.name,
                    "size_bytes": stat.st_size,
                    "size_mb": round(stat.st_size / 1024 / 1024, 1),
                    "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            except OSError:
                pass

    total_bytes = sum(a["size_bytes"] for a in archives)
    return {
        "count": len(archives),
        "total_size_mb": round(total_bytes / 1024 / 1024, 1),
        "archives": archives,
    }


@router.post("/system/backups/run")
async def run_backup_now():
    """Trigger an immediate backup regardless of schedule."""
    from src.services.db import log_admin_audit

    state = get_app_state()
    backup_manager = state.get("backup_manager")
    if not backup_manager:
        raise HTTPException(status_code=500, detail="Backup manager not initialised")

    await log_admin_audit("manual_backup_triggered", "ui", "User triggered manual backup")
    ok = await backup_manager.execute_backup()
    if ok:
        return {"ok": True, "message": "Backup completed successfully"}
    raise HTTPException(status_code=500, detail="Backup failed — check logs for details")


@router.delete("/system/backups/{filename}")
async def delete_backup(filename: str):
    """Delete a specific backup archive by filename."""
    from src.config.manager import AppConfig
    from src.services.db import log_admin_audit

    if not filename.startswith("backup_") or not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Invalid backup filename")

    cfg = AppConfig.load()
    target = cfg.data_dir / "backups" / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Backup {filename!r} not found")

    target.unlink()
    await log_admin_audit("backup_deleted", "ui", f"Deleted backup: {filename}")
    return {"ok": True, "deleted": filename}


@router.get("/system/backups/{filename}/probe")
async def probe_backup(filename: str):
    """
    Inspect a backup archive without restoring it.
    Returns gateway count, credential count, metrics row count, and the backup timestamp.
    Safe/read-only — no side effects.
    """
    import zipfile
    import sqlite3 as _sqlite3
    import tempfile
    import shutil

    if not filename.startswith("backup_") or not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Invalid backup filename")

    from src.config.manager import AppConfig
    cfg = AppConfig.load()
    target = cfg.data_dir / "backups" / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Backup {filename!r} not found")

    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="fhai_probe_")
        with zipfile.ZipFile(str(target), "r") as z:
            if "config.db" not in z.namelist():
                raise HTTPException(status_code=422, detail="Backup does not contain config.db")
            z.extract("config.db", tmp_dir)

        db_path = f"{tmp_dir}/config.db"
        conn = _sqlite3.connect(db_path, timeout=10)
        try:
            gw = conn.execute("SELECT short_id, full_serial, name FROM gateways").fetchall()
            cred_count = conn.execute("SELECT COUNT(*) FROM gateway_credentials").fetchone()[0]
            metrics_count = 0
            try:
                metrics_count = conn.execute("SELECT COUNT(*) FROM gateway_metrics").fetchone()[0]
            except Exception:
                pass
            schema = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
            schema_version = schema[0] if schema else None
        finally:
            conn.close()

        stat = target.stat()
        return {
            "ok": True,
            "filename": filename,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "gateways": [{"short_id": r[0], "full_serial": r[1], "name": r[2]} for r in gw],
            "gateway_count": len(gw),
            "credential_count": cred_count,
            "metrics_rows": metrics_count,
            "schema_version": schema_version,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Probe failed: {e}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/system/backups/{filename}/restore")
async def restore_backup(filename: str):
    """
    Restore config.db from a named backup archive.
    Saves current DB to /data/config.db.pre_restore_bak first (one-slot safety net).
    Then extracts config.db from the zip and restarts the app.
    WARNING: all data written since the backup was taken will be lost.
    """
    import zipfile
    import shutil

    if not filename.startswith("backup_") or not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Invalid backup filename")

    from src.config.manager import AppConfig
    from src.services.db import log_admin_audit, get_db_path
    cfg = AppConfig.load()
    backup_path = cfg.data_dir / "backups" / filename
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail=f"Backup {filename!r} not found")

    db_path = get_db_path()

    try:
        # 1. Probe first to make sure the zip is valid and has a gateway
        with zipfile.ZipFile(str(backup_path), "r") as z:
            if "config.db" not in z.namelist():
                raise HTTPException(status_code=422, detail="Backup does not contain config.db")

        # 2. Save current DB as a one-slot safety net
        pre_restore = cfg.data_dir / "config.db.pre_restore_bak"
        shutil.copy2(str(db_path), str(pre_restore))
        logger.info(f"Restore: saved current DB to {pre_restore}")

        # 3. Extract backup into live DB path
        with zipfile.ZipFile(str(backup_path), "r") as z:
            with z.open("config.db") as src, open(str(db_path), "wb") as dst:
                shutil.copyfileobj(src, dst)
        logger.info(f"Restore: extracted {filename} -> {db_path}")

        await log_admin_audit(
            "db_restored", "ui",
            f"Restored database from backup: {filename}. Previous DB saved to config.db.pre_restore_bak"
        )

        # 4. Restart the app so it picks up the restored DB
        import asyncio, os
        async def _delayed_exit():
            await asyncio.sleep(1.5)
            os._exit(1)
        asyncio.create_task(_delayed_exit())

        return {
            "ok": True,
            "message": f"Database restored from {filename}. Application is restarting…",
            "backup": filename,
            "safety_copy": "config.db.pre_restore_bak",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")


# ── Database Browser ──────────────────────────────────────────────────────────

@router.get("/system/db/tables")
async def db_tables():
    """List all DB tables with row counts, size estimates, and last-updated timestamps."""
    import aiosqlite
    from src.services.db import get_db_path

    db_path = get_db_path()
    tables = []

    async with aiosqlite.connect(db_path) as conn:
        # Get all user tables
        all_tables = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )

        # Per-table byte sizes via dbstat (available in SQLite ≥ 3.7.17)
        size_map: dict[str, int] = {}
        try:
            stat_rows = await conn.execute_fetchall(
                "SELECT name, SUM(payload) FROM dbstat WHERE aggregate=TRUE GROUP BY name"
            )
            size_map = {r[0]: (r[1] or 0) for r in stat_rows}
        except Exception:
            pass  # dbstat not available — sizes will be 0

        for (name,) in all_tables:
            # Row count
            try:
                count_rows = await conn.execute_fetchall(f'SELECT COUNT(*) FROM "{name}"')
                count = count_rows[0][0] if count_rows else 0
            except Exception:
                count = -1

            # Last updated — check for created_at or updated_at column
            last_updated = None
            try:
                cols = [r[1] for r in await conn.execute_fetchall(f'PRAGMA table_info("{name}")')]
                ts_col = None
                for candidate in ("created_at", "updated_at", "timestamp", "ts"):
                    if candidate in cols:
                        ts_col = candidate
                        break
                if ts_col and count > 0:
                    ts_row = await conn.execute_fetchall(
                        f'SELECT MAX("{ts_col}") FROM "{name}"'
                    )
                    last_updated = ts_row[0][0] if ts_row else None
            except Exception:
                pass

            tables.append({
                "name": name,
                "row_count": count,
                "size_bytes": size_map.get(name, 0),
                "last_updated": last_updated,
            })

    try:
        size_bytes = db_path.stat().st_size
    except OSError:
        size_bytes = 0

    return {
        "tables": tables,
        "db_size_mb": round(size_bytes / 1024 / 1024, 1),
    }



@router.get("/system/db/tables/{table_name}")
async def db_table_rows(
    table_name: str,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginated row browser for a specific DB table."""
    import aiosqlite
    from src.services.db import get_db_path

    # Validate table name (alphanumeric + underscores only, prevent injection)
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    if not all(c in allowed for c in table_name.lower()):
        raise HTTPException(status_code=400, detail="Invalid table name")

    db_path = get_db_path()

    async with aiosqlite.connect(db_path) as conn:
        exists = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        )
        if not exists:
            raise HTTPException(status_code=404, detail=f"Table {table_name!r} not found")

        count_rows = await conn.execute_fetchall(f'SELECT COUNT(*) FROM "{table_name}"')
        total = count_rows[0][0] if count_rows else 0

        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f'SELECT * FROM "{table_name}" ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (limit, offset)
        )
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description] if cursor.description else []

        return {
            "table": table_name,
            "total": total,
            "limit": limit,
            "offset": offset,
            "columns": columns,
            "rows": [list(r) for r in rows],
        }


# ── Metrics Prune ─────────────────────────────────────────────────────────────

@router.post("/system/db/prune")
async def run_prune_now():
    """Manually trigger a metrics prune + VACUUM. Takes a safety backup first."""
    from src.services.db import log_admin_audit

    state = get_app_state()
    backup_manager = state.get("backup_manager")
    if not backup_manager:
        raise HTTPException(status_code=500, detail="Backup manager not initialised")

    await log_admin_audit("manual_prune_triggered", "ui", "User triggered manual metrics prune + VACUUM")
    result = await backup_manager.execute_prune()
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Prune failed"))
    return result


@router.get("/system/db/prune-preview")
async def prune_preview():
    """
    Dry-run: show exactly what rows execute_prune() would delete without committing.
    Returns per-table eligible row counts plus the protected and telemetry table lists.
    Safe to call at any time — read-only.
    """
    from src.services.db import log_admin_audit

    state = get_app_state()
    backup_manager = state.get("backup_manager")
    if not backup_manager:
        raise HTTPException(status_code=500, detail="Backup manager not initialised")

    result = await backup_manager.prune_dry_run()
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Dry-run failed"))
    return result


@router.get("/system/db/wal-status")
async def wal_status():
    """
    Return WAL file size, main DB size, and checkpoint lag information.
    Useful for monitoring runaway WAL growth (a secondary issue from April 2026 incident).
    """
    import aiosqlite
    from src.services.db import get_db_path

    db_path = get_db_path()
    wal_path = db_path.parent / (db_path.name + "-wal")
    shm_path = db_path.parent / (db_path.name + "-shm")

    def _sizes():
        try:
            db_size = db_path.stat().st_size if db_path.exists() else 0
            wal_size = wal_path.stat().st_size if wal_path.exists() else 0
            shm_size = shm_path.stat().st_size if shm_path.exists() else 0
            return db_size, wal_size, shm_size
        except OSError:
            return 0, 0, 0

    import asyncio
    db_size, wal_size, shm_size = await asyncio.to_thread(_sizes)

    # Get page-level checkpoint stats from SQLite
    wal_pages = 0
    checkpointed_pages = 0
    journal_mode = "unknown"
    try:
        async with aiosqlite.connect(str(db_path)) as conn:
            row = await conn.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)")
            if row:
                # (busy, log, checkpointed)
                wal_pages = row[0][1] if row[0][1] is not None else 0
                checkpointed_pages = row[0][2] if row[0][2] is not None else 0
            jm = await conn.execute_fetchall("PRAGMA journal_mode")
            journal_mode = jm[0][0] if jm else "unknown"
    except Exception:
        pass

    return {
        "journal_mode": journal_mode,
        "db_size_mb": round(db_size / 1024 / 1024, 1),
        "wal_size_mb": round(wal_size / 1024 / 1024, 1),
        "shm_size_mb": round(shm_size / 1024 / 1024, 1),
        "total_size_mb": round((db_size + wal_size + shm_size) / 1024 / 1024, 1),
        "wal_pages": wal_pages,
        "checkpointed_pages": checkpointed_pages,
        "wal_lag_pages": max(0, wal_pages - checkpointed_pages),
        "healthy": wal_size < 50 * 1024 * 1024,  # flag if WAL > 50 MB
    }



class DbQueryRequest(BaseModel):
    sql: str
    limit: int = 200  # max rows returned to UI


@router.post("/system/db/query")
async def db_query(req: DbQueryRequest):
    """
    Execute a read-only SQL query against the app database.
    Only SELECT, PRAGMA, and EXPLAIN statements are permitted.
    All query executions are logged to the admin audit log.
    Returns: { columns, rows, row_count, truncated, elapsed_ms }
    """
    import time
    import aiosqlite
    from src.services.db import get_db_path, log_admin_audit

    sql = req.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="SQL query is empty")

    # Safety guard — only allow read-only statements
    first_word = sql.split()[0].upper() if sql.split() else ""
    ALLOWED = {"SELECT", "PRAGMA", "EXPLAIN", "WITH"}
    if first_word not in ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"Only SELECT/PRAGMA/EXPLAIN/WITH statements are permitted. Got: {first_word}"
        )

    # Secondary guard — block any write keywords anywhere in the query
    BLOCKED_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH", "DETACH"}
    upper_sql = sql.upper()
    for kw in BLOCKED_KEYWORDS:
        if kw in upper_sql:
            raise HTTPException(status_code=400, detail=f"Statement contains blocked keyword: {kw}")

    await log_admin_audit("db_query", "ui", f"SQL: {sql[:200]}")

    limit = min(req.limit, 500)
    db_path = get_db_path()

    try:
        t0 = time.monotonic()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # Apply LIMIT if not already present (safety net)
            exec_sql = sql
            if first_word == "SELECT" and "LIMIT" not in upper_sql:
                exec_sql = f"{sql} LIMIT {limit}"

            async with conn.execute(exec_sql) as cursor:
                rows_raw = await cursor.fetchmany(limit)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        rows = [list(r) for r in rows_raw]
        total_fetched = len(rows)

        return {
            "columns": columns,
            "rows": rows,
            "row_count": total_fetched,
            "truncated": total_fetched >= limit,
            "elapsed_ms": elapsed_ms,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query error: {e}")


# ── Local usage telemetry (GH #40 phase 1 — collection only) ──────────────────
# Nothing here transmits. The outbox holds the exact payload that a later send
# phase would post, so the UI shows what was built rather than re-deriving it —
# two renderings of the same thing eventually disagree, and the one the user
# reads must be the one that would leave.

@router.get("/system/telemetry")
async def telemetry_status():
    """Consent state, install id, and every rollup collected so far."""
    from src.services import telemetry
    from src.services import db as _db

    enabled = await telemetry.is_enabled()

    rows = []
    async with _db.get_db() as conn:
        async with conn.execute(
            "SELECT id, period, status, error, created_at, sent_at, "
            "length(payload_json) AS size FROM telemetry_outbox "
            "ORDER BY period DESC LIMIT 400"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    return {
        "ok": True,
        "enabled": enabled,
        # Stated plainly: it links this install's rollups over time.
        "install_id": await telemetry.install_id() if enabled else None,
        "transmits": False,
        "note": "Collection only. Nothing is sent anywhere in this version.",
        "excluded_prefixes": list(telemetry.EXCLUDED_PREFIXES),
        "rollups": rows,
    }


@router.get("/system/telemetry/{outbox_id}")
async def telemetry_payload(outbox_id: int):
    """The stored payload, verbatim — what the UI renders and what downloads."""
    from src.services import db as _db
    import json as _json

    async with _db.get_db() as conn:
        async with conn.execute(
            "SELECT id, period, status, payload_json FROM telemetry_outbox WHERE id = ?",
            (outbox_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No such rollup")

    return {
        "ok": True,
        "id": row["id"],
        "period": row["period"],
        "status": row["status"],
        "payload": _json.loads(row["payload_json"]),
    }


@router.post("/system/telemetry/collect")
async def telemetry_collect_now(force: bool = False):
    """Build a rollup now — the dry run. Still transmits nothing.

    `force` collects even when telemetry is disabled, so the payload can be
    inspected before consenting to anything. That is the point: opt-in means
    more when it is "send this?" with the JSON on screen than "allow usage
    reporting?" in the abstract.
    """
    from src.services import telemetry
    return await telemetry.collect(force=force)


@router.post("/system/telemetry/consent")
async def telemetry_consent(enabled: bool):
    """Opt in or out. Disabling stops collection, not merely transmission."""
    from src.services.db import set_config_value, log_admin_audit
    from src.services import telemetry

    await set_config_value(telemetry.CONSENT_KEY, bool(enabled))
    try:
        await log_admin_audit("TELEMETRY_CONSENT", f"telemetry_enabled={bool(enabled)}")
    except Exception:
        pass
    return {"ok": True, "enabled": bool(enabled)}
