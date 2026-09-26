"""
Amber Electric pricing adapter.

Uses the official `amberelectric` Python SDK (same approach as the
HA core amberelectric integration coordinator).

Wire format:
  GET /v1/sites                               → [{id, nmi, ...}]
  api.get_current_prices(site_id, next=48)   → list of interval objects
    - CurrentInterval  (channel_type=GENERAL|FEEDIN|CONTROLLEDLOAD)
    - ForecastInterval (same channels, future periods)

Key fields per interval:
  .per_kwh          → ¢/kWh (already in cents)
  .channel_type     → ChannelType enum
  .spike_status     → SpikeStatus enum (none/potential/spike)
  .descriptor       → PriceDescriptor enum (negative/extremelyLow/veryLow/low/neutral/high/spike)
  .renewables       → int 0-100
  .start_time       → datetime
  .end_time         → datetime
  .tariff_information.demand_window → bool (if present)
  .tariff_information.period        → str  (offPeak/shoulder/solarSponge/peak)
  .tariff_information.season        → str  (default/summer/autumn/winter/spring/etc.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.services.pricing.base import PricingAdapter, PriceSnapshot, PricePeriod

logger = logging.getLogger(__name__)

_DESCRIPTOR_MAP = {
    "OFFPEAK":            "OFFPEAK",
    "SHOULDER":           "SHOULDER",
    "PEAK":               "PEAK",
    "HIGH":               "PEAK",
    "EXTREMELYEXPENSIVE": "SPIKE",
    "SPIKE":              "SPIKE",
    "VERYEXPENSIVE":      "PEAK",
    "CHEAP":              "OFFPEAK",
    "VERYFREE":           "OFFPEAK",
    "FREE":               "OFFPEAK",
    "EXCESSENERGY":       "OFFPEAK",
    "SUPEROFFPEAK":       "OFFPEAK",
}


def _to_str(val) -> str | None:
    """Safe coercion: returns .value if val is an enum, str(val) if not None."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return getattr(val, "value", str(val))


def _channel_is(channel_type_field, target_str: str) -> bool:
    """Compare a channel_type field (enum or string) to a target string."""
    s = _to_str(channel_type_field)
    return s == target_str if s else False


