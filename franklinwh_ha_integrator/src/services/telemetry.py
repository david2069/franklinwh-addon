"""Local usage telemetry — collection only. Nothing is transmitted.

Phase one of GH #40. Counters accumulate per period; a scheduled rollup
materialises one `telemetry_outbox` row holding the exact JSON that *would* be
sent. The UI renders that row, so what a user reads is what was built rather
than a second rendering that can drift from it.

No network egress exists in this module by design. You cannot leak what you do
not transmit, and building the mechanism first means the payload can be
inspected before anyone decides whether to ship it anywhere.

Three properties worth preserving if this is extended:

  * **Collection is off by default and gated on explicit consent.** Disabled
    means *not collecting*, not collecting-and-withholding — a populated outbox
    on an install that opted out is a nasty thing to discover.
  * **Route templates, never concrete paths.** `/api/gateways/{short_id}` is a
    metric; `/api/gateways/99900000000099900001` is a gateway serial in a
    counter name. The templated form is what keeps identifiers out.
  * **Pollers are excluded, not rate-limited.** This app polls itself several
    times a second; counting that makes every endpoint look busy and the data
    argues for keeping everything, which is the opposite of its purpose.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.services import db

logger = logging.getLogger(__name__)

CONSENT_KEY = "telemetry_enabled"
INSTALL_ID_KEY = "telemetry_install_id"
PAYLOAD_SCHEMA_VERSION = 1

# Endpoints driven by timers rather than people. Matched as prefixes against the
# route template. An explicit list rather than a rate heuristic: a user holding
# refresh looks like a poller, and a slow poller looks like a user.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/health",
    "/api/gateways/status/all",
    "/api/ws/",
    "/static/",
    "/api/automation/notifications/test-result",   # polled during a test
    "/api/system/telemetry",                       # never count reading the telemetry
)

# Top-level payload keys permitted to leave this module. Enforced, not
# conventional — free text is how serials, site names and addresses escape, and
# no amount of review catches that reliably.
ALLOWED_PAYLOAD_KEYS: frozenset = frozenset({
    "schema", "period", "install_id", "app_version", "install_method",
    "country", "province", "city", "utc_offset",
    "gateway_count", "hardware", "counters",
})

# Keys that must never appear at any depth, whatever the allowlist says.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "serial", "email", "password", "token", "address", "site_name",
    "latitude", "longitude", "lat", "lon", "credential", "secret", "key",
)


def current_period(now: Optional[datetime] = None) -> str:
    """UTC day bucket. UTC so the bucket boundary does not encode a timezone."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def is_excluded(route_template: str) -> bool:
    return any(route_template.startswith(p) for p in EXCLUDED_PREFIXES)


async def is_enabled() -> bool:
    """Consent. Absent means off — telemetry is opt-in, never opt-out."""
    raw = await db.get_config_value(CONSENT_KEY, None)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes")


async def install_id() -> str:
    """Stable random identifier for this install.

    Pseudonymous, not anonymous: it links every rollup from one install. That is
    the price of telling "ten installs used this once" apart from "one install
    used it ten times", which is the question this exists to answer. Say
    pseudonymous in user-facing copy — overstating it is the sort of claim that
    fails exactly when someone checks.
    """
    existing = await db.get_config_value(INSTALL_ID_KEY, None)
    if existing:
        return str(existing)
    generated = str(uuid.uuid4())
    await db.set_config_value(INSTALL_ID_KEY, generated)
    return generated


async def record(metric: str, count: int = 1, period: Optional[str] = None) -> bool:
    """Increment a counter. No-op unless the user has opted in."""
    if not await is_enabled():
        return False
    period = period or current_period()
    async with db.get_db() as conn:
        await conn.execute(
            """INSERT INTO telemetry_counters (period, metric, count)
               VALUES (?, ?, ?)
               ON CONFLICT(period, metric) DO UPDATE SET count = count + excluded.count""",
            (period, metric, count),
        )
        await conn.commit()
    return True


