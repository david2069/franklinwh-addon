"""Export and import tariffs, utility services and schedule presets.

Entered by hand from a bill, and easy to lose: a Home Assistant add-on
uninstall can take `/data` with it, backups keep only seven days, and the
companion Bridges hold none of this — their `tariffs` and `utilities` tables
exist but are empty. When an AGL plan went missing there was nothing anywhere
to restore it from.

So: one file that can be kept outside the add-on, put back after a reinstall,
and handed to somebody on the same retailer plan.

**Account identifiers are stripped on export.** NMI, account number and meter
serial identify a person and a property, and a file meant to be shared must not
carry them. Everything else — the plan, its seasons, windows, rates and
standing charges — is the retailer's published pricing, which is the part worth
exchanging.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Bundle format. Raise it when the shape changes incompatibly.
BUNDLE_VERSION = 1

#: Tables carried, parents before children so foreign keys land in order.
TABLES = (
    "utility_services",
    "utility_tariffs",
    "utility_tariff_seasons",
    "utility_tariff_rates",
    "utility_standing_charges",
    "utility_service_windows",
)

#: Columns that identify a person or a property rather than a tariff.
PRIVATE_COLUMNS = {"nmi", "account_number", "meter_serial", "account", "address"}


def _strip_private(row: dict) -> dict:
    return {k: ("" if k in PRIVATE_COLUMNS and v else v) for k, v in row.items()}


async def export_bundle(include_presets: bool = True) -> dict[str, Any]:
    """Everything needed to recreate the pricing setup elsewhere."""
    from src.services import db

    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "tables": {},
        "presets": [],
    }

    async with db.get_db() as conn:
        for table in TABLES:
            try:
                async with conn.execute(f"SELECT * FROM {table}") as cur:
                    columns = [c[0] for c in cur.description]
                    rows = [dict(zip(columns, r)) for r in await cur.fetchall()]
            except Exception:
                logger.debug("tariff export: %s unavailable", table, exc_info=True)
                rows = []
            bundle["tables"][table] = [_strip_private(r) for r in rows]

    if include_presets:
        bundle["presets"] = _read_presets()

    counts = {t: len(v) for t, v in bundle["tables"].items() if v}
    logger.info("tariff export: %s, %d preset(s)", counts or "nothing", len(bundle["presets"]))
    return bundle


def _read_presets() -> list[dict]:
    """Schedule presets, which live in a JSON file rather than the database."""
    from src.services.schedule_presets import _default_preset_path

    try:
        with open(_default_preset_path(), encoding="utf-8") as f:
            presets = json.load(f)
        # Built-ins are seeded on every install; shipping them would overwrite
        # the recipient's copies with ours for no gain.
        return [p for p in presets if not p.get("built_in")]
    except (OSError, ValueError):
        logger.debug("tariff export: no presets file", exc_info=True)
        return []


async def import_bundle(bundle: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    """Restore a bundle. Returns what was written and what was refused.

    `replace` empties the tables first. Without it rows are merged by primary
    key, so re-importing your own export is a no-op rather than a duplicate.
    """
    from src.services import db

    version = bundle.get("bundle_version")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"bundle version {version!r} is not supported (expected {BUNDLE_VERSION})")

    tables = bundle.get("tables") or {}
    unknown = set(tables) - set(TABLES)
    if unknown:
        # Refuse rather than ignore: a bundle naming tables we do not carry was
        # made by something else, and half-importing it is worse than failing.
        raise ValueError(f"bundle contains unknown tables: {sorted(unknown)}")

    written: dict[str, int] = {}
    async with db.get_db() as conn:
        if replace:
            for table in reversed(TABLES):        # children first
                await conn.execute(f"DELETE FROM {table}")

        for table in TABLES:                       # parents first
            rows = tables.get(table) or []
            for row in rows:
                columns = list(row)
                placeholders = ", ".join("?" for _ in columns)
                await conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    [row[c] for c in columns],
                )
            if rows:
                written[table] = len(rows)
        await conn.commit()

    presets_written = _write_presets(bundle.get("presets") or [])

    logger.info("tariff import: %s, %d preset(s)", written or "nothing", presets_written)
    return {"tables": written, "presets": presets_written}


def _write_presets(incoming: list[dict]) -> int:
    """Merge presets by name. An existing name is kept, not overwritten."""
    from src.services.schedule_presets import _default_preset_path

    if not incoming:
        return 0

    path = _default_preset_path()
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = []

    have = {p.get("name") for p in existing}
    added = [p for p in incoming if p.get("name") not in have]
    if not added:
        return 0

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing + added, f, indent=2)
    except OSError:
        logger.warning("tariff import: could not write presets", exc_info=True)
        return 0
    return len(added)
