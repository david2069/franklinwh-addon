"""
HA Sensor Source — reads solar production data from Home Assistant entities.

Requires EXPLICIT entity IDs configured by the user in Solar Setup → HA Entities:
  - solar_actual_entity:   current live kW production  (e.g. sensor.fhp_solar_power)
  - solar_forecast_entity: remaining kWh today forecast (e.g. sensor.energy_production_today_remaining)

This source will NEVER auto-discover or guess entity IDs. If no entities are configured
is_available() returns False and the manager will fall back to another source or return [].
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class HASensorSource:
    """
    Pulls solar data directly from HA REST API states endpoint.
    Returns the same normalised slot format as other providers:
    [{"timestamp": ISO8601, "pv_kw": float, "period_mins": 30}, ...]
    """

    def __init__(self, config: Dict[str, Any]):
        self.ha_url: str = config.get("ha_url", "").rstrip("/")
        self.ha_token: str = config.get("ha_token", "")
        self.solar_actual_entity: str = config.get("solar_actual_entity", "").strip()
        self.solar_forecast_entity: str = config.get("solar_forecast_entity", "").strip()

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type":  "application/json",
        }

    def _get_state(self, entity_id: str) -> Optional[float]:
        """Fetch current numeric state for an explicitly named HA entity."""
        if not entity_id or not self.ha_url or not self.ha_token:
            return None
        url = f"{self.ha_url}/api/states/{entity_id}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=5)
            resp.raise_for_status()
            val = resp.json().get("state")
            return float(val)
        except (ValueError, TypeError):
            return None
        except Exception as exc:
            logger.debug(f"HASensorSource._get_state({entity_id}): {exc}")
            return None

    def get_forecast(self) -> List[Dict[str, Any]]:
        """
        Build a 48-slot (24h) solar profile using explicitly configured entity IDs only.
        - solar_actual_entity:   current live kW (anchors the current slot)
        - solar_forecast_entity: remaining kWh today (bell-curve distributed across daylight)
        Returns [] if no entity IDs are configured.
        """
        if not self.is_available():
            logger.warning("HASensorSource: no entity IDs configured — returning empty forecast")
            return []

        now = datetime.now(timezone.utc).astimezone()
        solar_noon_hour = 13

        live_kw       = self._get_state(self.solar_actual_entity) or 0.0
        remaining_kwh = self._get_state(self.solar_forecast_entity) or 0.0

        current_minute = now.minute
        start = now.replace(minute=30 if current_minute < 30 else 0, second=0, microsecond=0)
        if current_minute >= 30:
            start += timedelta(hours=1)

        today_date   = now.date()
        sunrise_hour = 6
        sunset_hour  = 20

        # Pre-compute Gaussian bell-curve weights for remaining daylight slots today
        day_weights: List[float] = []
        ts = start
        for _ in range(48):
            local_ts   = ts.astimezone()
            is_today   = local_ts.date() == today_date
            hour       = local_ts.hour
            is_daylight = sunrise_hour <= hour < sunset_hour
            if is_today and is_daylight:
                weight = math.exp(-0.5 * ((hour - solar_noon_hour) / 3.5) ** 2)
                day_weights.append(weight)
            else:
                day_weights.append(0.0)
            ts += timedelta(minutes=30)

        total_weight = sum(w for w in day_weights if w > 0)

        slots: List[Dict[str, Any]] = []
        ts = start
        for i in range(48):
            local_ts    = ts.astimezone()
            hour        = local_ts.hour
            is_today    = local_ts.date() == today_date
            is_daylight = sunrise_hour <= hour < sunset_hour

            if is_today and is_daylight and total_weight > 0:
                kwh_this_slot = remaining_kwh * (day_weights[i] / total_weight)
                pv_kw = round(kwh_this_slot / 0.5, 3)
                # Anchor the current slot to the live reading if available
                if i == 0 and live_kw > 0:
                    pv_kw = round((pv_kw + live_kw) / 2, 3)
            elif not is_today and is_daylight:
                # Tomorrow: flat proxy from today's total
                pv_kw = round(remaining_kwh / max(sunset_hour - sunrise_hour, 1) / 0.5, 3)
            else:
                pv_kw = 0.0

            slots.append({"timestamp": ts.isoformat(), "pv_kw": pv_kw, "period_mins": 30})
            ts += timedelta(minutes=30)

        logger.info(
            f"☀️  HASensorSource: {len(slots)} slots — "
            f"live={live_kw}kW (entity={self.solar_actual_entity or 'not set'}), "
            f"remaining={remaining_kwh}kWh (entity={self.solar_forecast_entity or 'not set'})"
        )
        return slots

    def is_available(self) -> bool:
        """Only available if the user has explicitly configured at least one entity ID."""
        return bool(
            self.ha_url and self.ha_token and
            (self.solar_actual_entity or self.solar_forecast_entity)
        )
