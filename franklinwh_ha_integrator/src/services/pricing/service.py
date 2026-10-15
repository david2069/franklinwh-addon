"""
PricingService — background poll loop, DB persistence, provider dispatch.

Boots alongside GatewayRegistry in main.py lifespan.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.services import db
from src.services.pricing.base import PriceSnapshot, PricingAdapter

logger = logging.getLogger(__name__)

_FALLBACK_INTERVAL = 300   # seconds between polls if config missing


class _EmulationAdapter(PricingAdapter):
    """Returns a canned PriceSnapshot. Used when `<provider>_emulation_mode`
    is set in pricing_settings — lets the user wire the active provider for
    a gateway without supplying real credentials.

    The same snapshot constants live in api_pricing.py (used by the Test
    Connection short-circuit). They're imported here at instantiation time
    to keep one source of truth."""

    def __init__(self, provider: str, snapshot: dict):
        self._provider = provider
        self._snap = snapshot

    async def get_snapshot(self) -> PriceSnapshot:
        from src.services.pricing.base import PricePeriod

        # Lift the canned forecast (24h × 5-min/30-min/60-min interval, depending
        # on provider) from the snapshot dict. Allows AEMO/ComEd/LocalVolts
        # emulation to render a realistic 24h forecast strip in the UI.
        forecast_periods: list[PricePeriod] = []
        for raw in self._snap.get("forecast", []) or []:
            try:
                forecast_periods.append(PricePeriod(
                    start          = datetime.fromisoformat(raw["start_time"]),
                    end            = datetime.fromisoformat(raw["end_time"]),
                    import_c_kwh   = float(raw.get("import_c_kwh", 0.0)),
                    export_c_kwh   = (float(raw["export_c_kwh"]) if raw.get("export_c_kwh") is not None else None),
                    tariff_type    = raw.get("tariff_type", "SHOULDER"),
                    renewables_pct = raw.get("renewables_pct"),
                ))
            except (KeyError, ValueError) as exc:
                logger.debug(f"EmulationAdapter: skip malformed forecast row: {exc}")

        return PriceSnapshot(
            provider       = self._provider,
            import_c_kwh   = float(self._snap.get("import_c_kwh", 0.0)),
            export_c_kwh   = (float(self._snap["export_c_kwh"]) if self._snap.get("export_c_kwh") is not None else None),
            tariff_type    = self._snap.get("tariff_type", "SHOULDER"),
            spike_status   = self._snap.get("spike_status", "NONE"),
            demand_window  = bool(self._snap.get("demand_window", False)),
            renewables_pct = self._snap.get("renewables_pct"),
            interval_min   = int(self._snap.get("interval_min", 5)),
            valid_until    = None,
            fetched_at     = datetime.now(timezone.utc),
            forecast       = forecast_periods,
        )


def _maybe_emulation_adapter(provider: str, settings: dict) -> Optional[PricingAdapter]:
    """Return an EmulationAdapter if the matching `<provider>_emulation_mode`
    flag is set in settings, otherwise None. Pulls the canned snapshot
    constants from api_pricing so test + service paths share data."""
    flag_key = f"{provider}_emulation_mode"
    if not settings.get(flag_key):
        return None
    # Lazy import to avoid circular dep at module load
    from src.routes import api_pricing as _ap
    snap_map = {
        "localvolts": getattr(_ap, "_LV_EMULATION_SNAPSHOT", None),
        "aemo":       getattr(_ap, "_AEMO_EMULATION_SNAPSHOT", None),
        "comed":      getattr(_ap, "_COMED_EMULATION_SNAPSHOT", None),
        # Amber emulation in api_pricing only mocks the Test Connection — its
        # full emulation flow goes through a different data path (cached usage),
        # so we don't route the service through this shim for amber.
    }
    snap = snap_map.get(provider)
    if not snap:
        return None
    logger.info(f"_build_adapter: provider={provider} → EmulationAdapter (mock snapshot)")
    return _EmulationAdapter(provider, snap)


def _build_adapter(cfg: dict) -> PricingAdapter:
    """Construct the right adapter from stored config."""
    from src.services.pricing.flat_rate import FlatRateAdapter

    provider = cfg.get("provider", "flat")
    creds    = cfg.get("credentials", {})
    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except Exception:
            creds = {}

    settings = cfg.get("settings", {})
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    # Emulation short-circuit — if `<provider>_emulation_mode` is set, return
    # a mock adapter that serves canned snapshots. Keeps the service path
    # consistent with the Test Connection short-circuit in api_pricing.
    emu = _maybe_emulation_adapter(provider, settings)
    if emu is not None:
        return emu

    if provider == "amber":
        from src.services.pricing.amber import AmberAdapter
        token   = creds.get("api_token", "")
        site_id = creds.get("site_id")
        if not token:
            raise ValueError("Amber provider requires api_token in credentials")
        return AmberAdapter(api_token=token, site_id=site_id)

    elif provider == "localvolts":
        from src.services.pricing.localvolts import LocalVoltsAdapter
        api_key    = creds.get("api_key", "")
        partner_id = creds.get("partner_id", "")
        nmi_id     = creds.get("nmi_id", "")
        if not (api_key and partner_id and nmi_id):
            raise ValueError("LocalVolts requires api_key, partner_id, nmi_id")
        return LocalVoltsAdapter(api_key, partner_id, nmi_id)

    elif provider == "aemo":
        from src.services.pricing.aemo import AEMOAdapter
        region = settings.get("region", "nsw")
        return AEMOAdapter(region=region)

    elif provider == "comed":
        from src.services.pricing.comed import ComedAdapter
        return ComedAdapter()

    elif provider == "franklinwh_tou":
        from src.services.pricing.franklinwh_tou import FranklinWHTOUAdapter
        return FranklinWHTOUAdapter(
            gateway_id    = settings.get("gateway_id") or None,
        )

    else:  # flat / fallback
        import_c = float(settings.get("import_c_kwh", 25.0))
        export_c = float(settings.get("export_c_kwh", -5.0))
        tariff   = settings.get("tariff_type", "SHOULDER")
        return FlatRateAdapter(import_c, export_c, tariff)


class PricingService:
    """
    Background service that polls the configured pricing provider for a specific utility service,
    stores snapshots in SQLite, and keeps an in-memory current snapshot.
    """

    def __init__(self, utility_service_id: str):
        self.utility_service_id = utility_service_id
        self._snapshot: Optional[PriceSnapshot] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._api_calls: list[float] = []
        self._adapter: Optional[PricingAdapter] = None
        self._adapter_cfg_hash = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Optional[PriceSnapshot]:
        return self._snapshot

    async def force_refresh(self) -> Optional[PriceSnapshot]:
        """Immediately poll the provider and update the cached snapshot."""
        import time
        raw_cfg = await db.get_utility_service(self.utility_service_id)
        if not raw_cfg:
            logger.info(f"PricingService[{self.utility_service_id}]: force_refresh skipped — config missing")
            return None
            
        cfg = {
            "provider": raw_cfg.get("pricing_provider", "flat"),
            "credentials": raw_cfg.get("pricing_credentials", "{}"),
            "settings": raw_cfg.get("pricing_settings", "{}")
        }
        self._api_calls.append(time.time())
        return await self._do_poll(cfg)

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            logger.info(f"PricingService[{self.utility_service_id}]: started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"PricingService[{self.utility_service_id}]: stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        """Main poll loop — checks DB for config on each iteration."""
        import time
        logger.info(f"PricingService[{self.utility_service_id}]: poll loop started")
        while self._running:
            try:
                raw_cfg = await db.get_utility_service(self.utility_service_id)
                if raw_cfg:
                    cfg = {
                        "provider": raw_cfg.get("pricing_provider", "flat"),
                        "credentials": raw_cfg.get("pricing_credentials", "{}"),
                        "settings": raw_cfg.get("pricing_settings", "{}")
                    }
                    settings = cfg.get("settings", {})
                    if isinstance(settings, str):
                        try:
                            settings = json.loads(settings)
                        except Exception:
                            settings = {}

                    provider = cfg.get("provider", "flat")
                    default_interval = 300  # 5 min default for all providers
                    # Enforce strict 5-minute minimum (300s) to prevent gateway instability
                    interval_secs = max(300, int(settings.get("poll_interval_secs", default_interval)))
                    rate_limit = int(settings.get("rate_limit_per_hour", 0))

                    # Rate limiter logic
                    if rate_limit > 0:
                        now = time.time()
                        self._api_calls = [t for t in self._api_calls if now - t < 3600]
                        if len(self._api_calls) >= rate_limit:
                            logger.warning(f"PricingService[{self.utility_service_id}]: Rate limit ({rate_limit}/hr) hit for {provider}. Throttling fetch.")
                            await asyncio.sleep(min(300, interval_secs))
                            continue

                    self._api_calls.append(time.time())
                    await self._do_poll(cfg)
                else:
                    interval_secs = _FALLBACK_INTERVAL
            except Exception as exc:
                logger.exception(f"PricingService[{self.utility_service_id}]: poll error — {exc}")
                interval_secs = _FALLBACK_INTERVAL

            await asyncio.sleep(interval_secs)

    async def _do_poll(self, cfg: dict) -> Optional[PriceSnapshot]:
        import hashlib
        import json
        cfg_str = json.dumps(cfg, sort_keys=True)
        h = hashlib.md5(cfg_str.encode()).hexdigest()
        
        if self._adapter is None or self._adapter_cfg_hash != h:
            try:
                self._adapter = _build_adapter(cfg)
                self._adapter_cfg_hash = h
            except ValueError as exc:
                logger.warning(f"PricingService[{self.utility_service_id}]: adapter config error — {exc}")
                return None

        # Read verbose_logging from settings (stored in JSON blob — no schema migration needed)
        settings = cfg.get("settings", {})
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}
        verbose_logging = bool(settings.get("verbose_logging", False))

        try:
            snap = await self._adapter.get_snapshot()

            # Detect meaningful change: only promote to INFO when tariff period
            # or import price shifts by more than 0.5¢ (avoids noise from rounding drift)
            prev = self._snapshot
            price_changed = (
                prev is None
                or prev.tariff_type != snap.tariff_type
                or abs(prev.import_c_kwh - snap.import_c_kwh) > 0.5
            )

            self._snapshot = snap
            await self._persist(snap)

            # SmartDispatch autonomous hook — evaluate rules on price tick.
            # Rate-limited to SD_EVAL_MIN_INTERVAL_SECS (default 300s / 5 min) per gateway.
            # SD disabled check is also performed inside evaluate_and_log() as an early guard.
            # Non-fatal: dispatch failure never prevents the price snapshot from being stored.
            SD_EVAL_MIN_INTERVAL_SECS = 300  # 5 min — matches provider poll cadence
            try:
                from src.services.smart_dispatch import smart_dispatch_engine
                from src.services.db import get_gateways_for_utility_service
                import time as _time
                if smart_dispatch_engine is not None:
                    gws = await get_gateways_for_utility_service(self.utility_service_id)
                    for gw_id in gws:
                        # Phase 2.D (2026-08-05) — if this gateway has opted
                        # into the MicroTicker path (schema v53), the 30s
                        # `sd:micro:tick` job drives SD for it. Skip here
                        # so exactly one path fires per gateway.
                        try:
                            from src.services import db as _db
                            _gw_cfg = await _db.get_smart_dispatch_config(gw_id) or {}
                            if bool(_gw_cfg.get("sd_use_micro_ticker", 0)):
                                logger.debug(f"PricingService[{self.utility_service_id}]: SD eval skipped for {gw_id} (MicroTicker owns)")
                                continue
                        except Exception:
                            pass
                        # Rate-limit SD eval per gateway
                        _last_eval = getattr(self, "_sd_last_eval", {})
                        _now_t = _time.time()
                        if _now_t - _last_eval.get(gw_id, 0) < SD_EVAL_MIN_INTERVAL_SECS:
                            logger.debug(f"PricingService[{self.utility_service_id}]: SD eval skipped for {gw_id} (rate-limited to {SD_EVAL_MIN_INTERVAL_SECS}s)")
                            continue
                        if not hasattr(self, "_sd_last_eval"):
                            self._sd_last_eval = {}
                        self._sd_last_eval[gw_id] = _now_t
                        # Fetch SoC from registry if available
                        soc_pct = None
                        try:
                            reg = smart_dispatch_engine._gateway_registry
                            if reg:
                                gateway = reg.get_gateway(gw_id)
                                if gateway and gateway.status and gateway.status.last_data:
                                    soc_pct = gateway.status.last_data.get("battery_soc") if gateway.status.last_data.get("battery_soc") is not None else 0.0
                        except Exception:
                            pass
                        await smart_dispatch_engine.evaluate_and_log(
                            snap,
                            utility_service_id=self.utility_service_id,
                            soc_pct=soc_pct,
                            gateway_serial=gw_id
                        )
            except Exception as _sd_exc:
                logger.warning(f"PricingService[{self.utility_service_id}]: SmartDispatch evaluation error (non-fatal): {_sd_exc}")

            exp_str = f"{snap.export_c_kwh:.3f}¢" if snap.export_c_kwh is not None else "None"
            msg = (
                f"PricingService[{self.utility_service_id}]: {snap.provider} — import={snap.import_c_kwh:.3f}¢ "
                f"export={exp_str} tariff={snap.tariff_type}"
            )
            if verbose_logging or price_changed:
                logger.info(msg)
            else:
                logger.debug(msg)
            return snap
        except Exception as exc:
            logger.error(f"PricingService[{self.utility_service_id}]: fetch error from {cfg.get('provider')} — {exc}")
            return None

    async def _persist(self, snap: PriceSnapshot):
        import json
        await db.insert_price_snapshot(
            provider=snap.provider,
            import_c_kwh=snap.import_c_kwh,
            export_c_kwh=snap.export_c_kwh,
            demand_window=int(snap.demand_window),
            solar_bonus=None,
            tariff_type=snap.tariff_type,
            renewables_pct=snap.renewables_pct,
            spike_status=snap.spike_status,
            interval_min=snap.interval_min,
            valid_until=snap.valid_until.isoformat() if snap.valid_until else None,
            forecast_json=json.dumps([
                {
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "import_c_kwh": p.import_c_kwh,
                    "export_c_kwh": p.export_c_kwh,
                    "tariff_type": p.tariff_type,
                    "renewables_pct": p.renewables_pct,
                }
                for p in snap.forecast
            ]),
            utility_service_id=self.utility_service_id
        )


class PricingServiceRegistry:
    def __init__(self):
        self._services: dict[str, PricingService] = {}

    async def start_all(self):
        services = await db.get_all_utility_services()
        for svc in services:
            sid = svc["id"]
            if sid not in self._services:
                self._services[sid] = PricingService(sid)
            self._services[sid].start()

    async def stop_all(self):
        for svc in self._services.values():
            await svc.stop()

    def get_service(self, utility_service_id: str) -> Optional[PricingService]:
        return self._services.get(utility_service_id)

    def get_all_services(self) -> dict[str, PricingService]:
        return self._services

    def get_primary_service(self) -> Optional[PricingService]:
        """Fallback for endpoints that don't pass an ID."""
        if self._services:
            return next(iter(self._services.values()))
        return None

    async def restart_service(self, utility_service_id: str):
        if utility_service_id in self._services:
            await self._services[utility_service_id].stop()
        else:
            self._services[utility_service_id] = PricingService(utility_service_id)
        self._services[utility_service_id].start()

    async def stop_service(self, utility_service_id: str):
        if utility_service_id in self._services:
            await self._services[utility_service_id].stop()
            del self._services[utility_service_id]

    async def restart_services_for_model(self, model_id: str):
        """Restart all active pricing services linked to a specific pricing model ID."""
        for sid in list(self._services.keys()):
            raw_cfg = await db.get_utility_service(sid)
            if raw_cfg and raw_cfg.get("pricing_model_id") == model_id:
                logger.info(f"PricingServiceRegistry: Restarting service {sid} for pricing model {model_id}")
                await self.restart_service(sid)


pricing_registry = PricingServiceRegistry()

