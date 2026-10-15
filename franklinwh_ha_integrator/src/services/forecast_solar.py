"""
Forecast.Solar API Client
Free solar PV forecasting without API key required
"""

import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass
class ForecastSolarConfig:
    """Configuration for Forecast.Solar API."""
    latitude: float
    longitude: float
    declination: float = 30.0      # Panel tilt angle (0-90°)
    azimuth: float = 0.0           # Panel direction: 0=south, -90=east, 90=west, 180=north
    kwp: float = 1.0               # Installed peak power in kW
    damping: float = 0.0           # Damping factor for morning/evening (0-1)
    inverter: Optional[float] = None  # Inverter limit in kW


class ForecastSolarClient:
    """
    Client for Forecast.Solar API.
    Docs: https://doc.forecast.solar/
    """
    
    BASE_URL = "https://api.forecast.solar"
    
    def __init__(self, config: ForecastSolarConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "ForecastSolar-Python/1.0"
        })
    
    def _build_url(self, endpoint: str, params: Optional[Dict] = None) -> str:
        """Build API URL with query parameters."""
        base = f"{self.BASE_URL}{endpoint}"
        if params:
            base += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        return base
    
    def _get(self, url: str) -> Dict[str, Any]:
        """Make GET request with rate limiting awareness."""
        # Free tier: 1 request per second
        import time
        time.sleep(1.1)
        
        response = self.session.get(url, timeout=30)
        
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise Exception(f"Rate limited. Retry after {retry_after} seconds")
        
        response.raise_for_status()
        return response.json()
    
    def _build_estimate_params(self) -> Dict[str, Any]:
        """Build common parameters for estimate endpoints."""
        return {
            "lat": self.config.latitude,
            "lon": self.config.longitude,
            "dec": self.config.declination,
            "az": self.config.azimuth,
            "kwp": self.config.kwp,
            "damping": self.config.damping if self.config.damping > 0 else None,
            "inverter": self.config.inverter,
        }
    
    def get_estimate(self) -> Dict[str, Any]:
        """
        Get basic forecast for today and tomorrow.
        Returns hourly estimates in watts.
        """
        params = self._build_estimate_params()
        url = self._build_url("/estimate/:lat/:lon/:dec/:az/:kwp", params)
        
        # Manual URL construction for path parameters
        url = (
            f"{self.BASE_URL}/estimate/"
            f"{self.config.latitude}/{self.config.longitude}/"
            f"{self.config.declination}/{self.config.azimuth}/"
            f"{self.config.kwp}"
        )
        
        # Add optional query params
        query_params = {}
        if self.config.damping > 0:
            query_params["damping"] = self.config.damping
        if self.config.inverter:
            query_params["inverter"] = self.config.inverter
        
        if query_params:
            url += "?" + urlencode(query_params)
        
        data = self._get(url)
        return self._parse_estimate_response(data)
    
    def get_estimate_watts(self) -> Dict[str, Any]:
        """
        Get detailed forecast with watts for each time period.
        More granular than basic estimate.
        """
        url = (
            f"{self.BASE_URL}/estimate/"
            f"{self.config.latitude}/{self.config.longitude}/"
            f"{self.config.declination}/{self.config.azimuth}/"
            f"{self.config.kwp}/watts"
        )
        
        query_params = {}
        if self.config.damping > 0:
            query_params["damping"] = self.config.damping
        if self.config.inverter:
            query_params["inverter"] = self.config.inverter
        
        if query_params:
            url += "?" + urlencode(query_params)
        
        data = self._get(url)
        return self._parse_watts_response(data)
    
    def get_estimate_watts_period(
        self,
        period: str = "hourly"
    ) -> Dict[str, Any]:
        """
        Get forecast aggregated by time period.
        
        Args:
            period: 'hourly', 'daily', 'monthly', or 'yearly'
        """
        valid_periods = ["hourly", "daily", "monthly", "yearly"]
        if period not in valid_periods:
            raise ValueError(f"Period must be one of {valid_periods}")
        
        url = (
            f"{self.BASE_URL}/estimate/"
            f"{self.config.latitude}/{self.config.longitude}/"
            f"{self.config.declination}/{self.config.azimuth}/"
            f"{self.config.kwp}/watts/{period}"
        )
        
        query_params = {}
        if self.config.damping > 0:
            query_params["damping"] = self.config.damping
        if self.config.inverter:
            query_params["inverter"] = self.config.inverter
        
        if query_params:
            url += "?" + urlencode(query_params)
        
        data = self._get(url)
        return self._parse_period_response(data, period)
    
    def get_check(self) -> Dict[str, Any]:
        """Check API status and rate limits."""
        url = f"{self.BASE_URL}/check"
        return self._get(url)
    
    def _parse_estimate_response(self, data: Dict) -> Dict[str, Any]:
        """Parse basic estimate response."""
        result = data.get("result", {})
        
        return {
            "api_version": data.get("message", {}).get("code", 0),
            "rate_limit_remaining": data.get("message", {}).get("ratelimit", {}).get("remaining", 0),
            "timezone": result.get("timezone"),
            "latitude": result.get("latitude"),
            "longitude": result.get("longitude"),
            "elevation": result.get("elevation"),
            "today": {
                "sunrise": result.get("sunrise"),
                "sunset": result.get("sunset"),
                "kwh": result.get("watt_hours_day", {}).get(self._today_str()),
            },
            "tomorrow": {
                "sunrise": result.get("sunrise"),
                "sunset": result.get("sunset"),
                "kwh": result.get("watt_hours_day", {}).get(self._tomorrow_str()),
            },
            "raw": data,
        }
    
    def _parse_watts_response(self, data: Dict) -> Dict[str, Any]:
        """Parse detailed watts response."""
        result = data.get("result", {})
        watts = result.get("watts", {})
        
        # Convert to list with timestamps
        forecast_list = []
        for timestamp_str, watt_value in sorted(watts.items()):
            forecast_list.append({
                "timestamp": timestamp_str,
                "datetime": datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")),
                "watts": watt_value,
                "kilowatts": round(watt_value / 1000, 3),
            })
        
        # Calculate totals
        today_str = self._today_str()
        tomorrow_str = self._tomorrow_str()
        
        today_watts = {
            k: v for k, v in watts.items() 
            if k.startswith(today_str)
        }
        tomorrow_watts = {
            k: v for k, v in watts.items() 
            if k.startswith(tomorrow_str)
        }
        
        # Each interval is typically 1 hour = 1Wh per W
        today_kwh = sum(today_watts.values()) / 1000
        tomorrow_kwh = sum(tomorrow_watts.values()) / 1000
        
        return {
            "timezone": result.get("timezone"),
            "intervals": forecast_list,
            "today": {
                "date": today_str,
                "total_kwh": round(today_kwh, 3),
                "peak_watts": max(today_watts.values()) if today_watts else 0,
                "intervals": len(today_watts),
            },
            "tomorrow": {
                "date": tomorrow_str,
                "total_kwh": round(tomorrow_kwh, 3),
                "peak_watts": max(tomorrow_watts.values()) if tomorrow_watts else 0,
                "intervals": len(tomorrow_watts),
            },
            "raw": data,
        }
    
    def _parse_period_response(self, data: Dict, period: str) -> Dict[str, Any]:
        """Parse period-aggregated response."""
        result = data.get("result", {})
        watts_period = result.get(f"watts_{period}", {})
        
        return {
            "period_type": period,
            "timezone": result.get("timezone"),
            "data": [
                {
                    "period": p,
                    "watts": w,
                    "kwh": round(w / 1000, 3) if period == "hourly" else round(w / 1000, 3),
                }
                for p, w in sorted(watts_period.items())
            ],
            "raw": data,
        }
    
    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")
    
    def _tomorrow_str(self) -> str:
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    # ============== Convenience Methods ==============
    
    def get_next_4_hours(self) -> Dict[str, Any]:
        """Get forecast for next 4 hours."""
        data = self.get_estimate_watts()
        now = datetime.now().astimezone()
        
        future_forecasts = [
            f for f in data["intervals"]
            if f["datetime"] > now
        ][:4]  # Next 4 hours
        
        total_kwh = sum(f["kilowatts"] for f in future_forecasts)
        
        return {
            "period": "next_4_hours",
            "current_time": now.isoformat(),
            "forecasts": future_forecasts,
            "total_expected_kwh": round(total_kwh, 3),
            "average_power_kw": round(total_kwh / len(future_forecasts), 3) if future_forecasts else 0,
        }
    
    def get_remaining_today(self) -> Dict[str, Any]:
        """Get forecast from now until sunset today."""
        data = self.get_estimate_watts()
        now = datetime.now().astimezone()
        today_str = self._today_str()
        
        remaining = [
            f for f in data["intervals"]
            if f["datetime"] > now and f["timestamp"].startswith(today_str)
        ]
        
        total_kwh = sum(f["kilowatts"] for f in remaining)
        
        # Find sunset (last non-zero production)
        production_periods = [f for f in remaining if f["kilowatts"] > 0.01]
        sunset = production_periods[-1]["timestamp"] if production_periods else "Unknown"
        
        return {
            "period": "remaining_today",
            "current_time": now.strftime("%H:%M"),
            "sunset_expected": sunset,
            "hours_remaining": len(remaining),
            "forecast_kwh": round(total_kwh, 3),
            "upcoming_hours": remaining[:6],  # Next 6 hours detail
            "is_declining": (
                remaining[3]["kilowatts"] < remaining[0]["kilowatts"] 
                if len(remaining) > 3 else False
            ),
        }
    
    def get_current_day(self) -> Dict[str, Any]:
        """Get full today forecast."""
        data = self.get_estimate_watts()
        return {
            "period": "current_day",
            "date": data["today"]["date"],
            "total_kwh": data["today"]["total_kwh"],
            "peak_kw": round(data["today"]["peak_watts"] / 1000, 3),
            "intervals": data["today"]["intervals"],
        }
    
    def get_tomorrow(self) -> Dict[str, Any]:
        """Get full tomorrow forecast."""
        data = self.get_estimate_watts()
        today = data["today"]["total_kwh"]
        tomorrow = data["tomorrow"]["total_kwh"]
        
        comparison = "similar"
        if tomorrow > today * 1.15:
            comparison = "better"
        elif tomorrow < today * 0.85:
            comparison = "worse"
        
        return {
            "period": "tomorrow",
            "date": data["tomorrow"]["date"],
            "total_kwh": data["tomorrow"]["total_kwh"],
            "peak_kw": round(data["tomorrow"]["peak_watts"] / 1000, 3),
            "comparison_to_today": comparison,
            "percent_of_today": round((tomorrow / today) * 100, 1) if today > 0 else 0,
        }