def validate_payload(payload: dict) -> None:
    """Raise if the payload carries anything it should not.

    Deliberately strict and deliberately at the boundary: a leak here is
    irreversible once transmitted, and "we reviewed it" is not a control.
    """
    extra = set(payload) - ALLOWED_PAYLOAD_KEYS
    if extra:
        raise ValueError(f"payload has non-allowlisted keys: {sorted(extra)}")

    def _walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                lowered = str(k).lower()
                for bad in FORBIDDEN_SUBSTRINGS:
                    if bad in lowered:
                        raise ValueError(f"forbidden key {path}{k!r} (matched {bad!r})")
                _walk(v, f"{path}{k}.")
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item, path)
        elif not isinstance(node, (str, int, float, bool, type(None))):
            raise ValueError(f"unsupported value type at {path}: {type(node).__name__}")

    _walk(payload)


async def _hardware_profile() -> dict:
    """Coarse deployment shape — what kit is in use, never which unit.

    Sourced from persona axes already detected from the gateway's own cloud
    record. Counts and booleans only: "two aPowers, DC solar, an aHub" is a
    deployment pattern; a serial is a device.

    Published as independent distributions rather than as a per-install tuple —
    see the aggregation note on GH #40. The join is what identifies, not any
    single field.
    """
    async with db.get_db() as conn:
        async with conn.execute(
            "SELECT key, value FROM app_config WHERE key LIKE 'persona.%'"
        ) as cur:
            axes = {r["key"]: r["value"] for r in await cur.fetchall()}

    # Axis keys come in two shapes: global ("persona.tariff_type") and
    # per-gateway ("persona.gateway_type.<serial>"). Matching on endswith missed
    # every per-gateway axis, because those end with the serial rather than the
    # axis name — the profile came back empty on a real install.
    def _values(axis: str) -> list:
        out = []
        for key, raw in axes.items():
            if not key.startswith("persona."):
                continue
            if key[len("persona."):].split(".", 1)[0] == axis:
                out.append(raw)
        return out

    def _flag(axis: str) -> Optional[bool]:
        raws = _values(axis)
        if not raws:
            return None
        # Any, not first: with several gateways "does this site have solar"
        # is true when one of them does.
        return any(str(r).strip().strip('"').lower() in ("true", "1", "yes") for r in raws)

    def _text(axis: str) -> Optional[str]:
        raws = _values(axis)
        if not raws:
            return None
        return str(raws[0]).strip().strip('"') or None

    battery_count = None
    try:
        async with db.get_db() as conn:
            async with conn.execute("SELECT COUNT(*) AS n FROM batteries") as cur:
                row = await cur.fetchone()
                battery_count = row["n"] if row else None
    except Exception as exc:
        logger.debug(f"telemetry: could not count batteries: {exc!r}")

    return {
        "gateway_type": _text("gateway_type"),
        "apower_count": battery_count,
        "solar_present": _flag("solar_present"),
        "mppt_enabled": _flag("mppt_enabled"),        # DC-coupled indicator
        "enphase_present": _flag("enphase_present"),  # AC-coupled / remote PV
        "has_ahub": _flag("has_ahub"),
        "has_mac1": _flag("has_mac1"),
        "grid_status": _text("grid_status"),
        "tariff_type": _text("tariff_type"),
    }


