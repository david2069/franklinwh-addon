"""
Forecast.Solar provider — free tier, no API key required.
API: GET https://api.forecast.solar/estimate/:lat/:lon/:dec/:az/:kwp

IMPORTANT — azimuth convention differs from Home Assistant:
  HA:  0=North, 90=East, 180=South, 270=West  (0…360)
  API: 0=South, -90=East, 90=West, ±180=North (-180…180)

  Conversion: api_az = ha_az - 180  (then clamp to -180…180)
  Example: HA az=15 (North-NorthEast) → API az = 15-180 = -165

API response keys (result section):
  watts             – average power (W) for each period
  watt_hours_period – energy (Wh) produced IN each period  ← use for slot kWh
  watt_hours        – cumulative daily meter (resets at midnight)
  watt_hours_day    – total Wh per day  ← use for today/tomorrow totals

We call the API directly (no forecast-solar library) so we can correctly consume
watt_hours_period — the right metric for 30-min slot scheduling.
We run in a ThreadPoolExecutor to avoid blocking FastAPI's event loop.
"""
import concurrent.futures
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfoNotFoundError

import urllib.request
import urllib.error
import json

from .base import SolarForecastProvider

logger = logging.getLogger(__name__)

# Shared thread pool
_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="forecast_solar")


def _ha_azimuth_to_api(ha_az: float) -> int:
    """
    Convert Home Assistant azimuth (0=North, 90=East, 180=South, 270=West)
    to Forecast.Solar API azimuth (0=South, -90=East, 90=West, ±180=North).

    Formula: api_az = ha_az - 180, clamped to [-180, 180]
    """
    api_az = ha_az - 180.0
    # Clamp to valid range
    if api_az < -180:
        api_az += 360
    if api_az > 180:
        api_az -= 360
    return int(round(api_az))


