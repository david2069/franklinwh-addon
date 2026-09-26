"""Where the site is, resolved from whichever source actually knows.

Coordinates drive the solar forecast, so an empty pair means no forecast at all.
They were only ever obtainable by pressing "Sync Location from Cloud", which
called a method that does not exist (`Client.raw`) and so had never once
succeeded — the button reported a 502 every time.

Two sources, preferred in this order:

1. **Home Assistant's own configuration.** Under the Supervisor this is already
   set, it is the home rather than the equipment, and it costs no cloud call.
2. **The FranklinWH cloud's equipment location**, for installs that are not
   add-ons or where HA's location was never set.

The cloud's latitude has been observed unsigned. The previous code guessed the
hemisphere from longitude — `100 < lng < 180` was treated as southern, which
silently flips Tokyo, Seoul, Beijing, Taipei and Manila into the Southern Ocean.
This repo's own rule is that sign is data and must not be inferred
(`docs/tariff_model.md`), so a sign is only ever corrected against a second
source that has one, never guessed from a coordinate.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SOURCE_HA = "home_assistant"
SOURCE_CLOUD = "franklinwh_cloud"


def _valid(lat, lng) -> bool:
    """A real fix, not a placeholder.

    (0, 0) is in the Gulf of Guinea and is what an unset field looks like, so it
    is rejected rather than accepted as a location nobody lives at.
    """
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return False
    return not (abs(lat) < 0.0001 and abs(lng) < 0.0001)


async def from_home_assistant() -> Optional[dict]:
    """HA's configured latitude/longitude, or None if it cannot be read."""
    try:
        from src.routes.api_ha import _ha_get

        config = await _ha_get("/config")
    except Exception as exc:
        logger.debug(f"site_location: Home Assistant config unavailable: {exc!r}")
        return None

    if not isinstance(config, dict):
        return None

    lat, lng = config.get("latitude"), config.get("longitude")
    if not _valid(lat, lng):
        return None

    return {
        "lat": float(lat),
        "lng": float(lng),
        "source": SOURCE_HA,
        "timezone": config.get("time_zone") or "",
        "elevation": config.get("elevation"),
    }


async def from_cloud(gw) -> Optional[dict]:
    """The gateway's equipment location from the FranklinWH cloud.

    `get_equipment_location()` takes no arguments — it reads the gateway serial
    off the client. It was being called through `Client.raw(...)`, which does
    not exist on the client at all.
    """
    try:
        client = await gw._get_or_create_client()
        loc = await client.get_equipment_location()
    except Exception as exc:
        logger.warning(f"site_location: cloud lookup failed: {exc!r}")
        return None

    if not isinstance(loc, dict):
        return None

    lat, lng = loc.get("latitude"), loc.get("longitude")
    if not _valid(lat, lng):
        return None

    return {
        "lat": float(lat),
        "lng": float(lng),
        "source": SOURCE_CLOUD,
        "city": loc.get("city") or "",
        "country": loc.get("country") or "",
        "timezone": loc.get("timezone") or "",
    }


def reconcile_sign(cloud: dict, reference: Optional[dict]) -> dict:
    """Correct an unsigned cloud latitude against a source that has a sign.

    Only acts when the two agree on magnitude but differ on sign, which is what
    an unsigned value looks like next to a signed one. With no reference, the
    cloud value is returned untouched — a wrong coordinate is recoverable, and a
    silently flipped one is not.
    """
    if not reference:
        return cloud

    c_lat, r_lat = cloud["lat"], reference["lat"]
    same_magnitude = abs(abs(c_lat) - abs(r_lat)) < 0.5
    differing_sign = (c_lat >= 0) != (r_lat >= 0)

    if same_magnitude and differing_sign:
        logger.info(
            f"site_location: cloud latitude {c_lat} contradicts "
            f"{reference['source']} {r_lat}; taking the signed value"
        )
        return {**cloud, "lat": r_lat, "sign_corrected": True}
    return cloud


async def resolve(gw=None) -> Optional[dict]:
    """Best available location, or None if no source knows one."""
    ha = await from_home_assistant()
    if ha:
        return ha

    if gw is not None:
        cloud = await from_cloud(gw)
        if cloud:
            return reconcile_sign(cloud, ha)

    return None


async def propagate(lat: float, lng: float, *, short_id: str = "") -> dict:
    """Write one coordinate pair to every store that holds it.

    One latitude lives in four places — see `docs/geolocation.md`. They mean the
    same thing in the same units and drift apart because nothing keeps them
    together. Open-Meteo read through to the solar config when the weather
    config was blank, which worked until someone typed a latitude on the Weather
    tab: after that the two diverged permanently and silently, and the weather
    panel described one place while the solar forecast described another.

    Home Assistant's own location is not written. It is the user's, and this
    integration is not entitled to change where they said they live.

    A value already set is left alone. Filling a blank is help; changing an
    answer is not.
    """
    from src.services import db

    if not _valid(lat, lng):
        return {"written": [], "skipped": ["invalid coordinates"]}

    written, skipped = [], []

    if short_id:
        try:
            await db.set_config_value(f"lat_{short_id}", str(lat))
            await db.set_config_value(f"lng_{short_id}", str(lng))
            written.append(f"config_value/{short_id}")
        except Exception:
            logger.debug("propagate: per-gateway config_value failed", exc_info=True)
            skipped.append("config_value")

    try:
        await db.upsert_solar_forecast_config(lat=lat, lng=lng)
        written.append("solar_forecast_config")
    except Exception:
        logger.debug("propagate: solar_forecast_config failed", exc_info=True)
        skipped.append("solar_forecast_config")

    # Weather keeps its own copy and only fell back to solar's while blank.
    try:
        cfg = await db.get_config_value("weather_provider_config", {}) or {}
        if isinstance(cfg, dict) and cfg.get("owm_lat") is None and cfg.get("owm_lng") is None:
            cfg["owm_lat"], cfg["owm_lng"] = lat, lng
            await db.set_config_value("weather_provider_config", cfg)
            written.append("weather_provider_config")
        else:
            skipped.append("weather_provider_config (already set)")
    except Exception:
        logger.debug("propagate: weather_provider_config failed", exc_info=True)
        skipped.append("weather_provider_config")

    if written:
        logger.info(f"Site location {lat}, {lng} propagated to: {', '.join(written)}")
    return {"written": written, "skipped": skipped}
