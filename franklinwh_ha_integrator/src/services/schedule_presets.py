"""
TOU Schedule Presets — JSON file-backed preset storage.

Provides save/load/delete/list operations for named TOU schedule presets.
Presets are stored as a JSON array in <data_dir>/schedule_presets.json.
Presets are cross-gateway (shared across all registered gateways).

On first run, seeds predefined schedules from franklinwh_cloud library constants.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Human-readable names → franklinwh_cloud constant names
BUILTIN_SCHEDULE_MAP = {
    "Self Consumption (Default)": "self_schedule",
    "Charge from Grid": "charge_from_grid",
    "Charge from Solar": "charge_from_solar",
    "Export to Grid (Always)": "export_to_grid_always",
    "Export to Grid (Peak Only)": "export_to_grid_peakonly",
    "Export to Grid (Peak x2)": "export_to_grid_peak2",
    "Power Home Only": "power_home_only",
    "Standby": "standby_schedule",
    "Custom (Multi-Period)": "custom_schedule",
    "Gap Schedule": "gap_schedule",
}


def _default_preset_path() -> str:
    """Derive preset path from the DB directory, which is already set by lifespan init."""
    try:
        from src.services.db import get_db_path
        return str(get_db_path().parent / "schedule_presets.json")
    except Exception:
        # Fallback for test environments where DB path may not be set
        return os.path.join(os.getcwd(), "data", "schedule_presets.json")


class SchedulePresets:
    """Manages TOU schedule presets backed by a JSON file.

    Presets are cross-gateway: the same preset list is shared across all
    registered gateways. Users can include a gateway name in the preset
    name string for per-gateway organisation if desired.
    """

    def __init__(self, path: str | None = None):
        self._path = path or _default_preset_path()
        self._presets: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        """Load presets from disk."""
        try:
            if os.path.exists(self._path):
                with open(self._path, "r") as f:
                    self._presets = json.load(f)
            else:
                self._presets = []
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"📋 Failed to load schedule presets from {self._path}: {e}")
            self._presets = []

    def _save(self) -> None:
        """Persist presets to disk."""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._presets, f, indent=2)

    def seed_defaults(self) -> None:
        """Seed predefined schedules from franklinwh_cloud library constants.

        Only runs once — skips if any built-in presets already exist.
        """
        if any(p.get("built_in") for p in self._presets):
            logger.debug("📋 Presets already seeded — skipping")
            return

        try:
            import franklinwh_cloud as fw
        except ImportError:
            logger.warning("📋 franklinwh_cloud not available — cannot seed default presets")
            return

        seeded = 0
        for display_name, const_name in BUILTIN_SCHEDULE_MAP.items():
            schedule_data = getattr(fw, const_name, None)
            if schedule_data is None:
                logger.debug(f"📋 Constant '{const_name}' not found in franklinwh_cloud — skipping")
                continue

            preset: dict[str, Any] = {
                "name": display_name,
                "description": f"Built-in: {const_name}",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "schedule": schedule_data,
                "built_in": True,
            }
            self._presets.append(preset)
            seeded += 1

        if seeded > 0:
            self._save()
            logger.info(f"📋 Seeded {seeded} built-in TOU presets")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def list_presets(self) -> list[dict[str, Any]]:
        """Return summary list of all presets (without full schedule data)."""
        return [
            {
                "name": p["name"],
                "description": p.get("description", ""),
                "created_at": p.get("created_at", ""),
                "slot_count": len(p.get("schedule", [])),
                "built_in": p.get("built_in", False),
                "unverified": p.get("unverified", False),
            }
            for p in self._presets
        ]

    def save_preset(
        self, name: str, description: str, schedule: list[dict],
        unverified: bool = False,
    ) -> dict[str, Any]:
        """Save or overwrite a preset by name.

        Built-in presets can be overwritten — there is no write-protection.
        Presets with ``unverified=True`` are excluded from the MQTT
        ``tou_saved_dispatches`` select entity options.
        """
        # Remove existing preset with same name (case-sensitive)
        self._presets = [p for p in self._presets if p["name"] != name]

        preset: dict[str, Any] = {
            "name": name,
            "description": description,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "schedule": schedule,
            "built_in": False,
            "unverified": unverified,
        }
        self._presets.append(preset)
        self._save()
        status = "unverified" if unverified else "verified"
        logger.info(f"📋 Preset saved: '{name}' ({len(schedule)} slots, {status})")
        return {"success": True, "preset": name, "unverified": unverified}

    def load_preset(self, name: str) -> dict[str, Any]:
        """Load a preset by name, returning the full schedule data."""
        preset = next((p for p in self._presets if p["name"] == name), None)
        if not preset:
            return {"success": False, "error": f'Preset "{name}" not found.'}
        return {
            "success": True,
            "name": preset["name"],
            "schedule": preset.get("schedule", []),
        }

    def delete_preset(self, name: str) -> dict[str, Any]:
        """Delete a preset by name."""
        before = len(self._presets)
        self._presets = [p for p in self._presets if p["name"] != name]
        if len(self._presets) == before:
            return {"success": False, "error": f'Preset "{name}" not found.'}
        self._save()
        logger.info(f"📋 Preset deleted: '{name}'")
        return {"success": True}
