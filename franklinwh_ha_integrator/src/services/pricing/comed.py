"""
ComEd Hourly Pricing adapter (US, Illinois, free — no auth required).

APIs used:
  5-min feed:    GET https://hourlypricing.comed.com/api?type=5minutefeed&format=json
  Current hour:  GET https://hourlypricing.comed.com/api?type=currenthouraverage&format=json

Response format:
  [{"millisUTC": "1774973700000", "price": "4.0"}, ...]
  → price is ¢/kWh (wholesale, can go negative)

Notes:
  - Import-only provider (US utility, no export/sell price)
  - 5-minute billing intervals
  - Last 24 hours of 5-min data available for forecast display
  - Prices can spike above 30¢ or go negative during solar surplus
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from src.services.pricing.base import PricingAdapter, PriceSnapshot, PricePeriod

logger = logging.getLogger(__name__)

_BASE   = "https://hourlypricing.comed.com/api"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


class ComedAdapter(PricingAdapter):
    """Adapter for ComEd Hourly Pricing (US). No credentials needed."""

    async def get_snapshot(self) -> PriceSnapshot:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            # Current 5-min price
            async with session.get(
                _BASE, params={"type": "5minutefeed", "format": "json"}
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"ComEd API error {resp.status}")
                feed: list[dict] = await resp.json()

        if not feed:
            raise RuntimeError("ComEd returned empty feed")

        # Feed is newest-first; [0] = current 5-min interval
        current = feed[0]
        import_c = float(current["price"])
        ts_ms    = int(current["millisUTC"])
        ts_dt    = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        valid_until = ts_dt + timedelta(minutes=5)

        tariff = self.classify_tariff(import_c)
        spike  = "SPIKE" if tariff == "SPIKE" else ("POTENTIAL" if import_c > 20 else "NONE")

        # Build forecast from last 24h historical data (reverse = oldest first)
        forecast_periods: list[PricePeriod] = []
        for entry in reversed(feed[1:]):
            try:
                p_c    = float(entry["price"])
                p_ts   = datetime.fromtimestamp(int(entry["millisUTC"]) / 1000, tz=timezone.utc)
                p_end  = p_ts + timedelta(minutes=5)
                tt     = self.classify_tariff(p_c)
                forecast_periods.append(PricePeriod(
                    start=p_ts,
                    end=p_end,
                    import_c_kwh=p_c,
                    export_c_kwh=None,   # ComEd has no export price
                    tariff_type=tt,
                    renewables_pct=None,
                ))
            except (KeyError, ValueError):
                continue

        return PriceSnapshot(
            provider="comed",
            import_c_kwh=import_c,
            export_c_kwh=None,
            tariff_type=tariff,
            spike_status=spike,
            demand_window=False,
            renewables_pct=None,
            interval_min=5,
            valid_until=valid_until,
            forecast=forecast_periods,
        )

    async def test_connection(self) -> dict:
        try:
            snap = await self.get_snapshot()
            return {
                "ok": True,
                "message": f"ComEd live — {snap.import_c_kwh:.2f}¢/kWh (no auth required)",
                "import_c_kwh": snap.import_c_kwh,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