class AmberAdapter(PricingAdapter):
    """Adapter for Amber Electric (Australia). Requires API token."""

    def __init__(self, api_token: str, site_id: Optional[str] = None):
        self._token = api_token
        self._site_id = site_id  # cached after first auth
        self._site_cache: Optional[dict] = None  # cached site metadata

    async def _get_api(self):
        """Return initialised AmberApi instance."""
        try:
            import amberelectric
            configuration = amberelectric.Configuration(access_token=self._token)
            return amberelectric.AmberApi(amberelectric.ApiClient(configuration))
        except ImportError:
            raise RuntimeError(
                "amberelectric package not installed. "
                "Run: pip install amberelectric"
            )

    async def _resolve_site_id(self, api) -> str:
        if self._site_id:
            return self._site_id
        import asyncio
        sites = await asyncio.get_event_loop().run_in_executor(None, api.get_sites)
        if not sites:
            raise RuntimeError("No Amber sites found for this API token")
        self._site_id = sites[0].id
        logger.info(f"AmberAdapter: resolved site_id={self._site_id}")
        return self._site_id

    # ------------------------------------------------------------------
    # NEW: Site metadata
    # ------------------------------------------------------------------
    async def get_site_info(self) -> dict:
        """Return site metadata (NMI, network, interval_length, channels, status).

        Result is cached in-memory for the lifetime of the adapter instance.
        """
        if self._site_cache:
            return self._site_cache
        import asyncio
        api = await self._get_api()
        sites = await asyncio.get_event_loop().run_in_executor(None, api.get_sites)
        if not sites:
            raise RuntimeError("No Amber sites found for this API token")
        s = sites[0]
        self._site_id = s.id
        self._site_cache = {
            "site_id":         s.id,
            "nmi":             s.nmi,
            "network":         getattr(s, "network", None),
            "status":          s.status.value if s.status else None,
            "interval_length": getattr(s, "interval_length", 30),
            "active_from":     getattr(s, "active_from", None),
            "channels": [
                {
                    "identifier": ch.identifier,
                    "type":       ch.type.value if ch.type else None,
                    "tariff":     getattr(ch, "tariff", None),
                }
                for ch in (s.channels or [])
            ],
        }
        logger.info(f"AmberAdapter: site_info cached for {s.id}")
        return self._site_cache

    # ------------------------------------------------------------------
    # NEW: Usage history
    # ------------------------------------------------------------------
    async def get_usage(self, start_date: str, end_date: str) -> dict:
        """Return actual usage data for a date range (max 7 days per API limit).

        Args:
            start_date: ISO date string YYYY-MM-DD
            end_date:   ISO date string YYYY-MM-DD

        Returns:
            {"general": [UsageRecord...], "feed_in": [UsageRecord...]}
            UsageRecord: {start_time, end_time, kwh, cost, renewables, tariff_type, quality}
        """
        import asyncio
        try:
            from amberelectric.models.channel import ChannelType
        except ImportError:
            raise RuntimeError("amberelectric package not installed")

        api = await self._get_api()
        site_id = await self._resolve_site_id(api)

        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.get_usage(
                site_id, start_date=start_date, end_date=end_date, _request_timeout=15
            )
        )

        general_usage: list[dict] = []
        feedin_usage:  list[dict] = []
        for item in (raw or []):
            u = item.actual_instance if hasattr(item, "actual_instance") else item
            record = {
                "start_time":         u.start_time.isoformat() if u.start_time else None,
                "end_time":           u.end_time.isoformat()   if u.end_time   else None,
                "kwh":                float(u.kwh)             if u.kwh   is not None else 0.0,
                "cost":               float(u.cost)            if u.cost  is not None else 0.0,
                "renewables":         round(u.renewables)      if u.renewables is not None else None,
                "tariff_type":        _DESCRIPTOR_MAP.get((_to_str(getattr(u, "descriptor", None)) or "").upper(), "UNKNOWN"),
                "quality":            _to_str(getattr(u, "quality", None)),
                "channel_identifier": _to_str(getattr(u, "channel_type", None)),
            }
            ct = _to_str(getattr(u, "channel_type", None))
            if ct in ("feedIn", "FEEDIN", "B1") or _channel_is(ct, "feedIn"):
                feedin_usage.append(record)
            else:
                general_usage.append(record)

        return {"general": general_usage, "feed_in": feedin_usage}

    # ------------------------------------------------------------------
    # NEW: Historical prices
    # ------------------------------------------------------------------
    async def get_prices(self, start_date: str, end_date: str) -> dict:
        """Return historical price data for a date range (max 7 days per API limit).

        Returns:
            {"general": [PriceRecord...], "feed_in": [PriceRecord...]}
            PriceRecord: {start_time, end_time, per_kwh, spot_per_kwh, renewables, tariff_type, type}
        """
        import asyncio
        try:
            from amberelectric.models.channel import ChannelType
        except ImportError:
            raise RuntimeError("amberelectric package not installed")

        api = await self._get_api()
        site_id = await self._resolve_site_id(api)

        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.get_prices(
                site_id, start_date=start_date, end_date=end_date, _request_timeout=15
            )
        )

        general_prices: list[dict] = []
        feedin_prices:  list[dict] = []
        for item in (raw or []):
            p = item.actual_instance if hasattr(item, "actual_instance") else item
            record = {
                "start_time":   p.start_time.isoformat()  if p.start_time else None,
                "end_time":     p.end_time.isoformat()    if p.end_time   else None,
                "per_kwh":      float(p.per_kwh)          if p.per_kwh    is not None else 0.0,
                "spot_per_kwh": float(p.spot_per_kwh)     if getattr(p, "spot_per_kwh", None) is not None else None,
                "renewables":   round(p.renewables)       if p.renewables is not None else None,
                "tariff_type":  _DESCRIPTOR_MAP.get((_to_str(getattr(p, "descriptor", None)) or "").upper(), "UNKNOWN"),
                "type":         _to_str(getattr(p, "type", "ActualInterval")),
            }
            ct = _to_str(getattr(p, "channel_type", None))
            if ct in ("feedIn", "FEEDIN", "B1") or _channel_is(ct, "feedIn"):
                record["per_kwh"] = -record["per_kwh"]
                feedin_prices.append(record)
            else:
                general_prices.append(record)

        return {"general": general_prices, "feed_in": feedin_prices}

    # ------------------------------------------------------------------
    # Live snapshot (updated: includes previous=12 actuals for price strip)
    # ------------------------------------------------------------------
    async def get_snapshot(self) -> PriceSnapshot:
        import asyncio
        try:
            import amberelectric
            from amberelectric.models.channel import ChannelType
        except ImportError:
            raise RuntimeError("amberelectric package not installed. Run: pip install amberelectric")

        api = await self._get_api()
        site_id = await self._resolve_site_id(api)

        # Fetch current + 288 forecast periods (24 hours at 5-min intervals) + 12 actual actuals
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.get_current_prices(
                site_id, next=288, previous=12, resolution=5, _request_timeout=10
            )
        )
        intervals = [i.actual_instance for i in raw]

        from amberelectric.models.current_interval import CurrentInterval
        from amberelectric.models.forecast_interval import ForecastInterval

        current = sorted([i for i in intervals if isinstance(i, CurrentInterval)], key=lambda x: getattr(x, "start_time", ""))
        forecasts = [i for i in intervals if isinstance(i, ForecastInterval)]

        # --- General (import) channel ---
        general = [i for i in current if _channel_is(getattr(i, "channel_type", None), "general") or
                   _to_str(getattr(i, "channel_type", None)) in ("general", "GENERAL", "E1")]
        if not general:
            # Fallback: any non-feedIn interval is general
            general = [i for i in current if _to_str(getattr(i, "channel_type", None)) not in ("feedIn", "FEEDIN", "B1")]
        if not general:
            raise RuntimeError("Amber: No GENERAL channel in current prices")
        g = general[-1]

        import_c = float(g.per_kwh)
        spot_price = float(g.spot_per_kwh) if hasattr(g, 'spot_per_kwh') and g.spot_per_kwh is not None else None
        spike_str = g.spike_status.value if g.spike_status else "NONE"
        tariff_str = _DESCRIPTOR_MAP.get(
            (g.descriptor or "").upper(), self.classify_tariff(import_c)
        )
        renewables = round(g.renewables) if g.renewables is not None else None
        demand_window = False
        if g.tariff_information and g.tariff_information.demand_window is not None:
            demand_window = bool(g.tariff_information.demand_window)

        # --- Feed-in (export) channel ---
        feed_in = [i for i in current if _to_str(getattr(i, "channel_type", None)) in ("feedIn", "FEEDIN", "B1")]
        export_c: Optional[float] = None
        if feed_in:
            export_c = -float(feed_in[-1].per_kwh)

        # --- Build forecast ---
        gen_forecasts = [i for i in forecasts if _to_str(getattr(i, "channel_type", None)) not in ("feedIn", "FEEDIN", "B1")]
        fi_forecasts  = {
            i.start_time: -float(i.per_kwh)
            for i in forecasts
            if _to_str(getattr(i, "channel_type", None)) in ("feedIn", "FEEDIN", "B1")
        }

        forecast_periods: list[PricePeriod] = []
        for fi in gen_forecasts:
            imp = float(fi.per_kwh)
            exp = fi_forecasts.get(fi.start_time)
            tt  = _DESCRIPTOR_MAP.get(
                (fi.descriptor or "").upper(), self.classify_tariff(imp)
            )
            # Extract per-interval signals — follow the demand_window pattern exactly
            fi_spike  = _to_str(getattr(fi, "spike_status", None)) or "none"
            fi_desc   = _to_str(getattr(fi, "descriptor",   None)) or "neutral"
            fi_demand = False
            fi_period = None
            fi_season = None
            if (ti := getattr(fi, "tariff_information", None)) is not None:
                fi_demand = bool(getattr(ti, "demand_window", False) or False)
                fi_period = _to_str(getattr(ti, "period",     None))
                fi_season = _to_str(getattr(ti, "season",     None))
            forecast_periods.append(PricePeriod(
                start=fi.start_time,
                end=fi.end_time,
                import_c_kwh=imp,
                export_c_kwh=exp,
                tariff_type=tt,
                renewables_pct=round(fi.renewables) if fi.renewables is not None else None,
                demand_window=fi_demand,
                spike_status=fi_spike,
                descriptor=fi_desc,
                tariff_period=fi_period,
                tariff_season=fi_season,
            ))

        # valid_until = end of current interval
        valid_until = g.end_time if hasattr(g, "end_time") else None

        return PriceSnapshot(
            provider="amber",
            import_c_kwh=import_c,
            export_c_kwh=export_c,
            spot_c_kwh=spot_price,
            tariff_type=tariff_str,
            spike_status=spike_str,
            demand_window=demand_window,
            renewables_pct=renewables,
            interval_min=5,
            valid_until=valid_until,
            forecast=forecast_periods,
            export_penalty_is_positive=False, # Amber uses negative values for export penalty (as per real API data)
        )

    async def test_connection(self) -> dict:
        try:
            import asyncio
            api = await self._get_api()
            try:
                from amberelectric.rest import ApiException
            except ImportError:
                ApiException = Exception

            try:
                sites = await asyncio.get_event_loop().run_in_executor(None, api.get_sites)
            except ApiException as ae:
                status = getattr(ae, 'status', None)
                if status == 401:
                    return {"ok": False, "message": "Invalid API token — check your Amber Developer Dashboard"}
                elif status == 404:
                    return {"ok": False, "message": "No Amber sites found for this token (HTTP 404)"}
                return {"ok": False, "message": f"Amber API error {status}: {getattr(ae,'reason',str(ae))}"}

            if not sites:
                return {"ok": False, "message": "Token valid but no sites found on this account"}
            self._site_id = sites[0].id
            snap = await self.get_snapshot()
            return {
                "ok": True,
                "message": f"✓ Connected to site {self._site_id} — {snap.import_c_kwh:.2f}¢/kWh import",
                "site_id": self._site_id,
                "nmi": sites[0].nmi,
                "network": getattr(sites[0], "network", None),
                "import_c_kwh": snap.import_c_kwh,
                "export_c_kwh": snap.export_c_kwh,
            }
        except RuntimeError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"Connection failed: {exc}"}
