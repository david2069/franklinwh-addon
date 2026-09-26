"""Persona detection adapter — reads cloud client outputs, writes persona.* keys.

Implements GH issue #11 (BD-04). The cloud client library
(`franklinwh-cloud` v0.4.9+) already curates all four persona axes:

- `client.discover(tier=2)` → gateway type, solar presence, mppt_enabled,
  accessories.has_ahub/has_mac1, country_code (from device_info)
- `client.get_stats().current.grid_connection_state` → 4-state enum
- `client.get_tou_info(1)` → live TOU wave (for TOU tariff variety check)

This module is a **thin adapter** — no cascade logic. Cloud client already
ran the cascade for us. See `docs/persona_matrix_plan.md` §8 for detail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.services import db, persona

logger = logging.getLogger(__name__)


# ── Grid state mapping ──────────────────────────────────────────────────

# GridConnectionState enum from franklinwh_cloud.models — .value is a
# string. We map to the persona flag:
#   CONNECTED           → connected
#   NOT_GRID_TIED       → off_grid  (installed off-grid, steady state)
#   SIMULATED_OFF_GRID  → off_grid  (installer-forced, treat as off-grid persona)
#   OUTAGE              → DO NOT FLIP (transient — persona reflects steady state)
_GRID_STATE_TO_PERSONA = {
    "CONNECTED":          persona.GRID_CONNECTED,
    "NOT_GRID_TIED":      persona.GRID_OFF_GRID,
    "SIMULATED_OFF_GRID": persona.GRID_OFF_GRID,
    # OUTAGE deliberately absent → returns None → skip write
}


def _map_grid_state(state_value: str | None) -> str | None:
    """Map cloud client GridConnectionState.value to persona.grid_status.
    Returns None for transient states (OUTAGE) — caller should skip write."""
    if not state_value:
        return None
    return _GRID_STATE_TO_PERSONA.get(str(state_value).upper())


# ── Tariff three-signal decision ────────────────────────────────────────

async def _decide_tariff_type(
    tariff_setting_flag: bool,
    tou_wave_types: list[int],
    has_dynamic_pricing_provider: bool,
    is_off_grid: bool,
) -> str:
    """Three-signal tariff decision (plan §4.4).

    - dynamic provider configured → dynamic
    - tariffSettingFlag + multi-wave TOU schedule → tou
    - tariffSettingFlag + single-wave schedule → flat (TOU wrapper over flat)
    - off-grid → none
    - else → flat
    """
    if has_dynamic_pricing_provider:
        return persona.TARIFF_DYNAMIC
    if is_off_grid:
        return persona.TARIFF_NONE
    if tariff_setting_flag:
        distinct_waves = len(set(tou_wave_types)) if tou_wave_types else 0
        if distinct_waves >= 2:
            return persona.TARIFF_TOU
        return persona.TARIFF_FLAT
    return persona.TARIFF_FLAT


async def _has_active_dynamic_pricing_provider() -> bool:
    """Check FHAI's pricing_config for an active dynamic provider (Amber, ConEd, etc.)."""
    try:
        cfg = await db.get_pricing_config()
    except Exception:
        return False
    if not cfg:
        return False
    # `pricing_config.provider` is a string like 'amber', 'coned', 'localvolts', ...
    # `pricing_config.enabled` gates whether the provider is active.
    provider = str(cfg.get("provider") or "").strip().lower()
    enabled = bool(cfg.get("enabled"))
    return bool(provider) and provider != "none" and enabled


# ── Enphase presence (FHAI-side, not cloud client) ──────────────────────

async def _detect_enphase(gateway_serial: str) -> bool:
    """Enphase presence via FHAI's enphase_configured flag on SD config.
    LAN mDNS probe is a Phase B enhancement (would auto-populate Envoy IP)."""
    try:
        sd_cfg = await db.get_smart_dispatch_config(gateway_serial)
    except Exception:
        return False
    return bool(sd_cfg.get("enphase_configured"))


# ── Detection log ───────────────────────────────────────────────────────

