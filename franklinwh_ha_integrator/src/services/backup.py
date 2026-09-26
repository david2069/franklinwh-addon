"""
BackupManager — crash-safe automated backups of SQLite config and App Logs.
Uses SQLite online backup API (safe with WAL). Runs every N hours (default 6).
Runs a daily metrics prune with VACUUM to keep the DB from growing unboundedly.
Stores rotating archives in `data/backups/`.
"""
import asyncio
import logging
import time
import zipfile
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Defaults (overridable at runtime via app_config DB table)
DEFAULT_BACKUP_INTERVAL_HOURS = 6
DEFAULT_BACKUP_RETENTION_DAYS = 7
DEFAULT_METRICS_RETENTION_DAYS = 14
PRUNE_HOUR = 2  # daily prune at 2am local time

# ── Prune safety: ONLY these rolling telemetry tables are ever eligible for pruning.
# Any table NOT in this list is permanently protected, regardless of schema.
# Rationale: tables with a created_at column are NOT necessarily time-series —
# config tables (gateways, gateway_credentials, batteries) also carry created_at
# for auditing purposes but must never be age-pruned.
TELEMETRY_TABLES: tuple[str, ...] = (
    "gateway_metrics",
    "api_edge_metrics",
    "api_performance",
    "startup_log",
    "automation_history",
    # Append-only and scheduled, so it grows forever without this. Added with
    # the table rather than after someone notices the database is 5 GB — which
    # is how gateway_metrics and api_edge_metrics got there.
    "telemetry_counters",
    "telemetry_outbox",
)

# Informational only — these tables are explicitly never pruned.
PROTECTED_TABLES: tuple[str, ...] = (
    "gateways",
    "gateway_credentials",
    "batteries",
    "app_config",
    "schema_version",
    "bms_sessions",
    "admin_audit_log",
    "credential_audit_log",
    "sqlite_sequence",
)


# Telemetry tables never agreed on a name for their time column, so resolve it
# per table instead of assuming one. Requiring "created_at" meant every table
# here failed the check and was skipped: the prune deleted nothing at all from
# 2026-04-14 until 2026-08-29, and a 14-day retention quietly kept 5 months —
# 4.85 GB of config.db, and a 132 MB backup archive every 6 hours.
TELEMETRY_TIME_COLUMNS: tuple[str, ...] = ("created_at", "timestamp", "ts", "boot_at")


def _resolve_time_column(conn: sqlite3.Connection, table: str) -> str | None:
    """Return the table's age column, or None if it has no recognised one."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    for candidate in TELEMETRY_TIME_COLUMNS:
        if candidate in cols:
            return candidate
    return None


def _logical_db_bytes(conn: sqlite3.Connection) -> int:
    """Database size from SQLite's own page accounting.

    db_file.stat() is the wrong ruler here. The database runs in WAL mode with
    the live app holding other connections, so VACUUM's rebuilt pages sit in
    the -wal file and the main file is not truncated until a checkpoint that
    this connection's close() cannot force. Measuring the file immediately
    after VACUUM therefore reported "0.0 MB reclaimed" for a prune that had
    just freed 980 MB (2026-08-30). page_count * page_size is checkpoint
    independent and reflects the compaction straight away.
    """
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return page_count * page_size


def _utc_cutoff(retention_days: int) -> str:
    """Cutoff string matching how telemetry timestamps are actually stored.

    Rows are written as UTC "YYYY-MM-DD HH:MM:SS" (SQLite datetime('now')), and
    these predicates are string comparisons. datetime.now().isoformat() was
    wrong twice: the "T" separator sorts above " " so same-day rows compared
    incorrectly, and a local-time cutoff shifted the boundary by the UTC offset
    (10h on this install).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


