"""Turn broker refusals into something a user can act on.

Both the publisher and the listener logged `MQTT disconnected: [code:135] Not
authorized. Reconnecting in 60s...` once a minute, forever. Three problems with
that: the code is meaningless to a reader, the cause (credentials) is never
named, and retrying a credential rejection cannot succeed — nothing changes
between attempts, so the loop is pure noise in a log a user has to read.

Reason codes are MQTT v5 (`[code:135]`) or v3.1.1 CONNACK returns, and aiomqtt
surfaces whichever the broker speaks, so both are mapped.
"""
from __future__ import annotations

import re

# MQTT v5 reason codes
_V5_NOT_AUTHORIZED = 135          # 0x87
_V5_BAD_CREDENTIALS = 134         # 0x86
_V5_BANNED = 138                  # 0x8A

# MQTT v3.1.1 CONNACK return codes
_V3_BAD_CREDENTIALS = 4
_V3_NOT_AUTHORIZED = 5

AUTH_CODES = {_V5_NOT_AUTHORIZED, _V5_BAD_CREDENTIALS, _V5_BANNED}
V3_AUTH_CODES = {_V3_BAD_CREDENTIALS, _V3_NOT_AUTHORIZED}

_CODE = re.compile(r"\[code:(\d+)\]")


def reason_code(exc: BaseException | str) -> int | None:
    """The numeric reason code in a broker error, or None."""
    m = _CODE.search(str(exc))
    return int(m.group(1)) if m else None


def is_auth_failure(exc: BaseException | str) -> bool:
    """Whether the broker refused us for credentials rather than a fault.

    v3.1.1's code 5 and v5's code 5 mean different things, so a bare 5 is only
    read as an auth failure when the text also says so — which is what aiomqtt
    renders alongside the code.
    """
    code = reason_code(exc)
    text = str(exc).lower()

    if code in AUTH_CODES:
        return True
    if code in V3_AUTH_CODES and ("auth" in text or "password" in text):
        return True
    return False


def is_refused(exc: BaseException | str) -> bool:
    """Whether nothing accepted the connection at all.

    Distinct from a credential rejection: the broker never answered, so the
    question is where it is, not who we are.
    """
    text = str(exc).lower()
    return any(t in text for t in (
        "connection refused", "connect call failed", "name or service not known",
        "nodename nor servname", "temporary failure in name resolution",
        "no route to host", "network is unreachable",
    ))


def explain(exc: BaseException | str, *, host: str = "", username: str = "") -> str:
    """A message naming the cause and the fix.

    `username` is reported only as set/unset. Connecting with no username at all
    is the most common way to land here — the Supervisor hands an add-on the
    broker's host even when it has no credentials to give, so the connection is
    attempted anonymously and a broker with `allow_anonymous false` (the
    Mosquitto add-on's default) refuses it.
    """
    if is_refused(exc):
        where = f" at {host}" if host else ""
        if host.startswith("localhost") or host.startswith("127."):
            return (
                f"Nothing is listening{where}. MQTT_HOST is not set, so this "
                "defaulted to localhost — set it to your broker's hostname."
            )
        return (
            f"Nothing is listening{where}. Check the broker is running and that "
            "MQTT_HOST and MQTT_PORT point at it."
        )

    if not is_auth_failure(exc):
        return str(exc)

    where = f" at {host}" if host else ""
    if not username:
        return (
            f"The MQTT broker{where} rejected the connection, and this add-on is "
            "connecting with no username. Home Assistant did not supply broker "
            "credentials, and the Mosquitto add-on refuses anonymous clients by "
            "default. Set mqtt_username and mqtt_password in the add-on's "
            "Configuration tab."
        )
    return (
        f"The MQTT broker{where} rejected the username '{username}'. Check "
        "mqtt_username and mqtt_password in the add-on's Configuration tab "
        "against the broker's own logins."
    )
