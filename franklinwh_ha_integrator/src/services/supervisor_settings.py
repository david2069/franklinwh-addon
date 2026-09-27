"""Settings the Supervisor owns, resolved in Python rather than via bashio.

`run.sh` asks bashio for the timezone and the MQTT broker. On a real install
every one of those calls returned:

    ERROR: Unable to access the API, forbidden

while the identical token worked from Python — `/addons/self/info` succeeded in
the same boot. So the token is valid and the add-on's grants are sufficient;
something about bashio's own request is not. The consequences were not cosmetic:

  * no timezone, so the container ran UTC while TOU blocks, demand windows and
    export windows are local wall-clock — ten hours out in Sydney;
  * no broker credentials, so an anonymous connection the Mosquitto add-on
    refuses with "[code:135] Not authorized", and therefore no entities in
    Home Assistant at all.

This module fills in whatever is still missing, over the path that works. It
never overrides a value the user set, and never raises: outside the add-on
there is no Supervisor, and that is a normal state, not a failure.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


_site_timezone: str | None = None


async def resolve_site_timezone() -> str:
    """The site's IANA timezone, from Home Assistant. Cached. "" if unknown.

    Deliberately does NOT set the process timezone, and an earlier version of
    this did — which broke far more than it fixed.

    Storage in this application is UTC: 132 columns default to SQLite's
    `datetime('now')`, which is UTC whatever the process is set to, and the
    Python writers use `utcnow()` or an aware UTC value. But 53 call sites use
    naive `datetime.now()`, which is LOCAL. On a container running UTC those two
    agree, so the inconsistency was invisible — which is why this never showed
    up before, in this project or in FEM. Set the process to Australia/Sydney
    and the same column starts receiving values ten hours apart depending on
    which line wrote them, and every historical row silently means something
    different from the new ones.

    Only the scheduler needs local time — TOU blocks, demand windows and export
    windows are local wall-clock. APScheduler takes its own timezone, so it can
    have one without the rest of the process changing underneath the database.
    """
    global _site_timezone
    if _site_timezone is not None:
        return _site_timezone

    _site_timezone = ""
    try:
        from src.routes.api_ha import _ha_get

        config = await _ha_get("/config")
    except Exception as exc:
        logger.debug(f"timezone: Home Assistant config unavailable: {exc!r}")
        return _site_timezone

    zone = ((config or {}).get("time_zone") or "").strip() if isinstance(config, dict) else ""
    if not zone:
        return _site_timezone

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(zone)
    except Exception:
        logger.warning(f"timezone: Home Assistant reports '{zone}', which this image cannot load")
        return _site_timezone

    _site_timezone = zone
    logger.info(
        f"Site timezone: {zone} (from Home Assistant). The scheduler uses it for "
        "TOU blocks and demand windows; stored timestamps stay UTC."
    )
    return _site_timezone


def site_timezone() -> str:
    """The zone resolved at startup, or "" if it could not be determined."""
    return _site_timezone or ""


async def apply_mqtt_credentials(config) -> bool:
    """Fill in broker host and credentials the Supervisor knows about.

    Only fills what is blank. A value the user typed wins — they may be pointing
    at a broker the Supervisor has never heard of.

    Returns True if anything was filled in.
    """
    # A username already present means run.sh succeeded, or the user set one.
    if (getattr(config, "mqtt_username", "") or "").strip():
        return False

    try:
        from src.services.addon_info import mqtt_service

        broker = await mqtt_service()
    except Exception as exc:
        logger.debug(f"mqtt: Supervisor lookup failed: {exc!r}")
        return False

    if not broker:
        return False

    filled = []
    host = (broker.get("host") or "").strip()
    # run.sh writes a guessed "core-mosquitto" when it cannot ask, so a host
    # matching the guess is still worth replacing with the real answer.
    if host and (not config.mqtt_host or config.mqtt_host == "core-mosquitto"):
        config.mqtt_host = host
        filled.append("host")
    if broker.get("port") and not getattr(config, "mqtt_port", 0):
        config.mqtt_port = int(broker["port"])
        filled.append("port")
    if broker.get("username"):
        config.mqtt_username = broker["username"]
        filled.append("username")
    if broker.get("password"):
        config.mqtt_password = broker["password"]
        filled.append("password")

    if filled:
        logger.info(
            f"MQTT: {', '.join(filled)} supplied by the Supervisor "
            f"({config.mqtt_host}:{config.mqtt_port}"
            + (f", user {config.mqtt_username}" if config.mqtt_username else "")
            + ")"
        )
        return True
    return False


#: Hostnames a broker commonly answers to on a Docker network or LAN. Ordered
#: by how specific they are — a host called "mosquitto" is almost certainly the
#: broker; one called "localhost" is whatever happens to be running here.
BROKER_CANDIDATES = ("mosquitto", "mqtt", "broker", "emqx", "homeassistant", "localhost")


async def _reachable(host: str, port: int, timeout: float = 0.75) -> bool:
    """Whether something accepts TCP on host:port. No protocol check."""
    import asyncio

    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception:
        return False
    w.close()
    try:
        await w.wait_closed()
    except Exception:
        pass
    return True


async def suggest_broker(config) -> str:
    """Look for a broker when nobody configured one. Returns a hostname or "".

    Only runs when the host is still the built-in default — outside the add-on
    there is no Supervisor to ask, so an unset MQTT_HOST meant connecting to
    localhost and failing with "connection refused" and no indication of what to
    set. The failure was silent in the sense that mattered: correct-looking,
    actionable-looking, and impossible to act on.

    Deliberately SUGGESTS rather than connects. The add-on can safely assume
    core-mosquitto because the Supervisor vouches for it; here the candidates are
    guesses, and publishing this system's telemetry to a broker nobody named is
    not a guess worth making on the user's behalf.
    """
    if getattr(config, "mqtt_host_configured", False):
        return ""

    port = int(getattr(config, "mqtt_port", 1883) or 1883)
    for host in BROKER_CANDIDATES:
        if await _reachable(host, port):
            return host
    return ""


async def report_unconfigured_broker(config, env: str) -> None:
    """Say clearly that no broker is set, and what to do about it.

    Entities are published over MQTT Discovery, so without a broker the
    integration runs and Home Assistant sees nothing at all — the least obvious
    failure mode there is.
    """
    if getattr(config, "mqtt_host_configured", False) or env == "ha_addon":
        return

    found = await suggest_broker(config)
    port = int(getattr(config, "mqtt_port", 1883) or 1883)

    logger.warning("MQTT: no broker is configured — MQTT_HOST is not set.")
    logger.warning(
        "MQTT: entities are published over MQTT Discovery, so Home Assistant "
        "will see nothing until one is."
    )
    if found:
        logger.warning(
            f"MQTT: a broker is answering at {found}:{port}. "
            f"Set MQTT_HOST={found} to use it."
        )
    else:
        logger.warning(
            f"MQTT: nothing answered on port {port} at any of "
            f"{', '.join(BROKER_CANDIDATES)}. Set MQTT_HOST, MQTT_USERNAME and "
            "MQTT_PASSWORD to point at your broker."
        )
