"""Basemap configuration — the map tile provider's API key.

CARTO's public basemaps now require a key. Without one the tiles arrive stamped
"API KEY REQUIRED" across the map, which is what the Weather & Radar and API
Metrics maps were showing.

The key belongs to the installation, not to the source. This repository's add-on
mirror is published publicly, so a key committed here would be scraped; it is
entered in the UI and stored in this instance's database, exactly like the
OpenWeatherMap key it sits next to.

Read is masked. Writing an empty value keeps whatever is stored — the same
blank-preserves rule the weather config uses, so saving a form you did not
retype cannot silently erase the key. Clearing is explicit.
"""
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from src.services import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/map", tags=["map"])

CONFIG_KEY = "carto_api_key"


class BasemapUpdate(BaseModel):
    carto_api_key: Optional[str] = None
    # Explicit, because "" means "unchanged" — there has to be some way to
    # remove a key that is wrong without being able to type a blank one.
    clear: bool = False


def _mask(key: str) -> str:
    """Enough to recognise the key, not enough to use it."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


async def stored_key() -> str:
    """The raw key, for the server-side template injection."""
    try:
        return (await db.get_config_value(CONFIG_KEY, "") or "").strip()
    except Exception:
        logger.debug("basemap: could not read the stored key", exc_info=True)
        return ""


@router.get("/basemap")
async def get_basemap():
    key = await stored_key()
    return {
        "ok": True,
        "has_key": bool(key),
        "masked": _mask(key),
        # What the maps will actually use, so the UI can say so rather than
        # leaving the user to guess why the tiles look different.
        "provider": "carto" if key else "esri",
        "provider_label": "CARTO" if key else "Esri (no key needed)",
    }


@router.put("/basemap")
async def put_basemap(payload: BasemapUpdate):
    if payload.clear:
        await db.set_config_value(CONFIG_KEY, "")
        logger.info("basemap: CARTO key cleared; falling back to keyless tiles")
        return {"ok": True, "has_key": False, "masked": "", "provider": "esri",
                "provider_label": "Esri (no key needed)", "changed": True}

    new_key = (payload.carto_api_key or "").strip()
    if not new_key:
        # Blank means "leave it alone", not "delete it".
        key = await stored_key()
        return {"ok": True, "has_key": bool(key), "masked": _mask(key),
                "provider": "carto" if key else "esri",
                "provider_label": "CARTO" if key else "Esri (no key needed)",
                "changed": False}

    await db.set_config_value(CONFIG_KEY, new_key)
    logger.info("basemap: CARTO key set; tiles will be served by CARTO")
    return {"ok": True, "has_key": True, "masked": _mask(new_key),
            "provider": "carto", "provider_label": "CARTO", "changed": True}