class ForecastSolarAdvancedClient(ForecastSolarClient):
    """
    Extended client with caching and multiple site support.
    Requires API key for professional features.
    """
    
    def __init__(self, config: ForecastSolarConfig, api_key: Optional[str] = None):
        super().__init__(config)
        self.api_key = api_key
        if api_key:
            self.session.headers["X-Digest"] = api_key
    
    def get_forecast_multiple_days(self, days: int = 3) -> Dict[str, Any]:
        """
        Get forecast for multiple days (requires paid API key).
        Free tier only provides today + tomorrow.
        """
        if not self.api_key:
            raise ValueError("API key required for multi-day forecasts")
        
        # Professional endpoint
        url = (
            f"{self.BASE_URL}/forecast/"
            f"{self.config.latitude}/{self.config.longitude}/"
            f"{self.config.declination}/{self.config.azimuth}/"
            f"{self.config.kwp}"
        )
        
        params = {"days": days}
        if self.config.damping > 0:
            params["damping"] = self.config.damping
        
        url += "?" + urlencode(params)
        return self._get(url)


# ── Legacy global stub (dead poll loop removed 2026-06-18) ───────────────────
# This module's SolarForecastManager.poll_loop() previously claimed to refresh
# the cached forecast every 30 min, but it was never started — no caller in
# main.py wired it into the app lifespan. The result: `cached_forecast` was
# always `{}`, and the only reader (smart_dispatch._build_context → solar
# manager.cached_forecast.get('forecast_kwh', 0.0)) silently received 0.0
# every time.
#
# The authoritative on-demand pipeline now lives in
# src/services/solar/manager.py (multi-provider, file-backed cache, 60-min TTL).
# This shim stays only so the smart_dispatch import doesn't break — the field
# remains empty until the read site is migrated to the new manager.
from typing import Optional


class SolarForecastManager:
    """Legacy stub — kept only so legacy `manager.cached_forecast` reads work.
    The real solar forecast pipeline is src/services/solar/manager.py."""
    def __init__(self):
        self.last_fetch: Optional[datetime] = None
        self.cached_forecast: Dict[str, Any] = {}


manager = SolarForecastManager()


