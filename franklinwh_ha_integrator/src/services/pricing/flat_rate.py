"""
Flat-rate fallback adapter.

Reads static import/export rates from configuration — useful when
no dynamic provider is configured, or as a fallback when the provider
API is unreachable.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.services.pricing.base import PricingAdapter, PriceSnapshot


class FlatRateAdapter(PricingAdapter):
    """Static flat tariff — no network calls."""

    def __init__(self, import_c_kwh: float = 25.0, export_c_kwh: float = -5.0,
                 tariff_type: str = "SHOULDER"):
        self._import = import_c_kwh
        self._export = export_c_kwh
        self._tariff = tariff_type

    async def get_snapshot(self) -> PriceSnapshot:
        now = datetime.now(tz=timezone.utc)
        return PriceSnapshot(
            provider="flat",
            import_c_kwh=self._import,
            export_c_kwh=self._export,
            tariff_type=self._tariff,
            spike_status="NONE",
            demand_window=False,
            renewables_pct=None,
            interval_min=30,
            valid_until=now + timedelta(hours=1),
            forecast=[],
        )

    async def test_connection(self) -> dict:
        return {
            "ok": True,
            "message": f"Flat rate: {self._import}¢ import / {self._export}¢ export",
            "import_c_kwh": self._import,
            "export_c_kwh": self._export,
        }
