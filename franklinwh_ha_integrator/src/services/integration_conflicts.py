"""Other integrations talking to the same FranklinWH cloud account.

A Home Assistant install can carry a second FranklinWH integration — richo's
HACS `franklin_wh` is the common one — and both then poll the same cloud account
with the same credentials. FranklinWH's backend does not serve two clients
gracefully: the symptom is HTTP 200 with `result: null`, which surfaces in the
log as

    get_stats: API payload was empty. Returning last-known-good stats with
    is_stale=True.

twice a minute, and telemetry that is quietly last-known-good rather than live.
The duplicate entities are the visible half; the contention is the costly half.

Detection is read-only and cheap: Home Assistant's `/api/config` lists every
loaded component by domain. Acting on it is not cheap — disabling someone's
integration is a change to a part of their system we do not own — so that is a
separate, explicitly-consented call, never automatic.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Integration domains known to poll the FranklinWH cloud. Keyed by HA domain.
KNOWN_FRANKLIN_INTEGRATIONS = {
    "franklin_wh": {
        "name": "FranklinWH (franklin_wh)",
        "source": "HACS — github.com/richo/homeassistant-franklinwh",
        "note": (
            "Polls the same FranklinWH cloud account as this add-on. Two clients "
            "on one account is what produces the empty-payload warnings and stale "
            "telemetry."
        ),
    },
}


async def detect() -> dict:
    """Which competing integrations are loaded. Read-only.

    Returns `{"ok": bool, "conflicts": [...], "checked": bool}`. `checked` is
    False when Home Assistant could not be reached — which must not be reported
    as "no conflicts", because it is not the same thing.
    """
    try:
        from src.routes.api_ha import _ha_get

        config = await _ha_get("/config")
    except Exception as exc:
        logger.debug(f"conflicts: Home Assistant config unavailable: {exc!r}")
        return {"ok": True, "checked": False, "conflicts": []}

    if not isinstance(config, dict):
        return {"ok": True, "checked": False, "conflicts": []}

    components = config.get("components")
    if not isinstance(components, list):
        return {"ok": True, "checked": False, "conflicts": []}

    # `components` carries both bare domains and "domain.platform" entries.
    loaded = {str(c).split(".")[0] for c in components}

    conflicts = [
        {"domain": domain, **meta}
        for domain, meta in KNOWN_FRANKLIN_INTEGRATIONS.items()
        if domain in loaded
    ]
    if conflicts:
        logger.info(
            "Another FranklinWH integration is loaded: "
            + ", ".join(c["domain"] for c in conflicts)
        )
    return {"ok": True, "checked": True, "conflicts": conflicts}


async def _ws_command(messages: list[dict]) -> list[dict]:
    """Run commands against Home Assistant's WebSocket API via the Supervisor.

    Config entries cannot be enabled or disabled over the REST API — only the
    WebSocket API exposes them — and an add-on reaches that through the
    Supervisor's core proxy with its own token.

    aiohttp rather than `websockets`: both are installed, but only aiohttp is
    declared in requirements.txt, and the add-on image is built from that file.
    """
    from src.services.addon_info import supervisor_token

    token = supervisor_token()
    if not token:
        raise RuntimeError("no Supervisor token — this is only available to the add-on")

    import aiohttp

    replies: list[dict] = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("http://supervisor/core/websocket", timeout=15) as ws:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected greeting from Home Assistant: {hello.get('type')}")

            await ws.send_json({"type": "auth", "access_token": token})
            auth = await ws.receive_json(timeout=10)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant refused the Supervisor token: {auth}")

            for i, msg in enumerate(messages, start=1):
                await ws.send_json({"id": i, **msg})
                while True:
                    reply = await ws.receive_json(timeout=15)
                    if reply.get("id") == i and reply.get("type") == "result":
                        replies.append(reply)
                        break
    return replies


async def disable(domain: str) -> dict:
    """Disable every config entry for `domain`. Requires an explicit request.

    Never called automatically, and never as a side effect of detection: this
    changes an integration this add-on does not own. Disabled rather than
    deleted, so the user can put it back from the same screen and their
    history is not thrown away.
    """
    if domain not in KNOWN_FRANKLIN_INTEGRATIONS:
        return {"ok": False, "error": f"{domain} is not a known FranklinWH integration"}

    try:
        found = await _ws_command([{"type": "config_entries/get", "domain": domain}])
    except Exception as exc:
        logger.warning(f"conflicts: could not read config entries: {exc!r}")
        return {"ok": False, "error": str(exc)}

    result = (found[0] or {}).get("result") or []
    entries = [e for e in result if e.get("domain") == domain]
    if not entries:
        return {"ok": True, "disabled": 0, "detail": f"No {domain} config entry is set up."}

    already = [e for e in entries if e.get("disabled_by")]
    active = [e for e in entries if not e.get("disabled_by")]
    if not active:
        return {"ok": True, "disabled": 0, "detail": f"{domain} is already disabled."}

    try:
        replies = await _ws_command([
            {"type": "config_entries/disable", "entry_id": e["entry_id"], "disabled_by": "user"}
            for e in active
        ])
    except Exception as exc:
        logger.warning(f"conflicts: could not disable {domain}: {exc!r}")
        return {"ok": False, "error": str(exc)}

    failed = [r for r in replies if not r.get("success")]
    disabled = len(replies) - len(failed)
    needs_restart = any(((r.get("result") or {}).get("require_restart")) for r in replies)

    logger.info(f"conflicts: disabled {disabled} {domain} config entry(ies) at the user's request")
    return {
        "ok": not failed,
        "disabled": disabled,
        "already_disabled": len(already),
        "require_restart": bool(needs_restart),
        "detail": (
            f"Disabled {disabled} {domain} config entry(ies)."
            + (" Home Assistant needs a restart to finish." if needs_restart else "")
        ),
        "errors": [r.get("error") for r in failed] or None,
    }
