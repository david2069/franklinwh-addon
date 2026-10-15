"""
Solcast provider — uses the rooftop_sites PV forecast endpoint.
Free hobbyist tier: 10 calls/day — cache aggressively (60+ min).

Authentication: Bearer token via Authorization header (NOT query param)
Docs: https://docs.solcast.com.au/
"""
import json
import logging
import os
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .base import SolarForecastProvider

logger = logging.getLogger(__name__)

# Solcast rooftop endpoint requires a pre-created site at toolkit.solcast.com.au
_ROOFTOP_FORECAST_URL = "https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts"
# Radiation + weather: any lat/lng, no site required — rough proxy only
_RADIATION_URL = "https://api.solcast.com.au/data/forecast/radiation_and_weather"


class SolcastProvider(SolarForecastProvider):
    """
    Two modes:
    - site_id + api_key → rooftop_sites endpoint (accurate, site-specific)
    - api_key only       → radiation proxy (rough proxy, no site required)

    Free hobbyist tier: 10 calls/day — enforced here with a persistent call log.
    """

    # Hard limit for the Solcast free hobbyist tier
    DAILY_CALL_LIMIT = 10

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.site_id: str = config.get("site_id", "")
        # Resolve data dir for call log (same dir as forecast cache)
        data_dir = config.get("_data_dir", "./data")
        self._call_log_file = Path(data_dir) / "solcast_call_log.json"

    # ── Call counting ────────────────────────────────────────────────────────

    def get_calls_today(self) -> int:
        """Return number of Solcast API calls made today (UTC date)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if self._call_log_file.exists():
                data = json.loads(self._call_log_file.read_text())
                return data.get(today, 0)
        except Exception:
            pass
        return 0

    def _record_call(self) -> int:
        """Increment today's call count and persist. Returns new count."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data: dict = {}
        try:
            if self._call_log_file.exists():
                data = json.loads(self._call_log_file.read_text())
        except Exception:
            pass
        # Prune old dates (keep last 7 days max)
        data = {k: v for k, v in data.items() if k >= today[:7]}
        data[today] = data.get(today, 0) + 1
        try:
            self._call_log_file.write_text(json.dumps(data))
        except Exception as exc:
            logger.warning(f"Solcast: could not persist call log — {exc}")
        return data[today]

    def _check_limit(self) -> bool:
        """Return True if a call is allowed (under limit). Log warning if near limit."""
        count = self.get_calls_today()
        if count >= self.DAILY_CALL_LIMIT:
            logger.error(
                f"Solcast: daily call limit reached ({count}/{self.DAILY_CALL_LIMIT}) — "
                f"using cache. Limit resets at 00:00 UTC."
            )
            return False
        if count >= self.DAILY_CALL_LIMIT - 2:
            logger.warning(
                f"Solcast: approaching daily limit ({count}/{self.DAILY_CALL_LIMIT}) — "
                f"{self.DAILY_CALL_LIMIT - count} call(s) remaining today."
            )
        return True

    def get_forecast(
        self,
        lat: float,
        lon: float,
        installation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not self.api_key:
            logger.warning("Solcast: no API key configured")
            return []
        # Enforce daily call limit before hitting the API
        if not self._check_limit():
            return []  # Caller will fall back to cache

        if self.site_id:
            return self._fetch_rooftop(installation)
        return self._fetch_radiation_proxy(lat, lon, installation)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ── rooftop_sites endpoint ──────────────────────────────────────────────
    def _fetch_rooftop(self, installation: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = _ROOFTOP_FORECAST_URL.format(site_id=self.site_id)
        try:
            self._record_call()
            resp = requests.get(
                url,
                params={"format": "json", "hours": 48, "period": "PT30M"},
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            forecasts = resp.json().get("forecasts", [])
            slots: List[Dict[str, Any]] = []
            for f in forecasts:
                slots.append({
                    "timestamp":   f.get("period_end", ""),
                    "pv_kw":       round(f.get("pv_estimate", 0.0), 3),
                    "period_mins": 30,
                })
            logger.info(f"☀️  Solcast rooftop: {len(slots)} slots")
            return slots
        except Exception as exc:
            logger.error(f"Solcast rooftop fetch failed: {exc}")
            return []

    # ── radiation proxy fallback ────────────────────────────────────────────
    def _fetch_radiation_proxy(
        self,
        lat: float,
        lon: float,
        installation: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        kwp = installation.get("kwp", 5.0)
        try:
            resp = requests.get(
                _RADIATION_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "format": "json",
                    "hours": 48,
                    "period": "PT30M",
                },
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            forecasts = resp.json().get("forecasts", [])
            slots: List[Dict[str, Any]] = []
            for f in forecasts:
                # GHI W/m² → rough kW estimate (not site-specific)
                ghi = f.get("ghi", 0)
                pv_kw = round(ghi * kwp / 1000.0 * 0.8, 3)  # ~80% system efficiency
                slots.append({
                    "timestamp":   f.get("period_end", ""),
                    "pv_kw":       pv_kw,
                    "period_mins": 30,
                })
            logger.info(f"☀️  Solcast radiation proxy: {len(slots)} slots")
            return slots
        except Exception as exc:
            logger.error(f"Solcast radiation proxy fetch failed: {exc}")
            return []

    def validate_connection(self) -> bool:
        if not self.api_key:
            return False
        try:
            if self.site_id:
                url = _ROOFTOP_FORECAST_URL.format(site_id=self.site_id)
                resp = requests.get(
                    url,
                    params={"format": "json", "hours": 1, "period": "PT30M"},
                    headers=self._headers(),
                    timeout=10,
                )
                return resp.status_code == 200
            return True  # radiation proxy assumes ok if we have a key
        except Exception:
            return False
