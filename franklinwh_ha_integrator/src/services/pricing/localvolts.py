"""
LocalVolts pricing adapter (Australia, 5-minute intervals).

API: GET https://api.localvolts.com/v1/partner/interval
Auth: api_key + partner_id + nmi_id query params

Key fields (per HA integration):
  costsFlexUp    → import cost in $/kWh × 100 (i.e. divide by 100 to get ¢/kWh)
  earningsFlexUp → export earn in $/kWh × 100
  demandInterval → 0 | 1  (demand tariff window active)
  intervalEnd    → ISO timestamp (end of 5-min interval)

No forecast available — returns empty list.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from src.services.pricing.base import PricingAdapter, PriceSnapshot, PricePeriod

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.localvolts.com/v1/partner/interval"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


class LocalVoltsAdapter(PricingAdapter):
    """Adapter for LocalVolts (AU). Requires api_key, partner_id, nmi_id."""

    def __init__(self, api_key: str, partner_id: str, nmi_id: str):
        self._api_key    = api_key
        self._partner_id = partner_id
        self._nmi_id     = nmi_id

    async def get_snapshot(self) -> PriceSnapshot:
        params = {
            "nmi":       self._nmi_id,
            "apiKey":    self._api_key,
            "partner":   self._partner_id,
        }
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(_BASE_URL, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"LocalVolts API error {resp.status}: {text[:200]}")
                data = await resp.json()

        if not data:
            raise RuntimeError("LocalVolts returned empty response")

        # API returns a list; take the most recent interval
        record = data[0] if isinstance(data, list) else data

        # costsFlexUp and earningsFlexUp are in $/kWh × 100 → ¢/kWh
        import_c  = float(record.get("costsFlexUp", 0)) / 100
        export_c  = float(record.get("earningsFlexUp", 0)) / 100
        demand    = bool(int(record.get("demandInterval", 0)))

        # intervalEnd is ISO string
        interval_end_str = record.get("intervalEnd")
        valid_until: Optional[datetime] = None
        if interval_end_str:
            try:
                valid_until = datetime.fromisoformat(interval_end_str)
            except ValueError:
                pass

        tariff = self.classify_tariff(import_c)
        # LocalVolts is 5-minute intervals
        return PriceSnapshot(
            provider="localvolts",
            import_c_kwh=import_c,
            export_c_kwh=export_c,
            tariff_type=tariff,
            spike_status="SPIKE" if tariff == "SPIKE" else "NONE",
            demand_window=demand,
            renewables_pct=None,
            interval_min=5,
            valid_until=valid_until,
            forecast=[],  # LocalVolts has no forecast endpoint
        )

    async def test_connection(self) -> dict:
        try:
            snap = await self.get_snapshot()
            return {
                "ok": True,
                "message": f"Connected — import {snap.import_c_kwh:.4f}¢/kWh (NMI {self._nmi_id})",
                "import_c_kwh": snap.import_c_kwh,
                "export_c_kwh": snap.export_c_kwh,
                "demand_window": snap.demand_window,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
