"""Persona helpers — read-side API for the persona matrix.

Implements GH issue #11 (BD-04). Persona flags are stored in `app_config`
as `persona.*` keys. Detection is done by `persona_detector.py`; this
module is the read-side API everyone else consumes.

Design notes:
- Override precedence: `persona.override.{key}` beats detected value.
- Per-gateway keys: `persona.<axis>.{gateway_serial}`.
- Global keys: `persona.tariff_type`, `persona.country_code`, `persona.detected_at`.
- Callers MUST use these helpers rather than reading `app_config` directly
  so override precedence is enforced consistently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.services import db


# ── Enums as plain strings (JSON-friendly, no import overhead) ──────────

GRID_CONNECTED = "connected"
GRID_OFF_GRID = "off_grid"

TARIFF_DYNAMIC = "dynamic"
TARIFF_TOU = "tou"
TARIFF_FLAT = "flat"
TARIFF_NONE = "none"


@dataclass(frozen=True)
class SDEngineFlags:
    """SD engine flags derived from persona. Consumed by SD rule gating.

    Phase A defaults are permissive — every flag is True unless explicitly
    computed False from persona. This keeps Phase A behaviour-neutral;
    per-rule gating on these flags is Phase B (per-tab-gating issues).
    """
    pricing_available: bool = True
    solar_available: bool = True
    grid_charging_permitted: bool = True


@dataclass(frozen=True)
class Persona:
    """Snapshot of the persona flags for one gateway (or global-only fields)."""
    gateway_serial: str | None = None
    gateway_type: str = "unknown"
    grid_status: str = GRID_CONNECTED
    solar_present: bool = False
    mppt_enabled: bool = False
    enphase_present: bool = False
    has_ahub: bool = False
    has_mac1: bool = False
    tariff_type: str = TARIFF_FLAT   # global
    country_code: str = "US"          # global
    detected_at: str | None = None    # global ISO ts
    overrides_active: bool = False    # global


# ── Internal ────────────────────────────────────────────────────────────

async def _read_with_override(key: str, default: Any = None) -> Any:
    """Read app_config value, preferring `persona.override.{axis}` when set.

    `key` is the full app_config key (e.g. 'persona.grid_status.S1'). The
    override system operates on the axis path (`grid_status.S1`), so we
    strip the leading `persona.` before composing the override key.
    """
    axis = key.removeprefix("persona.")
    override_key = f"persona.override.{axis}"
    v = await db.get_config_value(override_key, None)
    if v is not None:
        return v
    return await db.get_config_value(key, default)


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


# ── Public read API ─────────────────────────────────────────────────────

async def get_persona(gateway_serial: str | None = None) -> Persona:
    """Return persona snapshot for one gateway (or global-only fields if None)."""
    tariff = await _read_with_override("persona.tariff_type", TARIFF_FLAT)
    country = await _read_with_override("persona.country_code", "US")
    detected_at = await db.get_config_value("persona.detected_at", None)
    overrides_active = _as_bool(await db.get_config_value("persona.overrides_active", False))

    if gateway_serial is None:
        return Persona(
            tariff_type=str(tariff),
            country_code=str(country),
            detected_at=detected_at,
            overrides_active=overrides_active,
        )

    return Persona(
        gateway_serial=gateway_serial,
        gateway_type=str(await _read_with_override(f"persona.gateway_type.{gateway_serial}", "unknown")),
        grid_status=str(await _read_with_override(f"persona.grid_status.{gateway_serial}", GRID_CONNECTED)),
        solar_present=_as_bool(await _read_with_override(f"persona.solar_present.{gateway_serial}", False)),
        mppt_enabled=_as_bool(await _read_with_override(f"persona.mppt_enabled.{gateway_serial}", False)),
        enphase_present=_as_bool(await _read_with_override(f"persona.enphase_present.{gateway_serial}", False)),
        has_ahub=_as_bool(await _read_with_override(f"persona.has_ahub.{gateway_serial}", False)),
        has_mac1=_as_bool(await _read_with_override(f"persona.has_mac1.{gateway_serial}", False)),
        tariff_type=str(tariff),
        country_code=str(country),
        detected_at=detected_at,
        overrides_active=overrides_active,
    )


async def is_grid_connected(gateway_serial: str) -> bool:
    v = await _read_with_override(f"persona.grid_status.{gateway_serial}", GRID_CONNECTED)
    return str(v) == GRID_CONNECTED


async def is_solar_present(gateway_serial: str) -> bool:
    return _as_bool(await _read_with_override(f"persona.solar_present.{gateway_serial}", False))


async def is_apower_s(gateway_serial: str) -> bool:
    return _as_bool(await _read_with_override(f"persona.mppt_enabled.{gateway_serial}", False))


async def is_pricing_available() -> bool:
    tariff = await _read_with_override("persona.tariff_type", TARIFF_FLAT)
    return str(tariff) in (TARIFF_DYNAMIC, TARIFF_TOU)


async def is_grid_charging_permitted(gateway_serial: str) -> bool:
    """False when off-grid AND no generator hybrid capability. Phase A
    conservative: True unless persona says off-grid — matches today's SD
    behaviour of blocking GRID_CHARGE/GRID_EXPORT when grid_connected=False.
    """
    return await is_grid_connected(gateway_serial)


async def is_australian() -> bool:
    cc = await _read_with_override("persona.country_code", "US")
    return str(cc).upper() == "AU"


async def sd_engine_flags(gateway_serial: str) -> SDEngineFlags:
    """Convenience: three-way summary consumed by SD rule gating.
    Phase A: computed but unused by rules; Phase B wires each rule to check."""
    return SDEngineFlags(
        pricing_available=await is_pricing_available(),
        solar_available=await is_solar_present(gateway_serial),
        grid_charging_permitted=await is_grid_charging_permitted(gateway_serial),
    )


# ── Override management (admin-only) ────────────────────────────────────

async def has_override(key: str) -> bool:
    v = await db.get_config_value(f"persona.override.{key}", None)
    return v is not None


async def set_override(key: str, value: Any) -> None:
    """Admin-mode only. Callers must enforce RBAC before calling this."""
    await db.set_config_value(f"persona.override.{key}", value)
    await db.set_config_value("persona.overrides_active", True)


async def clear_overrides() -> None:
    """Remove every persona.override.* key. Admin-mode only."""
    import aiosqlite
    async with aiosqlite.connect(db.get_db_path(), timeout=30.0) as conn:
        await conn.execute("DELETE FROM app_config WHERE key LIKE 'persona.override.%'")
        await conn.commit()
    await db.set_config_value("persona.overrides_active", False)


async def list_overrides() -> dict[str, Any]:
    import aiosqlite
    out: dict[str, Any] = {}
    async with aiosqlite.connect(db.get_db_path(), timeout=30.0) as conn:
        async with conn.execute("SELECT key, value FROM app_config WHERE key LIKE 'persona.override.%'") as cur:
            async for row in cur:
                out[row[0].removeprefix("persona.override.")] = row[1]
    return out


# ── Fleet-wide convenience ──────────────────────────────────────────────

async def any_gateway_has(model_substring: str, gateway_serials: Iterable[str]) -> bool:
    """True if any gateway's `persona.gateway_type` contains the substring
    (case-insensitive). E.g., `any_gateway_has("aHub", serials)` → true
    if any configured gateway is an aHub."""
    needle = model_substring.lower()
    for serial in gateway_serials:
        v = await _read_with_override(f"persona.gateway_type.{serial}", "unknown")
        if needle in str(v).lower():
            return True
    return False
