"""
FranklinWH TOU (Time-of-Use) pricing adapter.

Reads the active TOU block directly from the device's cloud TOU schedule via get_current_tou_price().
Builds a schedule-based 24h forecast from the FranklinWH touDispatchList.

Config (provider settings):
  gateway_id:        str    — short_id of the gateway to read (default: first registered)

No credentials required — reads from the in-process GatewayService.

FranklinWH wave_type integer → tariff band:
  0 → Super Off-Peak  → OFFPEAK
  1 → Off-Peak        → OFFPEAK
  2 → Shoulder        → SHOULDER
  3 → Peak            → PEAK
  4 → Super Peak      → SPIKE (treated as spike-level)
  -1/None → UNKNOWN (device not in TOU mode or data not yet available)

FranklinWH dispatch_code → automation hint:
  1 → Charge (force charge)
  2 → Discharge (force discharge / sell)
  3 → Hold (maintain SOC)
  Other → Neutral
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from src.services.pricing.base import PricingAdapter, PriceSnapshot, PricePeriod

logger = logging.getLogger(__name__)

# FranklinWH wave_type → our tariff band.
#
# Corrected to match the gateway. This map previously read 2=SHOULDER and
# 4=SPIKE, while the schedule editor's _waveRateKey and franklinwh-cloud's
# RATE_FIELD_MAP both have 2=Peak and 4=Super Off-Peak — so an On-Peak block
# was priced as shoulder and a Super Off-Peak block as a price spike. The
# authority is the rate field each wave writes to, and that is what this
# follows.
_WAVE_TO_TARIFF = {
    0: "OFFPEAK",    # Off-Peak       → eleticRateValley
    1: "SHOULDER",   # Mid-Peak       → eleticRateShoulder
    2: "PEAK",       # On-Peak        → eleticRatePeak
    3: "SPIKE",      # Sharp          → eleticRateSharp
    4: "OFFPEAK",    # Super Off-Peak → eleticRateSuperOffPeak
}

# waveType → the suffix of its rate field on a dayTypeVoList entry. This is the
# canonical relationship; _WAVE_TO_TARIFF above is derived from it.
_WAVE_RATE_SUFFIX = {
    0: "Valley",
    1: "Shoulder",
    2: "Peak",
    3: "Sharp",
    4: "SuperOffPeak",
}

# Human-readable band labels (matching gateway_service._WAVE_TYPE_LABELS)
_WAVE_LABELS = {
    0: "Off-Peak",
    1: "Mid-Peak",
    2: "On-Peak",
    3: "Sharp",
    4: "Super Off-Peak",
}

# FranklinWH dispatch_code → tariff_period label (automation engine compatibility)
_DISPATCH_TO_PERIOD = {
    1: "offPeak",    # charge = off-peak window
    2: "peak",       # discharge = peak window (sell)
    3: "shoulder",   # hold = shoulder
}


class FranklinWHTOUAdapter(PricingAdapter):
    """
    FranklinWH TOU schedule as a pricing adapter.

    No external API keys required. Reads from the GatewayService in-process
    using get_current_tou_price() to fetch rates configured in the app.
    """

    def __init__(
        self,
        gateway_id: Optional[str] = None,
    ):
        self._gateway_id = gateway_id
        self._buy_rates = {}
        self._sell_rates = {}

    def _rate_for_tariff(self, tariff: str) -> float:
        rate = {
            "OFFPEAK":  self._buy_rates.get("valley", 0.0),
            "SHOULDER": self._buy_rates.get("shoulder", 0.0),
            "PEAK":     self._buy_rates.get("peak", 0.0),
            "SPIKE":    self._buy_rates.get("sharp", 0.0),
        }.get(tariff, 0.0)
        return float(rate) * 100.0 if float(rate) < 10.0 else float(rate)

    def _sell_rate_for_tariff(self, tariff: str) -> float:
        rate = {
            "OFFPEAK":  self._sell_rates.get("valley", 0.0),
            "SHOULDER": self._sell_rates.get("shoulder", 0.0),
            "PEAK":     self._sell_rates.get("peak", 0.0),
            "SPIKE":    self._sell_rates.get("sharp", 0.0),
        }.get(tariff, 0.0)
        return float(rate) * 100.0 if float(rate) < 10.0 else float(rate)

    def _resolve_gateway(self):
        """Get the GatewayService for the configured (or primary) gateway."""
        from src.main import get_app_state
        registry = get_app_state().get("registry")

        if not registry:
            raise RuntimeError("No gateway registered — FranklinWH TOU requires a configured gateway")

        if self._gateway_id:
            svc = registry.get_gateway(self._gateway_id)
            if svc is None:
                raise RuntimeError(
                    f"FranklinWH TOU: gateway '{self._gateway_id}' not found in registry"
                )
            return svc

        # Default: first registered gateway (most users have exactly one)
        svc = next(iter(registry._services.values()), None)
        if svc is None:
            raise RuntimeError("FranklinWH TOU: gateway registry is empty")
        return svc

    async def get_snapshot(self) -> PriceSnapshot:
        """Derive current price from the active FranklinWH TOU block."""
        svc = self._resolve_gateway()
        client = await svc._get_or_create_client()

        live_price = await client.get_current_tou_price()

        if not live_price:
            logger.warning("FranklinWH TOU: get_current_tou_price returned empty.")
            return PriceSnapshot(
                provider="franklinwh_tou",
                import_c_kwh=0.0,
                export_c_kwh=0.0,
                tariff_type="SHOULDER",
                spike_status="NONE",
                demand_window=False,
                renewables_pct=None,
                interval_min=30,
                valid_until=None,
                spot_c_kwh=None,
                forecast=[]
            )

        wave_type      = live_price.get("wave_type")
        dispatch_code  = live_price.get("dispatch_id")
        block_name     = live_price.get("block_name", "")
        end_time_str   = live_price.get("block_end", "")
        
        self._buy_rates = live_price.get("buy_rates", {})
        self._sell_rates = live_price.get("sell_rates", {})

        raw_import = float(live_price.get("current_buy_rate") or 0.0)
        raw_export = float(live_price.get("current_sell_rate") or 0.0)
        
        import_c = raw_import * 100.0 if raw_import < 10.0 else raw_import
        export_c = raw_export * 100.0 if raw_export < 10.0 else raw_export

        tariff = _WAVE_TO_TARIFF.get(int(wave_type) if wave_type is not None else -1, "SHOULDER")

        # Valid until: parse end time string from TOU block
        valid_until: Optional[datetime] = None
        if end_time_str:
            try:
                # FranklinWH returns HH:MM strings for intra-day blocks
                now = datetime.now(timezone.utc)
                hh, mm = map(int, end_time_str.split(":"))
                candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                # If the end time is earlier than now, it wraps to tomorrow
                if candidate < now:
                    candidate += timedelta(days=1)
                valid_until = candidate
            except Exception:
                pass

        # Forecast from the stored TOU schedule — see _build_schedule_forecast.
        forecast = self._build_schedule_forecast(await self._load_strategy_list())

        spike_status = self.normalize_spike_status(import_c)
        descriptor   = self.normalize_descriptor(import_c)
        tariff_period = _DISPATCH_TO_PERIOD.get(
            int(dispatch_code) if dispatch_code is not None else -1,
            self.normalize_tariff_period(tariff),
        )

        logger.debug(
            f"FranklinWH TOU [{svc.short_id}]: wave={wave_type} "
            f"({_WAVE_LABELS.get(wave_type, '?')}) → import={import_c:.2f}¢ "
            f"tariff={tariff} block='{block_name}'"
        )

        return PriceSnapshot(
            provider="franklinwh_tou",
            import_c_kwh=import_c,
            export_c_kwh=export_c,
            tariff_type=tariff,
            spike_status=spike_status,
            demand_window=(tariff in ("PEAK", "SPIKE")),
            renewables_pct=None,
            interval_min=30,           # FranklinWH TOU blocks are 30-min aligned
            valid_until=valid_until,
            spot_c_kwh=None,           # No wholesale spot — user-defined rates only
            forecast=forecast,
        )

    # ── Schedule-derived forecast ────────────────────────────────────────
    #
    # The previous implementation read cached["touDispatchList"] out of the
    # gateway's poll payload. That key only ever appears inside
    # `detailDefaultVo` of the TOU detail response and is never placed in
    # last_data, so the lookup always returned [] and this provider shipped an
    # empty forecast for its entire life — which is what the Pricing tab has
    # been reporting as "No forecast data from provider".
    #
    # The schedule itself is persisted per gateway in tou_snapshots as
    # strategy_json: seasons, each with month sets and dayTypeVoList entries
    # carrying both the blocks and their rates. That is the real source, it
    # needs no extra API call, and it is the same data the Schedule tab edits.

    @staticmethod
    def _to_cents(value) -> float:
        """Gateway rates are $/kWh; PricePeriod wants cents.

        Guarded rather than multiplied blindly: a value already in cents would
        otherwise be inflated a hundredfold, and both units appear in the wild
        depending on how a schedule was written.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        return v * 100.0 if abs(v) < 10.0 else v

    @staticmethod
    def _season_for(strategy_list: list, day) -> Optional[dict]:
        """The season whose month set contains this date.

        Months are a CSV and need not be contiguous — AGL's peak season is
        Nov-Mar *and* Jun-Aug — so this is membership, not a range check.
        """
        for season in strategy_list:
            months = {
                int(m) for m in str(season.get("month") or "").split(",")
                if m.strip().isdigit()
            }
            if day.month in months:
                return season
        return strategy_list[0] if strategy_list else None

    @staticmethod
    def _daytype_for(season: dict, day) -> Optional[dict]:
        """Weekday/weekend entry for this date, falling back to everyday.

        Chosen per date rather than once, so a forecast that crosses into
        Saturday picks up the weekend rates instead of projecting Friday's.
        """
        entries = season.get("dayTypeVoList") or []
        if not entries:
            return None
        wanted = 1 if day.weekday() < 5 else 2          # 1=weekday 2=weekend
        for entry in entries:
            if entry.get("dayType") == wanted:
                return entry
        for entry in entries:
            if entry.get("dayType") == 3:               # 3=everyday
                return entry
        return entries[0]

    def _rates_for_wave(self, daytype: dict, wave) -> tuple[float, float]:
        """Buy and sell for a wave, read off the season's own rate fields.

        Deliberately not via self._buy_rates: those are the *current* block's
        rates, and a forecast spans seasons and day types with different ones.
        """
        suffix = _WAVE_RATE_SUFFIX.get(int(wave) if wave is not None else 0, "Valley")
        return (
            self._to_cents(daytype.get(f"eleticRate{suffix}")),
            self._to_cents(daytype.get(f"eleticSell{suffix}")),
        )

    async def _load_strategy_list(self) -> list:
        """Newest persisted schedule for this gateway."""
        try:
            from src.services import db
            return await db.get_latest_tou_strategy(self._gateway_id)
        except Exception:
            logger.debug("franklinwh_tou: no persisted TOU schedule available", exc_info=True)
            return []

    # How far ahead the schedule is projected. One day was enough to answer
    # "what happens next", but not "what does the weather do to me this week" —
    # and the solar forecast runs eight days with a 5.8x swing between the best
    # and worst of them on a real site. A schedule that looks comfortable
    # against 40 kWh of sun does not survive a 7 kWh day, and there was no way
    # to see that coming.
    # Produce the whole window and let callers slice. The snapshot is built
    # once on a poll, so a per-request horizon cannot reach back into it —
    # generating the lot is cheaper than plumbing a parameter through the
    # pricing service for every provider that does not need one.
    DEFAULT_HORIZON_DAYS = 8
    MAX_HORIZON_DAYS = 8

    def _build_schedule_forecast(
        self, strategy_list: list, horizon_days: int | None = None
    ) -> list[PricePeriod]:
        """Project the stored TOU schedule forward.

        Blocks are HH:MM local-day boundaries, so each day is expanded
        separately and the season and day type are resolved for *that* day —
        which matters across a week, where the projection can cross a weekend
        or a season boundary.
        """
        if not strategy_list:
            return []

        days = max(1, min(int(horizon_days or self.DEFAULT_HORIZON_DAYS), self.MAX_HORIZON_DAYS))
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days, hours=2)
        periods: list[PricePeriod] = []

        for day_offset in range(days):
            day = (now + timedelta(days=day_offset)).date()
            season = self._season_for(strategy_list, day)
            if not season:
                continue
            daytype = self._daytype_for(season, day)
            if not daytype:
                continue

            for block in (daytype.get("detailVoList") or []):
                start_s = block.get("startHourTime") or block.get("startTime") or ""
                end_s = block.get("endHourTime") or block.get("endTime") or ""
                start_dt = self._parse_hhmm(start_s, day)
                end_dt = self._parse_hhmm(end_s, day)
                if start_dt is None or end_dt is None:
                    continue
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)          # block wraps midnight
                if end_dt <= now or start_dt > cutoff:
                    continue

                wave = block.get("waveType")
                tariff = _WAVE_TO_TARIFF.get(int(wave) if wave is not None else -1, "SHOULDER")
                import_c, export_c = self._rates_for_wave(daytype, wave)
                code = block.get("dispatchId") or block.get("dispatchCode")

                periods.append(PricePeriod(
                    start=start_dt,
                    end=end_dt,
                    import_c_kwh=import_c,
                    export_c_kwh=export_c,
                    tariff_type=tariff,
                    renewables_pct=None,
                    spike_status=self.normalize_spike_status(import_c),
                    descriptor=self.normalize_descriptor(import_c),
                    tariff_period=_DISPATCH_TO_PERIOD.get(
                        int(code) if code is not None else -1,
                        self.normalize_tariff_period(tariff),
                    ),
                ))

        periods.sort(key=lambda p: p.start)
        return periods

    @staticmethod
    def _parse_hhmm(value: str, day) -> Optional[datetime]:
        """HH:MM on a given day, as an instant. '24:00' is end of that day.

        Schedule blocks are **local** wall-clock times — a 17:00 peak means
        17:00 where the battery is. Building them as UTC shifts every period by
        the site's offset, which put a Sydney evening block at 03:00 and had
        the Smart Dispatch panel announce a price spike in the small hours.

        So: construct in local time, then convert. datetime.astimezone() on a
        naive value interprets it as local, which is exactly what is wanted
        here and exactly the opposite of what gateway_metrics needs — the
        metrics are stored naive UTC. Two conventions, one codebase; hence the
        note in both places.
        """
        try:
            hh, mm = map(int, str(value).split(":"))
        except (ValueError, AttributeError):
            return None
        local_midnight = datetime(day.year, day.month, day.day, 0, 0, 0).astimezone()
        return (local_midnight + timedelta(hours=hh, minutes=mm)).astimezone(timezone.utc)

    async def test_connection(self) -> dict:
        """Test FranklinWH TOU adapter — verifies gateway is reachable."""
        try:
            svc = self._resolve_gateway()
            client = await svc._get_or_create_client()

            snap = await self.get_snapshot()
            
            cached = getattr(svc, "_last_stats", None) or {}
            work_mode  = (cached.get("mode") or {}).get("work_mode")

            mode_label = {1: "TOU", 2: "Self-Consumption", 3: "Emergency Backup"}.get(
                work_mode, f"Mode {work_mode}" if work_mode is not None else "Unknown"
            )

            return {
                "ok": True,
                "message": (
                    f"Connected — Gateway {svc.short_id} · "
                    f"Mode: {mode_label} · "
                    f"Tariff: {snap.tariff_type} · "
                    f"import={snap.import_c_kwh:.2f}¢/kWh  "
                    f"export={snap.export_c_kwh:.2f}¢/kWh  "
                    f"forecast_periods={len(snap.forecast)}"
                ),
                "import_c_kwh":     snap.import_c_kwh,
                "export_c_kwh":     snap.export_c_kwh,
                "tariff_type":      snap.tariff_type,
                "spike_status":     snap.spike_status,
                "forecast_periods": len(snap.forecast),
                "gateway_id":       svc.short_id,
                "work_mode":        mode_label,
            }
        except Exception as exc:
            exc_str = str(exc) or type(exc).__name__
            msg = f"FranklinWH TOU: {exc_str}"
            logger.warning(msg)
            return {"ok": False, "message": msg}