async def _log_axis(
    axis: str,
    value: Any,
    source: str,
    confidence: str,
    previous_value: Any = None,
    gateway_serial: str | None = None,
) -> None:
    """Append one row to persona_detection_log for auditability."""
    import aiosqlite
    async with aiosqlite.connect(db.get_db_path(), timeout=30.0) as conn:
        await conn.execute(
            """INSERT INTO persona_detection_log
               (detected_at, gateway_serial, axis, value, source, confidence, previous_value)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                gateway_serial,
                axis,
                str(value) if value is not None else None,
                source,
                confidence,
                str(previous_value) if previous_value is not None else None,
            ),
        )
        await conn.commit()


async def _set_and_log(
    key: str,
    new_value: Any,
    source: str,
    confidence: str = "high",
    gateway_serial: str | None = None,
    axis_name: str | None = None,
) -> None:
    """Write to app_config + log to persona_detection_log iff value changed."""
    previous = await db.get_config_value(key, None)
    # Normalise for comparison — bool vs "1"/"true" collision
    if previous != new_value and str(previous).lower() != str(new_value).lower():
        await db.set_config_value(key, new_value)
        await _log_axis(
            axis=axis_name or key.replace("persona.", ""),
            value=new_value,
            source=source,
            confidence=confidence,
            previous_value=previous,
            gateway_serial=gateway_serial,
        )
    else:
        # Still persist (idempotent) but don't log — reduces log noise
        await db.set_config_value(key, new_value)


# ── Detection driver ────────────────────────────────────────────────────

async def detect_for_gateway(gateway_serial: str, client: Any) -> dict[str, Any]:
    """Detect all per-gateway persona axes using an already-constructed
    cloud client. Returns dict of axis→value for the gateway (for logging).

    `client` is expected to be a `franklinwh_cloud.client.Client` instance
    (or something with the same `discover`, `get_stats`, `get_tou_info`
    methods for testing).
    """
    axes: dict[str, Any] = {}

    # 1. Discover — single call for structure (gateway type, solar, accessories, country)
    try:
        snap = await client.discover(tier=2)
    except Exception as e:
        logger.warning(f"[persona/{gateway_serial}] discover() failed: {e}")
        snap = None

    if snap is not None:
        # Gateway type — snap.agate.model_name preferred, snap.agate.model fallback
        agate = getattr(snap, "agate", None)
        if agate is not None:
            model = getattr(agate, "model_name", None) or getattr(agate, "model", None) or "unknown"
            await _set_and_log(
                f"persona.gateway_type.{gateway_serial}", str(model),
                source="cloud_client_discover", gateway_serial=gateway_serial,
                axis_name="gateway_type",
            )
            axes["gateway_type"] = model

        # Solar + MPPT (aPower S detection)
        flags = getattr(snap, "flags", None)
        if flags is not None:
            solar = bool(getattr(flags, "solar", False))
            mppt = bool(getattr(flags, "mppt_enabled", False))
            await _set_and_log(f"persona.solar_present.{gateway_serial}", solar,
                               source="cloud_client_discover", gateway_serial=gateway_serial,
                               axis_name="solar_present")
            await _set_and_log(f"persona.mppt_enabled.{gateway_serial}", mppt,
                               source="cloud_client_discover", gateway_serial=gateway_serial,
                               axis_name="mppt_enabled")
            axes["solar_present"] = solar
            axes["mppt_enabled"] = mppt

        # Accessories (aHub, MAC-1)
        acc = getattr(snap, "accessories", None)
        if acc is not None:
            has_ahub = bool(getattr(acc, "has_ahub", False))
            has_mac1 = bool(getattr(acc, "has_mac1", False))
            await _set_and_log(f"persona.has_ahub.{gateway_serial}", has_ahub,
                               source="cloud_client_discover", gateway_serial=gateway_serial,
                               axis_name="has_ahub")
            await _set_and_log(f"persona.has_mac1.{gateway_serial}", has_mac1,
                               source="cloud_client_discover", gateway_serial=gateway_serial,
                               axis_name="has_mac1")
            axes["has_ahub"] = has_ahub
            axes["has_mac1"] = has_mac1

        # Country (global — last one wins, most installs are single-country)
        country = _extract_country_code(snap)
        # The device snapshot does not always carry country — on a live aGate
        # X-01-AU none of snap.agate.country_code, snap.country_code or
        # device_info.countryId were populated, so the axis stayed empty while
        # the cloud knew the answer perfectly well. The location endpoint has
        # both country and the IANA timezone, so fetch it once and take the two
        # fields, discarding latitude, longitude, address and postcode.
        site_timezone = None
        province = city = None
        try:
            loc = await client.get_equipment_location()
            country = country or _country_from_location(loc)
            site_timezone = _timezone_from_location(loc)
            province, city = _region_from_location(loc)
        except Exception as exc:
            logger.debug(f"persona: equipment location unavailable: {exc!r}")

        for axis, value in (("province", province), ("city", city)):
            if value:
                await _set_and_log(f"persona.{axis}", value,
                                   source="cloud_equipment_location",
                                   axis_name=axis)
                axes[axis] = value

        if site_timezone:
            # Scheduling already honours a site timezone where one is known, but
            # it was only ever captured at onboarding — an install that enrolled
            # before that, or whose zoneInfo was blank then, silently ran every
            # "site-local" job in UTC.
            await _set_and_log("persona.timezone", site_timezone,
                               source="cloud_equipment_location",
                               axis_name="timezone")
            axes["timezone"] = site_timezone

        if country:
            await _set_and_log("persona.country_code", country,
                               source="cloud_client_discover",
                               axis_name="country_code")
            axes["country_code"] = country

    # 2. Grid state — get_stats().current.grid_connection_state
    try:
        stats = await client.get_stats()
        gcs = getattr(getattr(stats, "current", None), "grid_connection_state", None)
        state_value = getattr(gcs, "value", None) if gcs is not None else None
        mapped = _map_grid_state(state_value)
        if mapped is not None:
            await _set_and_log(f"persona.grid_status.{gateway_serial}", mapped,
                               source="cloud_client_stats", gateway_serial=gateway_serial,
                               axis_name="grid_status")
            axes["grid_status"] = mapped
        else:
            # OUTAGE or unknown — don't flip persona; log at debug
            logger.debug(f"[persona/{gateway_serial}] grid_state={state_value} — transient, persona unchanged")
    except Exception as e:
        logger.warning(f"[persona/{gateway_serial}] get_stats() failed: {e}")

    # 3. Enphase (FHAI-side)
    enphase = await _detect_enphase(gateway_serial)
    await _set_and_log(f"persona.enphase_present.{gateway_serial}", enphase,
                       source="fhai_sd_config", gateway_serial=gateway_serial,
                       axis_name="enphase_present")
    axes["enphase_present"] = enphase

    return axes


def _country_from_location(loc: Any) -> str | None:
    """ISO alpha-2 from a get_equipment_location response, and nothing else.

    That endpoint is dense with PII — exact latitude and longitude, street
    address, postcode, city. Only the country is taken, and only the country is
    ever stored or logged. Do not widen this to return the response: the point
    is that the rest never leaves the call.

    `alphaCode` is preferred because it already is ISO ("AU,CX,CC,HM,NF" — take
    the first). The country name is a fallback for responses that omit it.
    """
    if not isinstance(loc, dict):
        return None

    alpha = loc.get("alphaCode")
    if isinstance(alpha, str) and alpha.strip():
        first = alpha.split(",")[0].strip().upper()
        if len(first) == 2 and first.isalpha():
            return first

    name = loc.get("country")
    if isinstance(name, str) and name.strip():
        code = _COUNTRY_NAME_TO_CODE.get(name.strip().lower())
        if code:
            return code

    return None


def _region_from_location(loc: Any) -> tuple[str | None, str | None]:
    """(province/state, city) from a get_equipment_location response.

    Collected so deployment distribution can be shown — see GH #40. Note the
    asymmetry: a province is one of a handful per country, while a city can be
    a single household once combined with a hardware profile. That distinction
    does not matter while telemetry is local-only, and matters enormously at
    publication. The aggregation rule belongs there, not here.

    As with the other helpers: these two fields, never the response. Latitude,
    longitude, postcode and street address stay behind.
    """
    if not isinstance(loc, dict):
        return None, None
    province = loc.get("province")
    city = loc.get("city")
    return (
        province.strip() if isinstance(province, str) and province.strip() else None,
        city.strip() if isinstance(city, str) and city.strip() else None,
    )


def _timezone_from_location(loc: Any) -> str | None:
    """IANA zone from a get_equipment_location response, e.g. Australia/Sydney.

    Same rule as the country helper: take this field and nothing else. The
    response also carries latitude, longitude, street address and postcode,
    none of which are wanted here or anywhere else.
    """
    if not isinstance(loc, dict):
        return None
    zone = loc.get("zoneInfo")
    if isinstance(zone, str) and "/" in zone:
        return zone.strip()
    return None


# Deliberately small: the markets this actually ships into. An unknown name
# yields None rather than a guess — a wrong country is worse than none.
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "australia": "AU", "new zealand": "NZ", "united states": "US",
    "united states of america": "US", "canada": "CA",
    "united kingdom": "GB", "ireland": "IE", "south africa": "ZA",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "japan": "JP", "philippines": "PH",
}


def _extract_country_code(snap: Any) -> str | None:
    """Extract country code from DeviceSnapshot. Cloud client exposes it
    variously; try the common paths."""
    # Try snap.agate.country_code
    agate = getattr(snap, "agate", None)
    if agate is not None:
        cc = getattr(agate, "country_code", None) or getattr(agate, "country", None)
        if cc:
            return str(cc).upper()
    # Try snap.country_code
    cc = getattr(snap, "country_code", None) or getattr(snap, "country", None)
    if cc:
        return str(cc).upper()
    # Try snap.raw device_info.countryId (int → map to code)
    raw = getattr(snap, "raw", None) or {}
    if isinstance(raw, dict):
        di = raw.get("device_info", {})
        country_id = di.get("countryId") if isinstance(di, dict) else None
        if country_id is not None:
            return _country_id_to_code(int(country_id))
    return None


def _country_id_to_code(cid: int) -> str | None:
    """FranklinWH country_id → ISO code. From device_catalog.json."""
    return {1: "CN", 2: "US", 3: "AU", 4: "CA"}.get(cid)


# ── Tariff decision (global) ────────────────────────────────────────────

async def detect_tariff_type(
    client: Any | None = None,
    gateway_serial: str | None = None,
) -> str:
    """Global tariff detection. Uses the first available gateway's TOU info
    if `client` and `gateway_serial` are provided (for the wave-variety
    check); otherwise falls back to the two other signals."""

    tariff_setting_flag = False
    tou_waves: list[int] = []
    is_off_grid = False

    if client is not None and gateway_serial is not None:
        try:
            tou = await client.get_tou_info(1)
            # tou_info returns a structure with blocks — extract waveType values
            tou_waves = _extract_tou_wave_types(tou)
        except Exception as e:
            logger.debug(f"[persona] get_tou_info failed: {e}")

        # tariffSettingFlag lives in entrance/discover raw data
        try:
            snap = await client.discover(tier=1)
            raw = getattr(snap, "raw", None) or {}
            if isinstance(raw, dict):
                entrance = raw.get("entrance", {}) or raw.get("entrance_info", {})
                if isinstance(entrance, dict):
                    tariff_setting_flag = bool(entrance.get("tariffSettingFlag", False))
        except Exception:
            pass

        # Off-grid check for this gateway
        try:
            is_off_grid = not await persona.is_grid_connected(gateway_serial)
        except Exception:
            pass

    has_dynamic = await _has_active_dynamic_pricing_provider()

    tariff = await _decide_tariff_type(
        tariff_setting_flag=tariff_setting_flag,
        tou_wave_types=tou_waves,
        has_dynamic_pricing_provider=has_dynamic,
        is_off_grid=is_off_grid,
    )

    # Source labels for the log
    if has_dynamic:
        source = "fhai_pricing_config"
    elif is_off_grid:
        source = "inferred_from_grid_status"
    elif tou_waves:
        source = "cloud_client_tou_info"
    else:
        source = "default"

    await _set_and_log("persona.tariff_type", tariff, source=source,
                       confidence="high" if source != "default" else "low",
                       axis_name="tariff_type")
    return tariff


def _extract_tou_wave_types(tou_info: Any) -> list[int]:
    """Extract the waveType integers from a TOU info payload. The exact
    shape varies by cloud client version — try common paths."""
    waves: list[int] = []
    # Iterable of blocks
    for path in ("blocks", "schedule", "data", "current_and_next"):
        blocks = getattr(tou_info, path, None)
        if blocks is None and isinstance(tou_info, dict):
            blocks = tou_info.get(path)
        if isinstance(blocks, list):
            for b in blocks:
                wt = getattr(b, "waveType", None) if not isinstance(b, dict) else b.get("waveType")
                if wt is not None:
                    try:
                        waves.append(int(wt))
                    except (TypeError, ValueError):
                        pass
            if waves:
                return waves
    # Direct attribute on the object
    wt = getattr(tou_info, "waveType", None)
    if wt is not None:
        try:
            waves.append(int(wt))
        except (TypeError, ValueError):
            pass
    return waves


# ── Top-level orchestrator ──────────────────────────────────────────────

async def detect_and_persist(
    gateways: list[dict] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run detection for every configured gateway + global tariff.

    Args:
        gateways: list of gateway dicts (must include 'serial' and provide a
                  way to obtain a cloud client). If None, uses gateway_service.
        force:    if False and `persona.detected_at` was set within the last
                  hour, skip re-detection (rate-limit against wizard mash).

    Returns:
        dict with `per_gateway` axes and `global` axes, plus `detected_at`.
    """
    if not force:
        last = await db.get_config_value("persona.detected_at", None)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if age_s < 3600:
                    logger.info(f"persona.detected_at is {int(age_s)}s old — skipping (force=False)")
                    return {"skipped": True, "detected_at": last}
            except Exception:
                pass

    from src.services import gateway_registry

    if gateways is None:
        # Enumerate from gateway_service via gateway_registry — the running
        # services expose live cloud clients. Fall back to DB list of serials.
        try:
            gws = await _enumerate_gateways_with_clients()
        except Exception as e:
            logger.warning(f"persona.detect_and_persist: gateway enumeration failed: {e}")
            gws = []
    else:
        gws = gateways

    per_gateway: dict[str, dict[str, Any]] = {}
    first_client = None
    first_serial = None
    for gw in gws:
        serial = gw.get("serial")
        client = gw.get("client")
        if not serial or client is None:
            continue
        try:
            per_gateway[serial] = await detect_for_gateway(serial, client)
            if first_client is None:
                first_client = client
                first_serial = serial
        except Exception as e:
            logger.warning(f"[persona/{serial}] detection failed: {e}")

    # Global tariff (uses first available gateway for TOU info)
    tariff = await detect_tariff_type(client=first_client, gateway_serial=first_serial)

    ts = datetime.now(timezone.utc).isoformat()
    await db.set_config_value("persona.detected_at", ts)
    await db.set_config_value("persona.detection_version", 1)

    return {
        "per_gateway": per_gateway,
        "global": {"tariff_type": tariff},
        "detected_at": ts,
    }


async def run_weekly_redetect() -> None:
    """Module-level entry point for the APScheduler weekly job so the
    scheduler can serialise it as `src.services.persona_detector:run_weekly_redetect`.
    (Local closures inside AutomationEngine.register_sd_jobs() fail
    SQLAlchemyJobStore serialisation with "reference to callable could
    not be determined".)"""
    try:
        await detect_and_persist(force=True)
    except Exception as e:
        logger.warning(f"Weekly persona re-detect failed (non-fatal): {e}")


async def _enumerate_gateways_with_clients() -> list[dict]:
    """Look up running gateway services + return [{serial, client}] pairs.

    Consumes `app_state["registry"]` (populated at lifespan startup by
    main.py). GatewayRegistry holds services in `_services` dict keyed by
    short_id; each GatewayService exposes its cloud client via
    `_get_or_create_client()` (returns cached _client if already opened).
    Falls back to empty list if registry isn't populated yet.
    """
    from src.app_state import get_app_state

    out: list[dict] = []
    state = get_app_state()
    registry = state.get("registry") if isinstance(state, dict) else None
    if registry is None:
        return out
    services = getattr(registry, "_services", None) or {}
    for short_id, svc in services.items():
        try:
            client = await svc._get_or_create_client()
        except Exception as e:
            logger.warning(f"[persona/{short_id}] client init failed: {e}")
            continue
        if client is not None:
            out.append({"serial": svc.full_serial or short_id, "client": client})
    return out
