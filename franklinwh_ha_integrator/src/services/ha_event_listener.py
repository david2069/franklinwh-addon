"""Receive actionable-notification taps over the Home Assistant WebSocket.

The return leg was built as a webhook: the user pastes a `rest_command` and an
automation into configuration.yaml, and Home Assistant POSTs back to a URL they
have to keep correct. Every part of that is theirs to maintain, and every
failure of it is silent from here — a tap against a wrong or unreachable URL
produces nothing at all, so "no response recorded" and "nobody tapped" are the
same observation. That is precisely the state this integration was in: pushes
delivering 5/5 at 200, and not one response ever recorded.

Subscribing to the event directly removes the whole configuration surface.
Home Assistant raises `mobile_app_notification_action` when a companion-app
button is pressed; a WebSocket subscription receives it with no YAML, no URL,
no port, and nothing for the user to keep in sync.

It works in both deployments, which the webhook never really did:

  * as an add-on, over the Supervisor proxy at ws://supervisor/core/websocket,
    authenticated with the Supervisor token — plain HTTP inside the Docker
    network, so no TLS, no certificate trust, no reachable-from-HA question;
  * standalone, against the user's own Home Assistant with the long-lived
    token they already entered.

The webhook path is left in place. Someone whose YAML works today keeps
working, and both routes converge on the same handler — whichever arrives
first clears the pending record, and the other finds nothing to do.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

EVENT_TYPE = "mobile_app_notification_action"

#: Only our own buttons. Every action this integration sends is prefixed, and
#: the household's other notifications raise this same event.
ACTION_PREFIX = "FWH_"

#: The Supervisor's Home Assistant proxy. It serves the socket at
#: `/core/websocket`; `/core/api/` is the REST proxy and answers a socket
#: request with a 404 rather than an upgrade.
SUPERVISOR_BASE = "http://supervisor/core"

#: Reconnect backoff. Home Assistant restarts, and the socket dies with it;
#: that is routine rather than an error, so the first retries are quick and
#: quiet and only a sustained outage escalates.
BACKOFF_SECONDS = (2, 5, 10, 30, 60)


def websocket_url(base: str) -> str:
    """The WebSocket endpoint for a Home Assistant base URL.

    The two deployments do not share a path. Home Assistant itself serves the
    socket at `/api/websocket`, but the Supervisor proxy exposes it at
    `/core/websocket` — `/core/api/...` is the REST proxy, and asking there for
    a socket gets a 404 rather than an upgrade. `integration_conflicts._ws()`
    already connects to `http://supervisor/core/websocket`, which is the
    working example to match.
    """
    base = (base or "").rstrip("/")

    # Match the Supervisor proxy exactly rather than by path suffix: a
    # standalone Home Assistant served at https://example.com/core would
    # otherwise be sent to /core/websocket, which is not where it listens.
    suffix = "/websocket" if base == SUPERVISOR_BASE else "/api/websocket"

    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + suffix
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + suffix
    return base + suffix


def extract_action(event_data: dict) -> str:
    """The action id from an event payload, however the companion nests it."""
    data = event_data or {}
    return (
        data.get("action")
        or data.get("actionName")
        or (data.get("action_data") or {}).get("action")
        or ""
    )


def is_ours(action: str) -> bool:
    return bool(action) and action.startswith(ACTION_PREFIX)


async def _handle_event(event_data: dict) -> bool:
    """Feed one tap into the same handler the webhook uses. True if handled."""
    from src.routes.api_automation import OverridePayload, notification_override

    action = extract_action(event_data)
    if not is_ours(action):
        return False

    data = dict(event_data or {})
    data.setdefault("action", action)

    try:
        payload = OverridePayload(**{
            k: v for k, v in data.items()
            if k in OverridePayload.model_fields
        })
    except Exception:
        logger.warning("ha event listener: could not read the event payload", exc_info=True)
        return False

    logger.info(
        "ha event listener: %s tapped (req=%s) — handling over the WebSocket",
        action, payload.get_request_id() or "none",
    )
    try:
        await notification_override(payload)
    except Exception as exc:
        # The webhook may also be configured and may have got there first, in
        # which case the pending record is already gone. Not an error.
        logger.info("ha event listener: handler declined %s — %s", action, exc)
    return True



def explain(exc: Exception, url: str) -> str:
    """Turn a connection failure into something a standalone install can act on.

    Under the Supervisor the socket is plain HTTP on the Docker network and
    almost nothing can go wrong. A self-hosted Home Assistant is reached over
    whatever the user has in front of it, and the raw exceptions there are
    close to useless: "Invalid response status" is what a reverse proxy that
    drops the Upgrade header looks like, and a certificate error is what a
    self-signed Home Assistant looks like.
    """
    text = str(exc)
    low = text.lower()

    if "certificate verify failed" in low or "sslcertverificationerror" in low:
        return (f"{text} — Home Assistant's TLS certificate could not be verified. "
                "A self-signed certificate will fail here; use a trusted certificate, "
                "or point the connection at http:// on the local network.")
    if "404" in text and url.rsplit("/", 1)[-1] == "websocket":
        return (f"{text} — nothing is listening for a WebSocket at {url}. "
                "Check the Home Assistant URL, and that any reverse proxy in front "
                "of it forwards the Upgrade and Connection headers.")
    if "invalid response status" in low:
        return (f"{text} — the connection was not upgraded to a WebSocket. "
                "A reverse proxy that does not forward the Upgrade and Connection "
                "headers does this; so does Home Assistant still starting up.")
    if "refused the token" in low or "401" in text:
        return (f"{text} — Home Assistant rejected the token. A long-lived access "
                "token is created under your profile in Home Assistant.")
    return text


async def _connect_once(session, url: str, token: str, label: str = "home assistant") -> None:
    """Authenticate, subscribe, and consume until the socket closes."""
    async with session.ws_connect(url, timeout=15, heartbeat=30) as ws:
        hello = await ws.receive_json(timeout=10)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {hello.get('type')}")

        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json(timeout=10)
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant refused the token: {auth.get('type')}")

        await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": EVENT_TYPE})
        result = await ws.receive_json(timeout=10)
        if not result.get("success", False):
            raise RuntimeError(f"could not subscribe to {EVENT_TYPE}: {result}")

        logger.info(
            "ha event listener [%s]: subscribed to %s — actionable taps no "
            "longer depend on the rest_command webhook", label, EVENT_TYPE,
        )

        async for message in ws:
            try:
                msg = message.json()
            except Exception:
                continue
            if msg.get("type") != "event":
                continue
            event = msg.get("event") or {}
            # Only one subscription is open, but check anyway: a second one
            # added later must not have its events fed into this handler.
            if event.get("event_type") != EVENT_TYPE:
                continue
            await _handle_event(event.get("data") or {})


#: How often the set of instances is re-evaluated, so a Home Assistant added
#: in the UI starts being listened to without restarting the add-on.
REFRESH_SECONDS = 60


async def _resolve_targets() -> dict[str, dict]:
    """Every Home Assistant we need a subscription on, keyed by socket URL.

    A device route is foreign-keyed to an `ha_instances` row, and a tap is
    raised by the Home Assistant the companion app is registered with — so a
    phone attached to a second instance raises its event there and nowhere
    else. One socket on the default connection would simply never see it.

    Keyed by URL so two rows describing the same Home Assistant collapse to a
    single subscription; handling the same tap twice is harmless but pointless.
    """
    from src.routes.api_ha import _get_ha_client
    from src.services import db
    from src.services.ha_autoconfig import resolve_instance_token

    targets: dict[str, dict] = {}

    # The connection this add-on uses for everything else. Present even when
    # no device routes exist, so behaviour does not depend on the device table.
    try:
        base, auth_header, _env = await _get_ha_client()
        if base and auth_header:
            targets[websocket_url(base)] = {
                "url": websocket_url(base),
                "instance_id": None,
                "label": "this Home Assistant",
            }
    except Exception:
        logger.debug("ha event listener: no default connection", exc_info=True)

    # Any instance a device actually routes to.
    try:
        devices = await db.get_notification_devices() or []
        wanted = {d.get("ha_instance_id") for d in devices if d.get("enabled")}
        instances = await db.get_ha_instances() or []
    except Exception:
        logger.debug("ha event listener: could not read instances", exc_info=True)
        return targets

    for inst in instances:
        if not inst.get("enabled") or inst.get("id") not in wanted:
            continue
        host = (inst.get("host") or "").strip()
        if not host or not resolve_instance_token(inst):
            continue
        url = websocket_url(host)
        # The default connection and the seeded local instance are the same
        # Home Assistant; keep whichever names itself, but only one socket.
        targets[url] = {
            "url": url,
            "instance_id": inst.get("id"),
            "label": inst.get("alias") or inst.get("id"),
        }

    return targets


async def _token_for(target: dict) -> str:
    """Resolve the target's token now, not when the task was created.

    The Supervisor token is re-minted per container start and a user can
    change a long-lived token at any time, so this is read on every connect
    rather than captured once.
    """
    from src.routes.api_ha import _get_ha_client

    if target.get("instance_id"):
        from src.services import db
        from src.services.ha_autoconfig import resolve_instance_token

        inst = await db.get_ha_instance(target["instance_id"])
        return resolve_instance_token(inst) if inst else ""

    _base, auth_header, _env = await _get_ha_client()
    return (auth_header or "").removeprefix("Bearer ").strip()


async def _listen_forever(target: dict, stop: asyncio.Event | None = None) -> None:
    """Keep one instance's subscription open, reconnecting as needed."""
    import aiohttp

    attempt = 0
    while not (stop and stop.is_set()):
        try:
            token = await _token_for(target)
            if not token:
                raise RuntimeError("no usable token")

            async with aiohttp.ClientSession() as session:
                attempt = 0        # a successful connect resets the backoff
                await _connect_once(session, target["url"], token, target["label"])

            logger.info("ha event listener [%s]: connection closed — reconnecting",
                        target["label"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            log = logger.info if attempt < 2 else logger.warning
            log("ha event listener [%s]: %s — retrying in %ss",
                target["label"], explain(exc, target["url"]), delay)
            attempt += 1
            await asyncio.sleep(delay)


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """Hold a subscription open on every Home Assistant that has devices.

    Re-evaluates periodically so an instance added in the UI is picked up
    without a restart, and one that loses its last device is dropped.
    """
    tasks: dict[str, asyncio.Task] = {}
    try:
        while not (stop and stop.is_set()):
            try:
                targets = await _resolve_targets()
            except Exception:
                logger.warning("ha event listener: could not resolve targets", exc_info=True)
                targets = {}

            for url, target in targets.items():
                task = tasks.get(url)
                if task is None or task.done():
                    tasks[url] = asyncio.create_task(
                        _listen_forever(target, stop),
                        name=f"ha_event_listener:{target['label']}",
                    )
                    logger.info("ha event listener: watching %s (%s)",
                                target["label"], url)

            for url in [u for u in tasks if u not in targets]:
                logger.info("ha event listener: stopping watch on %s", url)
                tasks.pop(url).cancel()

            await asyncio.sleep(REFRESH_SECONDS)
    finally:
        for task in tasks.values():
            task.cancel()