async def build_payload(period: str) -> dict:
    """Assemble the rollup for a period from accumulated counters."""
    from src import __version__
    from src.config.environment import detect_environment

    async with db.get_db() as conn:
        async with conn.execute(
            "SELECT metric, count FROM telemetry_counters WHERE period = ? ORDER BY metric",
            (period,),
        ) as cur:
            counters = {row["metric"]: row["count"] for row in await cur.fetchall()}

    # Country comes from the gateway's own cloud record via persona detection —
    # declared deliberately rather than inferred from an IP address server-side.
    country = await db.get_config_value("persona.country_code", None)

    # UTC offset in whole hours, NOT the IANA zone. The app knows
    # "Australia/Sydney", which is city-level and materially more identifying
    # than "AU"; the offset is shared by whole longitude bands. It is also close
    # to redundant with country here, since no timestamps are collected for it
    # to contextualise — included because it is cheap and coarse, and easily
    # dropped if it earns nothing.
    utc_offset = None
    zone = await db.get_config_value("persona.timezone", None)
    if zone:
        try:
            import zoneinfo
            from datetime import datetime as _dt
            off = _dt.now(zoneinfo.ZoneInfo(str(zone))).utcoffset()
            if off is not None:
                utc_offset = int(off.total_seconds() // 3600)
        except Exception as exc:
            logger.debug(f"telemetry: could not resolve offset for {zone!r}: {exc!r}")

    # Count, never identity. A wrong count is worse than none: the first
    # version of this called a function that does not exist (list_gateways —
    # it is get_all_gateways), and the bare except swallowed the AttributeError
    # and reported gateway_count 0 on an install with one gateway. Confidently
    # wrong data, silently. Report null on failure and say so in the log.
    gateway_count: Optional[int] = None
    try:
        gateway_count = len(await db.get_all_gateways())
    except Exception as exc:
        logger.warning(f"telemetry: could not count gateways: {exc!r}")

    payload = {
        "schema": PAYLOAD_SCHEMA_VERSION,
        "period": period,
        "install_id": await install_id(),
        "app_version": __version__,
        "install_method": detect_environment(),
        "country": country or None,
        # Province is one of a handful per country. City can be a single
        # household once joined to a hardware profile — harmless while nothing
        # is transmitted, decisive at publication. See GH #40.
        "province": await db.get_config_value("persona.province", None),
        "city": await db.get_config_value("persona.city", None),
        "utc_offset": utc_offset,
        "hardware": await _hardware_profile(),
        "gateway_count": gateway_count,
        "counters": counters,
    }
    validate_payload(payload)
    return payload


async def collect(period: Optional[str] = None, force: bool = False) -> dict:
    """Materialise one outbox row for a completed period.

    Returns a summary. Does not transmit — status is recorded as 'collected'
    precisely so a later send phase has something unambiguous to move on from.
    """
    if not force and not await is_enabled():
        return {"ok": False, "reason": "telemetry disabled"}

    period = period or current_period()
    payload = await build_payload(period)

    if not payload["counters"] and not force:
        return {"ok": False, "reason": "no counters for period", "period": period}

    body = json.dumps(payload, sort_keys=True, indent=2)
    async with db.get_db() as conn:
        # One row per period. Re-collecting refreshes it rather than appending a
        # near-duplicate that would double-count on any future send.
        await conn.execute(
            """INSERT INTO telemetry_outbox (period, payload_json, status)
               VALUES (?, ?, 'collected')
               ON CONFLICT(period) DO UPDATE SET
                 payload_json = excluded.payload_json,
                 status       = 'collected',
                 created_at   = datetime('now')""",
            (period, body),
        )
        await conn.commit()
        async with conn.execute(
            "SELECT id FROM telemetry_outbox WHERE period = ?", (period,)
        ) as cur:
            row = await cur.fetchone()

    outbox_id = row["id"] if row else None
    logger.info(
        f"telemetry: collected rollup for {period} — {len(payload['counters'])} counter(s), "
        f"outbox id={outbox_id}. Stored locally; nothing transmitted."
    )
    return {"ok": True, "period": period, "outbox_id": outbox_id,
            "counters": len(payload["counters"])}


async def run_daily_rollup() -> dict:
    """Scheduled entry point. Module-level for the SQLAlchemy jobstore."""
    if not await is_enabled():
        logger.debug("telemetry: disabled — no rollup.")
        return {"ok": False, "reason": "telemetry disabled"}
    return await collect()