def _fetch_api(lat: float, lon: float, dec: float, az_api: int, kwp: float, api_key: str = "") -> dict:
    """
    Call the Forecast.Solar estimate endpoint directly and return the parsed JSON.
    Runs in a worker thread — no asyncio required.
    """
    if api_key:
        url = f"https://api.forecast.solar/{api_key}/estimate/{lat}/{lon}/{dec}/{az_api}/{kwp}"
    else:
        url = f"https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az_api}/{kwp}"

    logger.info(f"Forecast.Solar API call: GET {url}")

    req = urllib.request.Request(url, headers={"User-Agent": "fwhhai/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")

    data = json.loads(raw)
    if data.get("message", {}).get("code", -1) != 0:
        msg = data.get("message", {})
        raise ValueError(
            f"Forecast.Solar API error {msg.get('code')}: {msg.get('text') or msg.get('type')}"
        )
    return data


def _build_slots(data: dict, period_mins: int = 30) -> List[Dict[str, Any]]:
    """
    Convert the full Forecast.Solar API JSON response into 30-min slots.

    Uses watt_hours_period (Wh energy per period between timestamps) as the
    primary source — this is the correct metric for slot-based scheduling.
    Falls back to watts (power) if watt_hours_period is absent.

    The API timestamps are NOT evenly spaced — they mark sunrise/sunset transitions.
    We distribute each period's energy proportionally across our 30-min grid.
    """
    result = data.get("result", {})
    wh_period: dict = result.get("watt_hours_period", {})
    watts: dict = result.get("watts", {})
    wh_day: dict = result.get("watt_hours_day", {})
    info: dict = data.get("message", {}).get("info", {})

    tz_name = info.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        tz = timezone.utc

    # Parse all API timestamps (local time from API)
    def _parse_ts(s: str) -> datetime:
        """Parse 'YYYY-MM-DD HH:MM:SS' as local time, then convert to UTC."""
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        local = naive.replace(tzinfo=tz)
        return local.astimezone(timezone.utc)

    # Build a map of UTC timestamp → Wh per period
    period_map: Dict[datetime, float] = {}
    if wh_period:
        prev_ts: Optional[datetime] = None
        for ts_str in sorted(wh_period.keys()):
            ts_utc = _parse_ts(ts_str)
            wh = float(wh_period[ts_str])
            period_map[ts_utc] = wh
            prev_ts = ts_utc
    elif watts:
        # Fallback: estimate Wh from watts × elapsed hours
        ts_list = sorted(watts.keys())
        for i, ts_str in enumerate(ts_list):
            ts_utc = _parse_ts(ts_str)
            w = float(watts[ts_str])
            if i + 1 < len(ts_list):
                next_ts = _parse_ts(ts_list[i + 1])
                elapsed_h = (next_ts - ts_utc).total_seconds() / 3600.0
                period_map[ts_utc] = w * elapsed_h
            else:
                period_map[ts_utc] = 0.0

    if not period_map:
        logger.warning("Forecast.Solar: no period data in API response")
        return []

    # Build 30-min slot grid from min to max API timestamp
    all_ts = sorted(period_map.keys())
    grid_start = all_ts[0].replace(minute=0, second=0, microsecond=0)
    grid_end   = all_ts[-1]

    # Map each API period to its 30-min grid buckets
    slot_wh: Dict[datetime, float] = {}

    periods = list(period_map.items())
    for idx, (end_ts, wh) in enumerate(periods):
        # The period runs from the previous timestamp to this one
        start_ts = periods[idx - 1][0] if idx > 0 else all_ts[0]
        if idx == 0:
            start_ts = grid_start  # open-ended start
        duration_s = (end_ts - start_ts).total_seconds()
        if duration_s <= 0 or wh <= 0:
            continue

        # Distribute wh proportionally across 30-min buckets that overlap this period
        t = start_ts
        while t < end_ts:
            bucket = t.replace(second=0, microsecond=0)
            bucket_half = 30 if bucket.minute < 30 else 0
            bucket = bucket.replace(minute=(0 if bucket.minute >= 30 else 0 if bucket.minute == 0 else 30))
            # Align to 30-min boundary
            bucket = t.replace(
                minute=0 if t.minute < 30 else 30,
                second=0, microsecond=0
            )
            bucket_end = bucket + timedelta(minutes=30)
            overlap_s = (min(end_ts, bucket_end) - max(start_ts, bucket)).total_seconds()
            if overlap_s > 0:
                slot_wh[bucket] = slot_wh.get(bucket, 0.0) + wh * (overlap_s / duration_s)
            t = bucket_end

    # Convert Wh slots to the normalised format
    slots: List[Dict[str, Any]] = []
    for bucket in sorted(slot_wh.keys()):
        wh_val = slot_wh[bucket]
        pv_kw  = round(wh_val / 1000.0 / 0.5, 3)  # Wh → kWh → kW for 30-min period
        slots.append({
            "timestamp":   bucket.isoformat(),
            "pv_kw":       pv_kw,
            "pv_wh":       round(wh_val, 1),
            "period_mins": 30,
        })

    logger.info(
        f"☀️  Forecast.Solar: {len(slots)} slots, "
        f"today={wh_day.get(list(wh_day.keys())[0], '?')} Wh, "
        f"tz={tz_name}"
    )
    return slots


class ForecastSolarProvider(SolarForecastProvider):
    """
    Calls api.forecast.solar to get per-period solar production estimates,
    resampled to 30-min slots for Smart Dispatch scheduling.

    Uses watt_hours_period from the full API JSON — the correct energy-per-period
    metric — rather than instantaneous watts.

    Azimuth stored in config uses HA convention (0=North).
    Automatically converted to Forecast.Solar API convention (0=South).
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
            future = _THREAD_POOL.submit(_fetch_api, lat, lon, declination, api_az, kwp, self.api_key or "")
            data   = future.result(timeout=30)
            return _build_slots(data)
        except concurrent.futures.TimeoutError:
            logger.error("Forecast.Solar fetch timed out after 30s")
            return []
        except Exception as exc:
            logger.error(f"Forecast.Solar fetch failed: {exc}")
            return []

    def validate_connection(self) -> bool:
        """
        Real connectivity check — HEAD the Forecast.Solar root.
        Returns True if the server is reachable (any non-5xx response).
        """
        url = "https://api.forecast.solar/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fwhhai-diagnostics/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception as exc:
            logger.warning(f"Forecast.Solar connectivity check failed: {exc}")
            return False

    def get_raw_api_url(self, lat: float, lon: float, installation: Dict[str, Any]) -> str:
        """Returns the exact API URL that will be called — for diagnostics display."""
        ha_az  = float(installation.get("azimuth", 180))
        dec    = float(installation.get("tilt",    22.5))
        kwp    = float(installation.get("kwp",     5.0))
        api_az = _ha_azimuth_to_api(ha_az)
        if self.api_key:
            return f"GET https://api.forecast.solar/{self.api_key}/estimate/{lat}/{lon}/{dec}/{api_az}/{kwp}"
        return f"GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{api_az}/{kwp}"
