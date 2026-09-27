"""Persona matrix API routes.

Implements GH issue #11 (BD-04) — read + admin-managed override surface.

GET    /api/persona                  → current persona snapshot (all gateways + global)
GET    /api/persona/log              → recent persona_detection_log entries
POST   /api/persona/detect           → trigger a fresh detection run (admin)
POST   /api/persona/override         → set persona.override.{key}=value (admin)
DELETE /api/persona/overrides        → clear ALL persona.override.* (admin)
"""
from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routes.api_security import require_admin
from src.services import db, persona, persona_detector

logger = logging.getLogger(__name__)
router = APIRouter(tags=["persona"])


class OverrideRequest(BaseModel):
    key: str
    value: Any


@router.get("/persona")
async def get_persona_snapshot() -> dict:
    """Return persona flags for every configured gateway + global fields."""
    from src.services import gateway_registry  # noqa: F401  (kept for parity)
    from src.app_state import get_app_state

    global_snap = await persona.get_persona(None)

    # Enumerate serials from app_state registry; fall back to app_config.
    per_gateway: dict[str, dict] = {}
    state = get_app_state()
    registry = state.get("registry") if isinstance(state, dict) else None
    serials: list[str] = []
    if registry is not None:
        svcs = getattr(registry, "_services", None) or {}
        # Prefer full_serial (the persona key format) over short_id
        for short_id, svc in svcs.items():
            serials.append(getattr(svc, "full_serial", None) or short_id)
    if not serials:
        # Fallback: enumerate persona.gateway_type.* keys from app_config
        async with aiosqlite.connect(db.get_db_path(), timeout=30.0) as conn:
            async with conn.execute(
                "SELECT key FROM app_config WHERE key LIKE 'persona.gateway_type.%'"
            ) as cur:
                async for row in cur:
                    serials.append(row[0].removeprefix("persona.gateway_type."))

    for serial in serials:
        gp = await persona.get_persona(serial)
        per_gateway[serial] = {
            "gateway_type": gp.gateway_type,
            "grid_status": gp.grid_status,
            "solar_present": gp.solar_present,
            "mppt_enabled": gp.mppt_enabled,
            "enphase_present": gp.enphase_present,
            "has_ahub": gp.has_ahub,
            "has_mac1": gp.has_mac1,
        }

    overrides = await persona.list_overrides()

    return {
        "global": {
            "tariff_type": global_snap.tariff_type,
            "country_code": global_snap.country_code,
            "detected_at": global_snap.detected_at,
            "overrides_active": global_snap.overrides_active,
        },
        "per_gateway": per_gateway,
        "overrides": overrides,
    }


@router.get("/persona/log")
async def get_persona_log(limit: int = 100) -> dict:
    """Return the last N detection log entries (newest first)."""
    limit = max(1, min(int(limit), 500))
    rows: list[dict] = []
    async with aiosqlite.connect(db.get_db_path(), timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT detected_at, gateway_serial, axis, value, source, confidence, previous_value
               FROM persona_detection_log
               ORDER BY detected_at DESC, id DESC
               LIMIT ?""",
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(dict(row))
    return {"entries": rows, "count": len(rows)}


@router.post("/persona/detect", dependencies=[Depends(require_admin)])
async def trigger_detect() -> dict:
    """Force a fresh detection run. Admin only."""
    try:
        result = await persona_detector.detect_and_persist(force=True)
    except Exception as e:
        logger.exception("persona detect_and_persist failed")
        raise HTTPException(status_code=500, detail=f"detection failed: {e}") from e
    return {"ok": True, "result": result}


@router.post("/persona/override", dependencies=[Depends(require_admin)])
async def set_override(req: OverrideRequest) -> dict:
    """Set persona.override.{key}=value. Sets persona.overrides_active=True.

    `key` must be a supported persona axis — accepted keys mirror the
    axis names used in persona.py:
      global: tariff_type, country_code
      per-gateway: grid_status.{serial}, solar_present.{serial},
                   mppt_enabled.{serial}, enphase_present.{serial},
                   has_ahub.{serial}, has_mac1.{serial}, gateway_type.{serial}
    """
    _validate_override_key(req.key)
    await persona.set_override(req.key, req.value)
    return {"ok": True, "key": req.key, "value": req.value}


@router.delete("/persona/overrides", dependencies=[Depends(require_admin)])
async def clear_all_overrides() -> dict:
    """Remove every persona.override.* key + set persona.overrides_active=False."""
    await persona.clear_overrides()
    return {"ok": True}


# ── Helpers ─────────────────────────────────────────────────────────────

_ALLOWED_GLOBAL_KEYS = {"tariff_type", "country_code"}
_ALLOWED_PER_GATEWAY_KEYS = {
    "gateway_type", "grid_status", "solar_present", "mppt_enabled",
    "enphase_present", "has_ahub", "has_mac1",
}


def _validate_override_key(key: str) -> None:
    """Reject anything not in the allowed axis list. Keeps callers from
    accidentally writing arbitrary app_config keys via the override path."""
    if key in _ALLOWED_GLOBAL_KEYS:
        return
    # per-gateway format: axis.{serial}
    if "." in key:
        axis, _, serial = key.partition(".")
        if axis in _ALLOWED_PER_GATEWAY_KEYS and serial:
            return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Invalid override key '{key}'. Allowed: "
            f"{sorted(_ALLOWED_GLOBAL_KEYS)} (global) or "
            f"{sorted(_ALLOWED_PER_GATEWAY_KEYS)}.<serial> (per-gateway)."
        ),
    )
