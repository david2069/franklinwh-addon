"""
config_integrity.py — SHA-256 hash verification + versioned backup for .env and options.json.

Called during application startup to detect silent corruption or unexpected changes.

Design:
  - Hash file (.env.sha256, .options.sha256) stored in data/ (inside the Docker volume).
  - On first boot: records baseline hash, creates first backup copy.
  - On subsequent boots: compares hash; if changed → warns in logs, creates new backup copy.
  - Keeps last MAX_BACKUP_VERSIONS versioned copies in data/backups/config_<timestamp>/.
  - Pruning of old copies is timestamp-sorted (oldest removed first).

Security posture:
  - This is tamper/corruption DETECTION, not prevention.
  - .env should contain no secrets once they are migrated to the DB (log reminder emitted).
  - options.json is managed by HA Supervisor — any change here is unusual and worth logging.
"""
import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BACKUP_VERSIONS = 10


def _sha256_file(path: Path) -> str | None:
    """Compute hex SHA-256 of a file. Returns None if file is missing or unreadable."""
    if not path.exists():
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as exc:
        logger.warning(f"Cannot read {path} for integrity check: {exc}")
        return None


def _read_stored_hash(hash_file: Path) -> str | None:
    if not hash_file.exists():
        return None
    try:
        content = hash_file.read_text().strip()
        return content if content else None
    except Exception:
        return None


def _write_hash(hash_file: Path, hex_hash: str) -> None:
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_text(hex_hash + "\n")


def _backup_file(src: Path, backup_dir: Path, timestamp: str) -> Path | None:
    """Copy src into backup_dir/config_<timestamp>/. Returns dest path or None."""
    if not src.exists():
        return None
    dest_dir = backup_dir / f"config_{timestamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        shutil.copy2(src, dest)
        return dest
    except Exception as exc:
        logger.warning(f"Could not back up {src.name}: {exc}")
        return None


def _prune_old_backups(backup_dir: Path) -> None:
    """Keep only MAX_BACKUP_VERSIONS most-recent config_* subdirectories."""
    if not backup_dir.exists():
        return
    entries = sorted(
        [d for d in backup_dir.iterdir() if d.is_dir() and d.name.startswith("config_")],
        key=lambda d: d.stat().st_mtime,
    )
    while len(entries) > MAX_BACKUP_VERSIONS:
        oldest = entries.pop(0)
        try:
            shutil.rmtree(oldest, ignore_errors=True)
            logger.debug(f"Pruned old config backup: {oldest.name}")
        except Exception:
            pass


async def verify_and_backup(
    file_path: Path,
    data_dir: Path,
    label: str,
) -> dict:
    """
    Verify integrity of a config file and create a versioned backup if it changed.

    :param file_path: Path to the file being checked (.env or options.json)
    :param data_dir:  Application data directory (hash files and backups stored here)
    :param label:     Short label for log messages, e.g. '.env' or 'options.json'
    :returns: {'status': 'ok'|'changed'|'missing', 'hash': str|None, 'backed_up': bool}
    """
    backup_dir = data_dir / "backups"
    safe_label = label.lstrip(".")
    hash_file = data_dir / f".{safe_label}.sha256"

    if not file_path.exists():
        logger.debug(f"Config integrity: {label} not present — skipping")
        return {"status": "missing", "hash": None, "backed_up": False}

    current_hash = _sha256_file(file_path)
    if current_hash is None:
        logger.warning(f"Config integrity: could not read {label}")
        return {"status": "missing", "hash": None, "backed_up": False}

    stored_hash = _read_stored_hash(hash_file)

    if current_hash == stored_hash:
        logger.debug(f"Config integrity: {label} — unchanged ({current_hash[:12]}…)")
        return {"status": "ok", "hash": current_hash, "backed_up": False}

    # Hash changed or no stored hash yet
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backed_up_path = _backup_file(file_path, backup_dir, timestamp)
    _write_hash(hash_file, current_hash)
    _prune_old_backups(backup_dir)

    if stored_hash is None:
        logger.info(
            f"Config integrity: {label} — baseline hash recorded ({current_hash[:12]}…). "
            f"Backup: {backed_up_path}"
        )
        return {"status": "ok", "hash": current_hash, "backed_up": True}
    else:
        logger.warning(
            f"⚠️  Config integrity: {label} changed since last boot "
            f"(prev={stored_hash[:12]}… now={current_hash[:12]}…). "
            f"Backup saved: {backed_up_path}"
        )
        return {"status": "changed", "hash": current_hash, "backed_up": True}


async def run_startup_integrity_checks(data_dir: Path, env_path: Path | None = None) -> dict:
    """
    Run integrity checks for .env and options.json. Called at startup (Stage 2.5).
    Returns a dict of results keyed by 'env' and 'options'.
    """
    results: dict = {}

    # .env check
    if env_path is not None:
        results["env"] = await verify_and_backup(env_path, data_dir, ".env")
    else:
        results["env"] = {"status": "missing", "hash": None, "backed_up": False}

    # options.json (HA Addon Supervisor managed)
    options_path = data_dir / "options.json"
    results["options"] = await verify_and_backup(options_path, data_dir, "options.json")

    return results
