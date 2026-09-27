"""
Open-Meteo Solar Forecast provider — free, keyless, and coordinate-based.
API: GET https://api.open-meteo.com/v1/forecast?latitude=:lat&longitude=:lon&hourly=global_tilted_irradiance&tilt=:tilt&azimuth=:azimuth&timezone=UTC

Azimuth convention mapping:
  HA:  0=North, 90=East, 180=South, 270=West  (0…360)
  API: 0=South, -90=East, 90=West, ±180=North (-180…180)

  Conversion: api_az = ha_az - 180  (then clamp to -180…180)
"""
import concurrent.futures
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error
import json

from .base import SolarForecastProvider

logger = logging.getLogger(__name__)

# Shared thread pool to avoid blocking FastAPI's event loop
_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="open_meteo_solar")


def _ha_azimuth_to_api(ha_az: float) -> int:
    """
    Convert Home Assistant azimuth (0=North, 90=East, 180=South, 270=West)
    to Open-Meteo API azimuth (0=South, -90=East, 90=West, ±180=North).
    """
    api_az = ha_az - 180.0
    if api_az < -180:
        api_az += 360
    if api_az > 180:
        api_az -= 360
    return int(round(api_az))


def _fetch_api(lat: float, lon: float, tilt: float, az_api: int) -> dict:
    """
    Call the Open-Meteo forecast endpoint directly and return the parsed JSON.
    Runs in a worker thread.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&hourly=global_tilted_irradiance"
        f"&tilt={tilt}&azimuth={az_api}&timezone=UTC"
    )
    logger.info(f"Open-Meteo Solar API call: GET {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "fwhhai/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")

    return json.loads(raw)


def _build_slots(data: dict, kwp: float) -> List[Dict[str, Any]]:
    """
    Convert the hourly Open-Meteo API JSON response into 30-min slots.
    
    Formula:
      pv_kw = (gti / 1000.0) * kwp * 0.85
    """
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    gti_list = hourly.get("global_tilted_irradiance", [])

    if not times or not gti_list:
        logger.warning("Open-Meteo Solar: no hourly global tilted irradiance in API response")
        return []

    slots: List[Dict[str, Any]] = []

    for i, t_str in enumerate(times):
        if i >= len(gti_list):
            break
        
        gti = gti_list[i]
        if gti is None:
            gti = 0.0

        # Estimate PV output in kW
        # 1000 W/m² corresponds to STC rating (kwp)
        # 0.85 is a standard system efficiency factor (temp, inverter efficiency, wiring, dust)
        pv_kw = round((float(gti) / 1000.0) * kwp * 0.85, 3)
        pv_kw = max(0.0, pv_kw)

        # Parse local ISO naive time (representing UTC due to timezone=UTC parameter)
        try:
            # e.g., "2026-06-14T08:00" -> append timezone offset to parse cleanly
            bucket_hour = datetime.fromisoformat(t_str + "+00:00")
        except Exception as err:
            logger.warning(f"Open-Meteo Solar: failed to parse timestamp '{t_str}': {err}")
            continue

        # Split 1 hour into two 30-minute slots
        for offset_mins in (0, 30):
            slot_time = bucket_hour + timedelta(minutes=offset_mins)
            slots.append({
                "timestamp":   slot_time.isoformat(),
                "pv_kw":       pv_kw,
                "period_mins": 30,
            })

    logger.info(f"☀️  Open-Meteo Solar: generated {len(slots)} forecast slots")
    return slots


class OpenMeteoSolarProvider(SolarForecastProvider):
    """
    Open-Meteo solar forecast provider.
    Calculates expected solar yields from tilted plane irradiance.
    """

    def get_forecast(
        self,
        lat: float,
        lon: float,
        installation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        ha_azimuth  = float(installation.get("azimuth", 180))  # HA: 0=North
        declination = float(installation.get("tilt",    22.5))
        kwp         = float(installation.get("kwp",     5.0))
        api_az      = _ha_azimuth_to_api(ha_azimuth)

        logger.debug(f"Azimuth: HA={ha_azimuth}° → API={api_az}° (0=South convention)")

        try:
            future = _THREAD_POOL.submit(_fetch_api, lat, lon, declination, api_az)
            data   = future.result(timeout=30)
            return _build_slots(data, kwp)
        except concurrent.futures.TimeoutError:
            logger.error("Open-Meteo Solar fetch timed out after 30s")
            return []
        except Exception as exc:
            logger.error(f"Open-Meteo Solar fetch failed: {exc}")
            return []

    def validate_connection(self) -> bool:
        """
        Connectivity check — probe Open-Meteo API with standard root/status.
        """
        url = "https://api.open-meteo.com/v1/forecast?latitude=-33.8688&longitude=151.2093&hourly=global_tilted_irradiance&tilt=22.5&azimuth=0"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fwhhai-diagnostics/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception as exc:
            logger.warning(f"Open-Meteo Solar connectivity check failed: {exc}")
            return False
