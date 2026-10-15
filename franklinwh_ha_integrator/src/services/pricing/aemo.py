"""
AEMO NEM spot price adapter (Australia — all NEM regions).

Data source: AEMO NEM Dashboard visualisation API (public, unauthenticated).
Library:     aemonemdata >= 2024.11.0  (pip install aemonemdata)

Regions:  nsw | qld | vic | sa | tas
No account, API key, or registration required.

Key data provided:
  current_5min_period_price  — actual 5-min spot ($/kWh)
  current_30min_estimated    — blend of actuals + forecast for current window ($/kWh)
  forecast[]                 — future 30-min window prices
  apc_flag                   — Administered Price Cap active (market stress indicator)
  market_suspended_flag      — full market suspension
  current_percent_cumulative_price — % of cumulative threshold consumed

Export price semantics:
  AEMO is a wholesale settlement market. There is no separate FiT concept.
  When spot price is negative, prosumers are effectively *paid* to consume and
  *charged* to export. We set export_c_kwh = import_c_kwh so that existing
  negative_export and negative_export_advisory engine rules fire correctly.

Forecast depth:
  The aemonemdata library returns the current 30-min window actuals + all
  available forecast 30-min periods (typically 24–48 periods = 12–24 hours
  of lookahead, but AEMO only publishes a few hours in practice).

Enrichment mode (BL-008 Mode B):
  When used as a secondary source alongside another provider, the service
  layer populates snap.spot_c_kwh from this adapter's current_5min_period_price.
  See PricingService._enrich_with_aemo() — wired if enrichment_region is set.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any


from src.services.pricing.base import PricingAdapter, PriceSnapshot, PricePeriod

logger = logging.getLogger(__name__)

# AEMO NEM region codes (case-insensitive input → library format)
VALID_REGIONS = {"nsw", "qld", "vic", "sa", "tas"}
_REGION_LABELS = {
    "nsw": "NSW — New South Wales",
    "qld": "QLD — Queensland",
    "vic": "VIC — Victoria",
    "sa":  "SA — South Australia",
    "tas": "TAS — Tasmania",
}

# $/kWh → ¢/kWh
_DOLLAR_TO_CENTS = 100.0


class AEMOAdapter(PricingAdapter):
    """
    Native AEMO NEM spot price adapter.

    No credentials required. Requires 'aemonemdata' package.

    Args:
        region: NEM region — 'nsw' | 'qld' | 'vic' | 'sa' | 'tas'
    """

    def __init__(self, region: str = "nsw"):
        region = region.lower().strip()
        if region not in VALID_REGIONS:
            raise ValueError(
                f"AEMO: invalid region '{region}'. Valid: {sorted(VALID_REGIONS)}"
            )
        self._region = region

    @property
    def region_label(self) -> str:
        return _REGION_LABELS.get(self._region, self._region.upper())

    async def get_snapshot(self) -> PriceSnapshot:
        """Fetch current AEMO NEM data and return a normalised PriceSnapshot."""
        from aemonemdata import AemoNemData, REGIONS

        # Let aemonemdata manage its own ClientSession internally.
        # Passing an outer session causes timeout inheritance issues when the library
        # makes multiple sequential HTTP calls — each borrowing from the same timeout budget.
        # With session_manage=True (the default when no session is passed), the library
        # opens and closes its own session per get_aemo_data() call.
        client = AemoNemData()   # no session argument → library creates its own
        raw = await client.get_aemo_data([self._region])

        region_key = REGIONS[self._region]          # e.g. "NSW1"
        rdata: dict[str, Any] = raw["current_30min_forecast"][region_key]

        # ── Prices ($/kWh → ¢/kWh) ──────────────────────────────────────────
        # current_30min_estimated is the best current-window price estimate:
        #   = (sum of settled 5-min actuals + forecast × remaining slots) / 6
        import_c = rdata["current_30min_estimated"] * _DOLLAR_TO_CENTS

        # 5-min raw spot for informational display
        spot_c = rdata["current_5min_period_price"] * _DOLLAR_TO_CENTS

        # AEMO is a settlement market: export price = same spot price.
        # Negative = generator/prosumer earns money, consumer is charged.
        export_c = import_c

        # ── Market stress flags ──────────────────────────────────────────────
        apc_active   = bool(rdata.get("apc_flag", False))
        mkt_suspended = bool(rdata.get("market_suspended_flag", False))
        cum_pct       = rdata.get("current_percent_cumulative_price", 0.0) or 0.0

        # Demand window semantics: treat APC activation as equivalent to a demand event
        demand_window = apc_active

        # Spike detection — use both price bands AND market stress signals
        spike_status = self._aemo_spike_status(import_c, apc_active, mkt_suspended, cum_pct)

        # ── Forecast periods ─────────────────────────────────────────────────
        forecast = self._build_forecast(rdata.get("forecast", []))

        # ── Valid until: end of current 30-min window ────────────────────────
        valid_until: Optional[datetime] = None
        try:
            settlement_str = rdata.get("settlement_date_str", "")
            if settlement_str:
                valid_until = datetime.fromisoformat(settlement_str + "+10:00")
        except (ValueError, TypeError):
            pass

        tariff = self.classify_tariff(import_c)

        logger.debug(
            f"AEMO [{self._region.upper()}]: import={import_c:.3f}¢ spot={spot_c:.3f}¢ "
            f"tariff={tariff} apc={apc_active} cum_pct={cum_pct:.1f}%"
        )

        return PriceSnapshot(
            provider="aemo",
            import_c_kwh=import_c,
            export_c_kwh=export_c,
            tariff_type=tariff,
            spike_status=spike_status,
            demand_window=demand_window,
            renewables_pct=None,     # AEMO provides gen mix but not a single % renewable
            interval_min=5,
            valid_until=valid_until,
            spot_c_kwh=spot_c,
            forecast=forecast,
        )

    def _aemo_spike_status(
        self,
        import_c: float,
        apc_active: bool,
        mkt_suspended: bool,
        cum_pct: float,
    ) -> str:
        """
        AEMO-aware spike detection.

        Uses price bands from base class PLUS AEMO market stress signals:
          - Market suspended or APC triggered → always SPIKE
          - Cumulative price > 80% of threshold → POTENTIAL
          - Otherwise: delegate to price-band heuristic
        """
        if mkt_suspended or apc_active:
            return "spike"
        if cum_pct >= 80.0:
            return "potential"
        return self.normalize_spike_status(import_c)

    def _build_forecast(self, raw_forecast: list) -> list[PricePeriod]:
        """Convert AEMO forecast list to PricePeriod objects."""
        periods = []
        for item in raw_forecast:
            try:
                start: datetime = item["start_time"]
                end: datetime   = item["end_time"]
                price_dollar    = item.get("price", 0.0) or 0.0
                import_c        = price_dollar * _DOLLAR_TO_CENTS
                export_c        = import_c   # AEMO: same price both directions
                tariff          = self.classify_tariff(import_c)

                periods.append(PricePeriod(
                    start=start,
                    end=end,
                    import_c_kwh=import_c,
                    export_c_kwh=export_c,
                    tariff_type=tariff,
                    renewables_pct=None,
                    spike_status=self.normalize_spike_status(import_c),
                    descriptor=self.normalize_descriptor(import_c),
                    tariff_period=self.normalize_tariff_period(tariff),
                ))
            except Exception as exc:
                logger.debug(f"AEMO forecast period skipped: {exc}")
        return periods

    async def test_connection(self) -> dict:
        """Test AEMO API connectivity. No credentials needed — just a live fetch."""
        try:
            snap = await self.get_snapshot()
            return {
                "ok": True,
                "message": (
                    f"Connected — {self.region_label}: "
                    f"import={snap.import_c_kwh:.3f}¢/kWh  "
                    f"spot={snap.spot_c_kwh:.3f}¢/kWh  "
                    f"tariff={snap.tariff_type}  "
                    f"forecast_periods={len(snap.forecast)}"
                ),
                "import_c_kwh":      snap.import_c_kwh,
                "spot_c_kwh":        snap.spot_c_kwh,
                "export_c_kwh":      snap.export_c_kwh,
                "tariff_type":       snap.tariff_type,
                "spike_status":      snap.spike_status,
                "demand_window":     snap.demand_window,
                "forecast_periods":  len(snap.forecast),
                "region":            self._region,
            }
        except Exception as exc:
            # TimeoutError / CancelledError have empty str() — always include type name
            exc_str = str(exc) or type(exc).__name__
            msg = f"AEMO fetch failed for {self._region.upper()}: {exc_str}"
            logger.warning(msg)
            return {
                "ok": False,
                "message": msg,
                "region": self._region,
            }
