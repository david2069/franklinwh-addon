"""Device Registry API routes — CRUD for device_models and device_accessories tables."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services import db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["device-registry"])


# ── Request models ─────────────────────────────────────────────────────────────

class DeviceModelCreate(BaseModel):
    hw_version_int: int
    device_class: str                    # 'agate' | 'apower'
    name: str
    sku: Optional[str] = None
    model: Optional[str] = None
    api_field_name: Optional[str] = None
    real_hw_version: Optional[str] = None
    country_id: Optional[int] = None
    generation: Optional[int] = None
    has_mppt: int = 0
    type: Optional[str] = None
    notes: Optional[str] = None
    # Electrical / MPPT / regional fields (columns added via later db.py
    # migrations). Without declaring them here, Pydantic silently drops
    # them on Add Model — same data-loss class as the SD Config 422 +
    # DeviceModelPatch defects (2026-06-16/17).
    sku_region:        Optional[str]   = None
    max_service_amps:  Optional[int]   = None
    nominal_kw:        Optional[float] = None
    peak_kw:           Optional[float] = None
    max_ac_amps:       Optional[float] = None
    ac_hz:             Optional[int]   = None
    mppt_count:        Optional[int]   = None
    mppt_isc_amps:     Optional[int]   = None
    mppt_imp_amps:     Optional[int]   = None
    mppt_max_kw:       Optional[float] = None
    ac_solar_max_kw:   Optional[float] = None
    rated_kwh:         Optional[float] = None
    ac_type:           Optional[str]   = None


class DeviceModelPatch(BaseModel):
    user_override_name: Optional[str] = None
    user_note: Optional[str] = None
    name: Optional[str] = None
    sku: Optional[str] = None
    model: Optional[str] = None
    real_hw_version: Optional[str] = None
    country_id: Optional[int] = None
    generation: Optional[int] = None
    has_mppt: Optional[int] = None
    type: Optional[str] = None
    notes: Optional[str] = None
    # Electrical / MPPT / regional fields (see DeviceModelCreate above for
    # why these matter). The Edit Model HW dialog in tabs/support.html
    # binds every one of these via x-model.number — without them declared
    # here Pydantic silently drops them and the Save Changes button looks
    # successful while the values never reach the DB. Fixed 2026-06-17 as
    # part of the Phase 1 "Save button looks right, doesn't work" sweep.
    sku_region:        Optional[str]   = None
    max_service_amps:  Optional[int]   = None
    nominal_kw:        Optional[float] = None
    peak_kw:           Optional[float] = None
    max_ac_amps:       Optional[float] = None
    ac_hz:             Optional[int]   = None
    mppt_count:        Optional[int]   = None
    mppt_isc_amps:     Optional[int]   = None
    mppt_imp_amps:     Optional[int]   = None
    mppt_max_kw:       Optional[float] = None
    ac_solar_max_kw:   Optional[float] = None
    rated_kwh:         Optional[float] = None
    ac_type:           Optional[str]   = None


class TombstoneRequest(BaseModel):
    note: str = ""
    successor_hw: Optional[int] = None


class AccessoryCreate(BaseModel):
    accessory_id: int
    name: str
    accessory_type: str                  # 'smart_circuits'|'generator'|'apbox'|'ahub'|'mac1'|'split_ct'
    api_accessory_type: Optional[int] = None
    sku: Optional[str] = None
    version: Optional[int] = None
    country_id: Optional[int] = None
    compatible_agates: Optional[str] = None   # JSON array '[100,101]' or 'ALL'
    compatible_apower: Optional[str] = None
    circuit_count: Optional[int] = None
    v2l_port: int = 0
    v2l_enables: int = 0
    v2l_requires_gen: int = 0
    notes: Optional[str] = None


class AccessoryPatch(BaseModel):
    user_note: Optional[str] = None
    name: Optional[str] = None
    sku: Optional[str] = None
    notes: Optional[str] = None
    compatible_agates: Optional[str] = None
    compatible_apower: Optional[str] = None
    circuit_count: Optional[int] = None
    v2l_port: Optional[int] = None
    v2l_enables: Optional[int] = None
    v2l_requires_gen: Optional[int] = None
    api_accessory_type: Optional[int] = None


# ── Device Models endpoints ────────────────────────────────────────────────────

@router.get("/models/devices")
async def list_models(device_class: Optional[str] = None):
    """List all device models. Optionally filter by ?device_class=agate|apower."""
    rows = await db.list_device_models(device_class=device_class)
    return {"models": rows, "count": len(rows)}


@router.get("/models/devices/{hw_version_int}")
async def get_model(hw_version_int: int):
    """Get a single device model by hw_version_int."""
    row = await db.get_device_model(hw_version_int)
    if not row:
        raise HTTPException(status_code=404, detail=f"Model hw_version_int={hw_version_int} not found")
    return row


@router.post("/models/devices")
async def create_model(body: DeviceModelCreate):
    """Add a new device model (source=user). Used for unreported hardware."""
    existing = await db.get_device_model(body.hw_version_int)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"hw_version_int={body.hw_version_int} already exists. Use PUT to update."
        )
    kwargs = body.model_dump(exclude={"hw_version_int"}, exclude_none=True)
    kwargs["source"] = "user"
    await db.upsert_device_model(body.hw_version_int, **kwargs)
    logger.info(f"Device Registry: added model hw_version={body.hw_version_int} ({body.name})")
    return {"ok": True, "hw_version_int": body.hw_version_int}


@router.put("/models/devices/{hw_version_int}")
async def update_model(hw_version_int: int, body: DeviceModelPatch):
    """Update user_override_name, user_note, or other fields on an existing model."""
    existing = await db.get_device_model(hw_version_int)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Model hw_version_int={hw_version_int} not found")
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    await db.upsert_device_model(hw_version_int, **updates)
    logger.info(f"Device Registry: updated model hw_version={hw_version_int}: {list(updates.keys())}")
    return {"ok": True, "hw_version_int": hw_version_int, "updated": list(updates.keys())}


@router.post("/models/devices/{hw_version_int}/tombstone")
async def tombstone_model(hw_version_int: int, body: TombstoneRequest):
    """Soft-delete a model (sets is_deprecated=1). Does not delete the row."""
    existing = await db.get_device_model(hw_version_int)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Model hw_version_int={hw_version_int} not found")
    await db.tombstone_device_model(hw_version_int, note=body.note)
    if body.successor_hw:
        await db.upsert_device_model(hw_version_int, successor_hw=body.successor_hw)
    logger.info(f"Device Registry: tombstoned model hw_version={hw_version_int}")
    return {"ok": True, "hw_version_int": hw_version_int, "is_deprecated": True}


@router.post("/models/devices/{hw_version_int}/restore")
async def restore_model(hw_version_int: int):
    """Restore a tombstoned model (clears is_deprecated)."""
    existing = await db.get_device_model(hw_version_int)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Model hw_version_int={hw_version_int} not found")
    await db.upsert_device_model(
        hw_version_int,
        is_deprecated=0,
        deprecated_since=None,
        deprecated_note=None,
    )
    return {"ok": True, "hw_version_int": hw_version_int, "is_deprecated": False}


# ── Accessories endpoints ──────────────────────────────────────────────────────

@router.get("/models/accessories")
async def list_accessories_route(accessory_type: Optional[str] = None):
    """List all accessories. Optionally filter by ?accessory_type=smart_circuits|generator|..."""
    rows = await db.list_accessories(accessory_type=accessory_type)
    return {"accessories": rows, "count": len(rows)}


@router.get("/models/accessories/{accessory_id}")
async def get_accessory_route(accessory_id: int):
    """Get a single accessory by FHAI-internal accessory_id."""
    row = await db.get_accessory(accessory_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Accessory id={accessory_id} not found")
    return row


@router.post("/models/accessories")
async def create_accessory(body: AccessoryCreate):
    """Add a new accessory (source=user). Used for unreported accessory models."""
    existing = await db.get_accessory(body.accessory_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"accessory_id={body.accessory_id} already exists. Use PUT to update."
        )
    kwargs = body.model_dump(exclude={"accessory_id"}, exclude_none=True)
    kwargs["source"] = "user"
    await db.upsert_accessory(body.accessory_id, **kwargs)
    logger.info(f"Device Registry: added accessory id={body.accessory_id} ({body.name})")
    return {"ok": True, "accessory_id": body.accessory_id}


@router.put("/models/accessories/{accessory_id}")
async def update_accessory(accessory_id: int, body: AccessoryPatch):
    """Update user_note or other fields on an existing accessory."""
    existing = await db.get_accessory(accessory_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Accessory id={accessory_id} not found")
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    await db.upsert_accessory(accessory_id, **updates)
    logger.info(f"Device Registry: updated accessory id={accessory_id}: {list(updates.keys())}")
    return {"ok": True, "accessory_id": accessory_id, "updated": list(updates.keys())}


@router.get("/models/devices/{hw_version_int}/accessories")
async def get_model_accessories(hw_version_int: int):
    """Return accessories compatible with a given model (reverse lookup)."""
    model = await db.get_device_model(hw_version_int)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model hw_version_int={hw_version_int} not found")
    accessories = await db.get_compatible_accessories(hw_version_int, model.get("device_class", "agate"))
    return {"hw_version_int": hw_version_int, "accessories": accessories, "count": len(accessories)}