class BackupManager:
    """
    Runs a crash-safe backup every backup_interval_hours (default 6).
    Runs a daily metrics prune + VACUUM at PRUNE_HOUR (default 2am).

    Uses sqlite3.backup() (online backup API) — safe for live WAL databases.
    Skips backup if DB fails integrity check (prevents archiving corrupt files).
    All config values (interval, retention, enabled) are read dynamically from DB.
    """

    def __init__(self, data_dir: Path, ttl_days: int = DEFAULT_BACKUP_RETENTION_DAYS, target_hour: int = 2):
        self.data_dir = data_dir
        self.backup_dir = self.data_dir / "backups"
        self.ttl_days = ttl_days
        self.target_hour = target_hour
        self._task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()
        self._last_prune_date: str | None = None  # YYYY-MM-DD

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._shutdown_event.clear()
        self._task = asyncio.create_task(self._backup_loop(), name="system-backup-manager")
        self._prune_task = asyncio.create_task(self._prune_loop(), name="system-metrics-pruner")
        logger.info(f"BackupManager started — Prune: daily at {PRUNE_HOUR:02d}:00")

    async def stop(self) -> None:
        self._shutdown_event.set()
        for task in (self._task, self._prune_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("BackupManager stopped")

    # ── Dynamic config helpers ───────────────────────────────────────────────

    async def _get_backup_interval_hours(self) -> int:
        try:
            from src.services.db import get_config_value
            return int(await get_config_value("backup_interval_hours", DEFAULT_BACKUP_INTERVAL_HOURS))
        except Exception:
            return DEFAULT_BACKUP_INTERVAL_HOURS

    async def _get_backup_retention_days(self) -> int:
        try:
            from src.services.db import get_config_value
            return int(await get_config_value("backup_retention_days", DEFAULT_BACKUP_RETENTION_DAYS))
        except Exception:
            return DEFAULT_BACKUP_RETENTION_DAYS

    async def _get_metrics_retention_days(self) -> int:
        try:
            from src.services.db import get_config_value
            return int(await get_config_value("metrics_retention_days", DEFAULT_METRICS_RETENTION_DAYS))
        except Exception:
            return DEFAULT_METRICS_RETENTION_DAYS

    async def _is_backup_enabled(self) -> bool:
        try:
            from src.services.db import get_config_value
            raw = await get_config_value("backup_enabled", "true")
            return str(raw).lower() in ("true", "1")
        except Exception:
            return True  # fail-open

    async def _is_vacuum_enabled(self) -> bool:
        try:
            from src.services.db import get_config_value
            raw = await get_config_value("prune_vacuum_enabled", "true")
            return str(raw).lower() in ("true", "1")
        except Exception:
            return True

    # ── Backup loop ──────────────────────────────────────────────────────────

    async def _backup_loop(self) -> None:
        interval_seconds = (await self._get_backup_interval_hours()) * 3600
        while not self._shutdown_event.is_set():
            try:
                remaining = interval_seconds
                while remaining > 0:
                    chunk = min(remaining, 3600)
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=chunk)
                        return
                    except asyncio.TimeoutError:
                        remaining -= chunk

                if await self._is_backup_enabled():
                    await self.execute_backup()
                else:
                    logger.debug("Backup skipped: backup_enabled=false")

                # Re-read interval in case it changed
                interval_seconds = (await self._get_backup_interval_hours()) * 3600

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"BackupManager backup loop error: {exc}")
                await asyncio.sleep(60)

    # ── Prune loop ──────────────────────────────────────────────────────────

    async def _prune_loop(self) -> None:
        """Run metrics prune once daily at PRUNE_HOUR (2am local)."""
        # Report the retention actually in force, not the module defaults. The
        # old line printed DEFAULT_* unconditionally, so a DB-configured 90-day
        # retention still logged "14d metrics" on every boot — a startup banner
        # stating the opposite of what the job would do.
        try:
            logger.info(
                f"BackupManager retention in force — "
                f"{await self._get_backup_retention_days()}d backups / "
                f"{await self._get_metrics_retention_days()}d metrics, "
                f"backup every {await self._get_backup_interval_hours()}h"
            )
        except Exception as exc:  # never let a log line stop the pruner starting
            logger.warning(f"BackupManager: could not read effective retention: {exc}")

        while not self._shutdown_event.is_set():
            try:
                now = datetime.now()
                target = now.replace(hour=PRUNE_HOUR, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                remaining = (target - now).total_seconds()

                while remaining > 0:
                    chunk = min(remaining, 3600)
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=chunk)
                        return
                    except asyncio.TimeoutError:
                        remaining -= chunk

                today = datetime.now().strftime("%Y-%m-%d")
                if self._last_prune_date != today:
                    await self.execute_prune()
                    self._last_prune_date = today

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"BackupManager prune loop error: {exc}")
                await asyncio.sleep(3600)

    # ── Core: execute_backup ─────────────────────────────────────────────────

    async def execute_backup(self) -> bool:
        """
        Crash-safe backup using SQLite online backup API.
        Safe to call while the app is actively writing to the database.
        Skips if DB integrity check fails (prevents archiving corrupt files).
        """
        retention_days = await self._get_backup_retention_days()

        def _do_backup():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            db_file = self.data_dir / "config.db"
            log_file = self.data_dir / "franklinwh.log"
            safe_copy = self.backup_dir / f"config_{timestamp}.db"

            if not db_file.exists():
                logger.warning("Backup skipped: config.db not found")
                return False

            # ── Step 1: Integrity check ───────────────────────────────────────────
            # Prevents filling disk with corrupt archives during crash loops.
            try:
                chk = sqlite3.connect(str(db_file))
                result = chk.execute("PRAGMA integrity_check").fetchone()
                chk.close()
                if result[0] != "ok":
                    logger.error(
                        f"Backup skipped: integrity_check='{result[0]}' — DB may be corrupt"
                    )
                    return False
            except Exception as chk_err:
                logger.error(f"Backup skipped: integrity check error: {chk_err}")
                return False

            # ── Step 2: Online backup (WAL-safe) ──────────────────────────────────
            # sqlite3.backup() is the ONLY safe way to snapshot a live WAL database.
            # shutil.copy2 on a live DB produces corrupt archives.
            try:
                src = sqlite3.connect(str(db_file))
                dst = sqlite3.connect(str(safe_copy))
                src.backup(dst, pages=500)
                dst.close()
                src.close()
            except Exception as bk_err:
                logger.error(f"Backup failed during online copy: {bk_err}")
                safe_copy.unlink(missing_ok=True)
                return False

            # ── Step 3: Zip ───────────────────────────────────────────────────────
            archive_path = self.backup_dir / f"backup_{timestamp}.zip"
            try:
                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as z:
                    z.write(safe_copy, "config.db")
                    if log_file.exists():
                        z.write(log_file, "franklinwh.log")
                size_mb = archive_path.stat().st_size // 1024 // 1024
                logger.info(f"Backup completed: backup_{timestamp}.zip ({size_mb} MB)")
            except Exception as zip_err:
                logger.error(f"Backup zip failed: {zip_err}")
                archive_path.unlink(missing_ok=True)
                return False
            finally:
                safe_copy.unlink(missing_ok=True)

            self._prune_old_backups(retention_days)
            return True

        ok = await asyncio.to_thread(_do_backup)
        if ok:
            try:
                from src.services.db import set_config_value
                await set_config_value("last_backup_at", datetime.now().isoformat())
            except Exception:
                pass
        return ok

    # ── Core: execute_prune ──────────────────────────────────────────────────

    async def prune_dry_run(self) -> dict:
        """
        Preview what execute_prune() would delete without committing anything.
        Returns per-table row counts and oldest row dates for each TELEMETRY_TABLE.
        Also returns the PROTECTED_TABLES list for UI display.
        Safe to call at any time — read-only, no side effects.
        """
        retention_days = await self._get_metrics_retention_days()
        db_file = self.data_dir / "config.db"
        if not db_file.exists():
            return {"ok": False, "error": "config.db not found"}

        def _do_dry_run():
            cutoff_str = _utc_cutoff(retention_days)
            try:
                conn = sqlite3.connect(str(db_file))
                preview = []
                for tbl in TELEMETRY_TABLES:
                    try:
                        # Check table exists
                        exists = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                        ).fetchone()
                        if not exists:
                            continue
                        tcol = _resolve_time_column(conn, tbl)
                        if tcol is None:
                            preview.append({
                                "table": tbl,
                                "skipped": "no recognised time column",
                                "rows_to_delete": 0,
                            })
                            continue
                        row = conn.execute(
                            f'SELECT COUNT(*), MIN("{tcol}"), MAX("{tcol}") '
                            f'FROM "{tbl}" WHERE "{tcol}" < ?', (cutoff_str,)
                        ).fetchone()
                        eligible = row[0] if row else 0
                        total_row = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()
                        total = total_row[0] if total_row else 0
                        preview.append({
                            "table": tbl,
                            "time_column": tcol,
                            "rows_to_delete": eligible,
                            "total_rows": total,
                            "oldest_eligible": row[1] if row else None,
                            "newest_eligible": row[2] if row else None,
                        })
                    except Exception as e:
                        preview.append({"table": tbl, "error": str(e)})
                conn.close()
                return {
                    "ok": True,
                    "retention_days": retention_days,
                    "cutoff": cutoff_str,
                    "preview": preview,
                    "protected_tables": list(PROTECTED_TABLES),
                    "telemetry_tables": list(TELEMETRY_TABLES),
                    "total_rows_to_delete": sum(p.get("rows_to_delete", 0) for p in preview),
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

        return await asyncio.to_thread(_do_dry_run)

    async def execute_prune(self) -> dict:
        """
        Prune telemetry rows older than metrics_retention_days.
        ONLY deletes from tables in TELEMETRY_TABLES — never touches config tables.
        Runs VACUUM after DELETE to reclaim disk space. Takes a backup first.

        Safety guarantee: gateways, gateway_credentials, batteries, app_config,
        schema_version, bms_sessions, admin_audit_log, credential_audit_log are
        NEVER touched by this method, even if they have a created_at column.
        """
        retention_days = await self._get_metrics_retention_days()
        do_vacuum = await self._is_vacuum_enabled()

        # Safety: backup before VACUUM (VACUUM rewrites the entire file)
        if await self._is_backup_enabled():
            logger.info("Prune: taking pre-prune backup as safety net...")
            await self.execute_backup()

        db_file = self.data_dir / "config.db"
        if not db_file.exists():
            return {"ok": False, "error": "config.db not found"}

        def _do_prune():
            cutoff_str = _utc_cutoff(retention_days)
            try:
                conn = sqlite3.connect(str(db_file))
                # Measure before the DELETEs so the figure covers the whole
                # prune, not just what VACUUM added on top of it.
                size_before = _logical_db_bytes(conn)

                # ── SAFE: only prune explicitly whitelisted telemetry tables ──
                # Never scan all tables for created_at — that approach deleted
                # gateway registrations (INCIDENT 2026-04-14). Whitelist only.
                pruned = {}
                for tbl in TELEMETRY_TABLES:
                    try:
                        exists = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                        ).fetchone()
                        if not exists:
                            continue
                        tcol = _resolve_time_column(conn, tbl)
                        if tcol is None:
                            # Loudly, not silently — a silent skip here is exactly
                            # what let a no-op prune look successful for months.
                            logger.warning(
                                f"Prune: table {tbl} has no recognised time column "
                                f"({', '.join(TELEMETRY_TIME_COLUMNS)}) — skipped."
                            )
                            continue
                        cur = conn.execute(
                            f'DELETE FROM "{tbl}" WHERE "{tcol}" < ?', (cutoff_str,)
                        )
                        if cur.rowcount > 0:
                            pruned[tbl] = cur.rowcount
                    except Exception as e:
                        logger.warning(f"Prune failed for table {tbl}: {e}")

                conn.commit()

                if do_vacuum:
                    logger.info("Prune: running VACUUM to reclaim disk space...")
                    conn.execute("VACUUM")
                    size_after = _logical_db_bytes(conn)
                    conn.close()
                    reclaimed_mb = round((size_before - size_after) / 1024 / 1024, 1)
                    logger.info(
                        f"Prune complete: {sum(pruned.values())} rows deleted across "
                        f"{list(pruned.keys()) or 'none'}, {reclaimed_mb} MB reclaimed. "
                        f"Protected tables untouched: {list(PROTECTED_TABLES)}"
                    )
                    return {
                        "ok": True, "pruned": pruned,
                        "reclaimed_mb": reclaimed_mb,
                        "retention_days": retention_days,
                        "protected_tables": list(PROTECTED_TABLES),
                    }
                else:
                    conn.close()
                    logger.info(
                        f"Prune complete (VACUUM disabled): {sum(pruned.values())} rows deleted. "
                        f"Protected tables untouched: {list(PROTECTED_TABLES)}"
                    )
                    return {
                        "ok": True, "pruned": pruned,
                        "reclaimed_mb": 0,
                        "retention_days": retention_days,
                        "protected_tables": list(PROTECTED_TABLES),
                    }

            except Exception as e:
                logger.error(f"Prune failed: {e}")
                return {"ok": False, "error": str(e)}

        result = await asyncio.to_thread(_do_prune)
        if result.get("ok"):
            try:
                from src.services.db import set_config_value
                total = sum(result.get("pruned", {}).values())
                await set_config_value("last_prune_at", datetime.now().isoformat())
                await set_config_value("last_prune_rows", str(total))
            except Exception:
                pass
        return result

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _prune_old_backups(self, retention_days: int) -> None:
        """Delete zip archives older than retention_days."""
        count = 0
        now = time.time()
        max_age = retention_days * 86400
        for f in self.backup_dir.glob("backup_*.zip"):
            if f.is_file() and (now - f.stat().st_mtime) > max_age:
                f.unlink(missing_ok=True)
                count += 1
        if count:
            logger.info(f"Pruned {count} old backup archive(s).")
