"""Where this instance is reachable from Home Assistant.

`fhai_host` is the callback URL Home Assistant POSTs approvals to. Getting it
wrong is uniquely nasty: notifications still go out and log success, and the
response simply never arrives, with no error on either side.

The old default was `http://fwhhai-app:8099` — a docker-compose container name.
Under the Supervisor that resolves to nothing, so a fresh add-on install
generated a Blueprint whose rest_command could never reach us. Same dead
callback as a stale URL, arrived at from the other direction.

Add-on hostnames depend on how the add-on was installed (local build vs
repository), so this asks the Supervisor rather than hardcoding a convention
that would break quietly for half of installs.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# The add-on is ingress-only (no `ports:` mapping), so Home Assistant reaches it
# over the Supervisor's internal network by hostname, in plain HTTP. TLS is
# refused in the add-on by scripts/start.sh — see security_guide.md §4a.2.
COMPOSE_FALLBACK = "http://fwhhai-app:8099"

_cache: Optional[str] = None
_looked_up = False


def supervisor_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""


async def addon_self_url() -> Optional[str]:
    """Base URL the Supervisor says this add-on answers on, or None.

    Cached: the hostname is fixed for the life of the container, and this is
    called from settings endpoints that the UI polls.
    """
    global _cache, _looked_up
    if _looked_up:
        return _cache
    _looked_up = True

    token = supervisor_token()
    if not token:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code != 200:
            # 403 here almost always means homeassistant_api/hassio_api is
            # missing from config.yaml — say so rather than leaving a bare code.
            logger.warning(
                f"addon_info: Supervisor returned {r.status_code} for /addons/self/info "
                f"— check hassio_api is granted in config.yaml. Falling back."
            )
            return None
        data = (r.json() or {}).get("data") or {}
    except Exception as exc:
        logger.warning(f"addon_info: could not ask the Supervisor for our hostname: {exc!r}")
        return None

    hostname = data.get("hostname")
    if not hostname:
        logger.warning("addon_info: Supervisor gave no hostname — falling back.")
        return None

    port = data.get("ingress_port") or 8099
    _cache = f"http://{hostname}:{port}"
    logger.info(f"addon_info: this add-on is reachable from Home Assistant at {_cache}")
    return _cache


async def resolve_fhai_host() -> str:
    """Resolve the callback base URL, most specific source first.

    An explicitly configured value always wins — someone may deliberately point
    callbacks at an external address. But when running as an add-on and the
    stored value is not the address the Supervisor reports, say so: that
    mismatch is the shape of a value carried over from a Docker install or a
    restored backup, and it silently breaks every callback.
    """
    from src.services import db

    configured = await db.get_config_value("fhai_host")
    derived = await addon_self_url()

    if configured:
        if derived and configured.rstrip("/") != derived.rstrip("/"):
            logger.warning(
                f"addon_info: fhai_host is {configured!r} but the Supervisor reports "
                f"this add-on at {derived!r}. Home Assistant callbacks will fail "
                f"unless the configured value is genuinely reachable from HA."
            )
        return configured

    if derived:
        return derived

    return os.environ.get("FHAI_HOST", COMPOSE_FALLBACK)


async def mqtt_service() -> Optional[dict]:
    """What the Supervisor knows about an MQTT broker on this instance.

    Returns the broker's connection details when some add-on *provides* the
    `mqtt` service, `None` when nothing does, and `None` when we are not running
    under the Supervisor and so cannot tell.

    This answers a different question from `/mqtt/status`, which reports only
    whether *our own* publisher has a connection. Conflating the two made the
    setup wizard tell a user with Mosquitto already running — and Zigbee2MQTT
    running against it — to "install the Mosquitto broker add-on". Whether a
    broker exists and whether we have connected to it are separate facts, and
    only the first of them decides between "install something" and "fix the
    connection to the thing you have".

    Not cached: a broker can be installed while the wizard is open, which is
    exactly what the Re-check button is for.
    """
    token = supervisor_token()
    if not token:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        logger.debug(f"addon_info: could not ask the Supervisor about MQTT: {exc!r}")
        return None

    if r.status_code != 200:
        # 400 is the documented answer when no add-on provides the service.
        return None

    data = (r.json() or {}).get("data") or {}
    # An empty object means the service exists but has no provider.
    if not data.get("host"):
        return None
    return data
