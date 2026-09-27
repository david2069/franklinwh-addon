"""
SolarForecastManager — orchestrates dual-source solar forecasting.

Source selection (auto mode):
  1. HA entities        — if ha_url + ha_token + at least one entity configured
  2. Forecast.Solar     — free, no key needed; requires lat/lng
  3. Solcast            — requires api_key; most accurate with site_id
  4. None               — returns empty list; forecast map uses flat SOC

Cache: data/solar_forecast_cache.json (60-min minimum TTL).
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import SolarForecastProvider
from .forecast_solar import ForecastSolarProvider
from .ha_sensor_source import HASensorSource
from .solcast import SolcastProvider
from .open_meteo import OpenMeteoSolarProvider

logger = logging.getLogger(__name__)

# How long the bell-curve HA source is valid (live data refreshes fast)
_HA_CACHE_MINS = 15
# How long provider caches are valid
_PROVIDER_CACHE_MINS = 60


class SolarForecastManager:
    """
    Central manager for the solar forecast pipeline.
    Call get_forecast() to get the current 48-slot profile regardless of source.
    """

    def __init__(self, config: Dict[str, Any], data_dir: Optional[Path] = None):
        self.config = config.copy() if config else {}
        self.data_dir = data_dir or Path("./data")
        self.cache_file = self.data_dir / "solar_forecast_cache.json"

        self.enabled: bool = self.config.get("enabled", False)
        self.source: str = self.config.get("source", "auto")  # auto|ha_entities|forecast_solar|solcast

        # Resilient fallbacks for flat database dictionaries
        if "installation" not in self.config:
            self.config["installation"] = {
                "lat":     self.config.get("lat"),
                "lng":     self.config.get("lng"),
                "azimuth": self.config.get("azimuth", 180.0),
                "tilt":    self.config.get("tilt", 22.5),
                "kwp":     self.config.get("kwp", 5.0),
            }
        if "providers" not in self.config:
            self.config["providers"] = {
                "forecast_solar": {
                    "api_key": self.config.get("forecast_solar_api_key") or "",
                },
                "solcast": {
                    "api_key": self.config.get("solcast_api_key") or "",
                    "site_id": self.config.get("solcast_site_id") or "",
                }
            }
        if "ha" not in self.config:
            self.config["ha"] = {
                "ha_url":                self.config.get("ha_url") or "",
                "ha_token":              self.config.get("ha_token") or "",
                "solar_actual_entity":   self.config.get("ha_solar_actual_entity") or "",
                "solar_forecast_entity": self.config.get("ha_solar_forecast_entity") or "",
            }

        # Installation specs (used by built-in providers)
        self.installation: Dict[str, Any] = self.config.get("installation", {})
        self.lat: Optional[float] = self.installation.get("lat")
        self.lng: Optional[float] = self.installation.get("lng")

        # HA entities config
        self._ha_source: Optional[HASensorSource] = self._build_ha_source()

        # Built-in provider
        self._provider: Optional[SolarForecastProvider] = self._build_provider()

    # ── Source construction ──────────────────────────────────────────────────

    def _build_ha_source(self) -> Optional[HASensorSource]:
        ha_config = self.config.get("ha", {})
        if ha_config.get("ha_url") and ha_config.get("ha_token"):
            return HASensorSource(ha_config)
        return None

    def _build_provider(self) -> Optional[SolarForecastProvider]:
        providers_cfg = self.config.get("providers", {})
        active = self.source if self.source not in ("auto", "ha_entities") else "openmeteo"

        if active == "solcast":
            return SolcastProvider(providers_cfg.get("solcast", {}))
        if active == "forecast_solar":
            return ForecastSolarProvider(providers_cfg.get("forecast_solar", {}))
        if active == "openmeteo":
            return OpenMeteoSolarProvider(providers_cfg.get("openmeteo", {}))
        return None

    # ── Public API ───────────────────────────────────────────────────────────

    def get_forecast(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Return 48 × 30-min solar production slots, from the best available source."""
        if not self.enabled:
            return []

        # 1. Try HA entities first (auto or explicit ha_entities mode)
        if self.source in ("auto", "ha_entities") and self._ha_source and self._ha_source.is_available():
            cache = self._load_cache()
            if not force_refresh and cache and cache.get("source") == "ha_entities":
                last_sync_str = cache.get("last_sync")
                if last_sync_str:
                    last_sync = datetime.fromisoformat(last_sync_str)
                    if datetime.now() < last_sync + timedelta(minutes=_HA_CACHE_MINS):
                        return cache["data"]

            data = self._ha_source.get_forecast()
            if data:
                self._save_cache(data, source="ha_entities")
                return data

        # 2. Fall back to built-in provider (requires lat/lng)
        if self.source == "ha_entities":
            # Explicit HA mode but not available — return empty
            return []

        if not self.lat or not self.lng:
            logger.debug("Solar forecast: no lat/lng configured — skipping built-in provider")
            return self._load_cache().get("data", []) if self._load_cache() else []

        if not self._provider:
            return []

        cache = self._load_cache() or {}
        last_sync_str = cache.get("last_sync")
        last_attempt_str = cache.get("last_attempt")

        # Determine rate limit window
        rate_limit_mins = int(self.config.get("forecast_solar_rate_limit_mins", 30))

        # Check if the cache is still valid
        if not force_refresh and last_sync_str:
            last_sync = datetime.fromisoformat(last_sync_str)
            if datetime.now() < last_sync + timedelta(minutes=_PROVIDER_CACHE_MINS):
                return cache.get("data", [])

        # Enforce rate limit / cooldown to prevent thundering herd / retry storms
        if self.source == "forecast_solar" and last_attempt_str:
            last_attempt = datetime.fromisoformat(last_attempt_str)
            cooldown_expiry = last_attempt + timedelta(minutes=rate_limit_mins)
            if datetime.now() < cooldown_expiry:
                logger.info(
                    f"Forecast.Solar fetch skipped: Rate limit cooldown active until {cooldown_expiry.isoformat()} "
                    f"({rate_limit_mins} mins). Returning cached forecast."
                )
                return cache.get("data", [])

        # Update last_attempt timestamp in the cache BEFORE making the API call.
        # This acts as a lock/guard to prevent concurrent requests or immediate retries
        # if the request hangs or throws an exception.
        self._save_cache_attempt(cache.get("data", []))

        data = self._provider.get_forecast(self.lat, self.lng, self.installation)
        if data:
            self._save_cache(data, source=self.source)
            return data

        return cache.get("data", [])

    def get_status(self) -> Dict[str, Any]:
        """Return current source, last sync time, and availability."""
        cache = self._load_cache()
        ha_avail = bool(self._ha_source and self._ha_source.is_available())

        if self.source == "auto":
            active_source = "ha_entities" if ha_avail else (
                "openmeteo" if self._provider else "none"
            )
        else:
            active_source = self.source

        return {
            "enabled":        self.enabled,
            "configured_source": self.source,
            "active_source":  active_source,
            "ha_available":   ha_avail,
            "provider_ready": bool(self._provider),
            "lat":            self.lat,
            "lng":            self.lng,
            "last_sync":      cache.get("last_sync") if cache else None,
            "last_source":    cache.get("source") if cache else None,
            "slot_count":     len(cache.get("data", [])) if cache else 0,
        }

    def test_connection(self) -> bool:
        if self._ha_source and self._ha_source.is_available():
            return bool(self._ha_source.get_forecast())
        if self._provider:
            return self._provider.validate_connection()
        return False

    # ── Cache helpers ────────────────────────────────────────────────────────

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        if self.cache_file.exists():
            try:
                with open(self.cache_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def _save_cache(self, data: List[Dict[str, Any]], source: str = "unknown") -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now().isoformat()
        with open(self.cache_file, "w") as f:
            json.dump({
                "last_sync": now_str,
                "last_attempt": now_str,
                "source":    source,
                "data":      data,
            }, f)

    def _save_cache_attempt(self, cache_data: List[Dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existing = self._load_cache() or {}
        now_str = datetime.now().isoformat()
        with open(self.cache_file, "w") as f:
            json.dump({
                "last_sync": existing.get("last_sync"),
                "last_attempt": now_str,
                "source":    existing.get("source", self.source),
                "data":      cache_data,
            }, f)
