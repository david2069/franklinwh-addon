"""
Pricing adapter base types.

All pricing adapters normalise provider-specific wire formats into
PriceSnapshot / PricePeriod, giving the rest of the app a single
unified interface regardless of provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PricePeriod:
    """One billing interval in the 24-hour forecast."""
    start: datetime
    end: datetime
    import_c_kwh: float           # buy price in ¢/kWh
    export_c_kwh: Optional[float] # sell price; None if provider doesn't supply
    tariff_type: str              # OFFPEAK | SHOULDER | PEAK | SPIKE
    renewables_pct: Optional[int] # 0-100, None if not available
    demand_window: bool = False   # True if this interval is inside a demand tariff window
    spike_status: str = "none"          # A1: none | potential | spike (Amber SpikeStatus.value)
    descriptor: str = "neutral"         # A2: raw Amber PriceDescriptor (negative/extremelyLow/veryLow/low/neutral/high/spike)
    tariff_period: Optional[str] = None # A3: offPeak | shoulder | solarSponge | peak
    tariff_season: Optional[str] = None # A3: default | summer | autumn | winter | spring | etc.


@dataclass
class PriceSnapshot:
    """Current price moment from any provider."""
    provider: str                   # 'amber' | 'localvolts' | 'comed' | 'flat'
    import_c_kwh: float             # buy price ¢/kWh
    export_c_kwh: Optional[float]   # sell price ¢/kWh (negative = earning)
    tariff_type: str                # OFFPEAK | SHOULDER | PEAK | SPIKE | UNKNOWN
    spike_status: str               # NONE | POTENTIAL | SPIKE
    demand_window: bool             # True if demand tariff window active
    renewables_pct: Optional[int]   # grid renewables percentage
    interval_min: int               # billing interval: 5 or 30
    valid_until: Optional[datetime] # end of this interval
    spot_c_kwh: Optional[float] = None # Wholesale AEMO spot price ¢/kWh
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    forecast: list[PricePeriod] = field(default_factory=list)
    export_penalty_is_positive: bool = False # True if positive export_c_kwh = customer debit (penalty)

    # Derived helpers
    @property
    def tariff_color(self) -> str:
        """CSS colour class for the current tariff type."""
        return {
            "OFFPEAK":    "#22c55e",
            "SHOULDER":   "#f59e0b",
            "PEAK":       "#f97316",
            "SPIKE":      "#ef4444",
            "UNKNOWN":    "#64748b",
        }.get(self.tariff_type, "#64748b")

    def to_dict(self) -> dict:
        stats = {
            "import": {"min": None, "max": None, "min_time": None, "max_time": None},
            "export": {"min": None, "max": None, "min_time": None, "max_time": None},
        }

        if self.forecast:
            # Import stats
            min_imp_period = min(self.forecast, key=lambda p: p.import_c_kwh)
            max_imp_period = max(self.forecast, key=lambda p: p.import_c_kwh)
            stats["import"]["min"] = min_imp_period.import_c_kwh
            stats["import"]["min_time"] = min_imp_period.start.isoformat()
            stats["import"]["max"] = max_imp_period.import_c_kwh
            stats["import"]["max_time"] = max_imp_period.start.isoformat()

            # Export stats
            export_periods = [p for p in self.forecast if p.export_c_kwh is not None]
            if export_periods:
                min_exp_period = min(export_periods, key=lambda p: p.export_c_kwh)
                max_exp_period = max(export_periods, key=lambda p: p.export_c_kwh)
                stats["export"]["min"] = min_exp_period.export_c_kwh
                stats["export"]["min_time"] = min_exp_period.start.isoformat()
                stats["export"]["max"] = max_exp_period.export_c_kwh
                stats["export"]["max_time"] = max_exp_period.start.isoformat()

        # FHP UI wants `import_forecast` and `export_forecast`. We unify them.
        import_forecast = []
        export_forecast = []
        for p in self.forecast:
            import_forecast.append({
                "start_time":    p.start.isoformat(),
                "price":         round(p.import_c_kwh, 3),
                "demand_window": p.demand_window,        # per-interval flag, not snapshot-level
                "tariff_band":   p.tariff_type.lower(),  # colour band (renamed from 'descriptor' to avoid collision)
                "renewables":    p.renewables_pct,
                "spike_status":  p.spike_status,
                "descriptor":    p.descriptor,
                "tariff_period": p.tariff_period,
                "tariff_season": p.tariff_season,
            })
            export_forecast.append({
                "start_time":    p.start.isoformat(),
                "price":         round(p.export_c_kwh, 3) if p.export_c_kwh is not None else 0,
                "demand_window": p.demand_window,        # per-interval flag
                "tariff_band":   p.tariff_type.lower(),  # colour band
                "renewables":    p.renewables_pct,
                "spike_status":  p.spike_status,
                "descriptor":    p.descriptor,
                "tariff_period": p.tariff_period,
                "tariff_season": p.tariff_season,
            })

        return {
            "provider": self.provider,
            "import_c_kwh": round(self.import_c_kwh, 5),
            "export_c_kwh": round(self.export_c_kwh, 5) if self.export_c_kwh is not None else None,
            "tariff_type": self.tariff_type,
            "tariff_color": self.tariff_color,
            "spike_status": self.spike_status,
            "demand_window": self.demand_window,
            "renewables_pct": self.renewables_pct,
            "interval_min": self.interval_min,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "fetched_at": self.fetched_at.isoformat(),
            "stats": stats,
            "current_period": {
                "import_price": round(self.import_c_kwh, 3),
                "export_price": round(self.export_c_kwh, 3) if self.export_c_kwh is not None else 0,
                "spot_price": round(self.spot_c_kwh, 3) if self.spot_c_kwh is not None else None,
                "renewables": self.renewables_pct,
                "tariff_info": {
                    "period": self.tariff_type,
                    "demand_window": self.demand_window
                }
            },
            "import_forecast": import_forecast,
            "export_forecast": export_forecast,
            "forecast": [
                {
                    "start":          p.start.isoformat(),
                    "end":            p.end.isoformat(),
                    "import_c_kwh":   round(p.import_c_kwh, 3),
                    "export_c_kwh":   round(p.export_c_kwh, 3) if p.export_c_kwh is not None else None,
                    "tariff_type":    p.tariff_type,
                    "renewables_pct": p.renewables_pct,
                    "demand_window":  p.demand_window,
                    "spike_status":   p.spike_status,
                    "descriptor":     p.descriptor,
                    "tariff_period":  p.tariff_period,
                    "tariff_season":  p.tariff_season,
                }
                for p in self.forecast
            ]
        }


class PricingAdapter(ABC):
    """Abstract base for all pricing provider adapters."""

    @abstractmethod
    async def get_snapshot(self) -> PriceSnapshot:
        """Fetch and return the current price snapshot."""
        ...

    async def test_connection(self) -> dict:
        """Test connectivity; return {"ok": bool, "message": str}."""
        try:
            snap = await self.get_snapshot()
            return {
                "ok": True,
                "message": f"Connected — import {snap.import_c_kwh:.2f}¢/kWh",
                "import_c_kwh": snap.import_c_kwh,
                "export_c_kwh": snap.export_c_kwh,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    @staticmethod
    def classify_tariff(price_c_kwh: float) -> str:
        """Classify a price in ¢/kWh into a tariff band."""
        if price_c_kwh < 0:
            return "OFFPEAK"
        elif price_c_kwh < 5:
            return "OFFPEAK"
        elif price_c_kwh < 15:
            return "SHOULDER"
        elif price_c_kwh < 30:
            return "PEAK"
        else:
            return "SPIKE"

    # ── B2: Tariff-Agnostic Normalisation ────────────────────────────────────
    # Default implementations work for any provider using price-band heuristics.
    # Amber overrides these with richer SDK values (spike_status, descriptor,
    # tariff_period from TariffInformation). All other adapters (LocalVolts,
    # AEMO, flat-rate) get working automation signal fields automatically.

    def normalize_descriptor(self, import_c_kwh: float) -> str:
        """Map any price to the universal PriceDescriptor vocabulary.

        Returns one of: negative | extremelyLow | veryLow | low | neutral | high | spike
        Adapters with provider-native descriptors (Amber) override this method.
        """
        if import_c_kwh < 0:    return "negative"
        if import_c_kwh < 5:    return "extremelyLow"
        if import_c_kwh < 10:   return "veryLow"
        if import_c_kwh < 20:   return "low"
        if import_c_kwh < 40:   return "neutral"
        if import_c_kwh < 100:  return "high"
        return "spike"

    def normalize_spike_status(self, import_c_kwh: float) -> str:
        """Map any price to spike | potential | none.

        Adapters with provider-native spike signals (Amber) override this method.
        """
        if import_c_kwh > 100:  return "spike"
        if import_c_kwh > 60:   return "potential"
        return "none"

    def normalize_tariff_period(self, tariff_type: str) -> Optional[str]:
        """Map a tariff_type band to a period label for automation rule matching.

        Returns one of: offPeak | shoulder | peak | None
        Amber overrides this with TariffInformation.period (which also includes solarSponge).
        """
        return {
            "OFFPEAK":  "offPeak",
            "SHOULDER": "shoulder",
            "PEAK":     "peak",
            "SPIKE":    "peak",
        }.get(tariff_type)
