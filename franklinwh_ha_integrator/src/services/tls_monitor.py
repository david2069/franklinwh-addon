"""Daily TLS certificate expiry watch.

Scheduling a renewal is not the same as knowing it worked. `tailscale cert`
runs from a host-side timer (LaunchAgent or cron) that FHAI cannot see, and an
external timer fails silently — the first symptom of a stopped agent is every
browser refusing to connect at once, on a date nobody is watching.

So watch the *outcome* rather than the mechanism. days_remaining catches a
broken renewal whatever was supposed to perform it: a stopped agent, a revoked
certificate, a Tailscale account change, or a hand-installed certificate nobody
scheduled anything for. It needs no knowledge of which of those applies.

See docs/remote_access.md for the renewal mechanisms this is watching over.
"""
import json
import logging
import os
from typing import Any, Optional

from src.services import db
from src.services.security_checker import parse_cert_info

logger = logging.getLogger(__name__)

# Descending. One notification per threshold crossed, never one per day — an
# alert that repeats daily for three weeks is an alert people mute.
WARN_THRESHOLDS: tuple[int, ...] = (21, 14, 7, 3, 0)

_STATE_KEY = "tls_cert_expiry_notified"

# Tailscale renews well before this, so reaching the first threshold on an
# auto-renewing certificate means the renewal path itself has stopped — a
# materially different message from "time to rotate your self-signed cert".
_AUTO_RENEW_EXPECTED_BY = 21


async def _load_state() -> dict:
    raw = await db.get_config_value(_STATE_KEY, None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _compose(info: dict, days: int) -> str:
    subject = (info.get("subject") or "").replace("CN=", "")
    expires = (info.get("expires_at") or "")[:10]

    if days < 0:
        head = f"TLS certificate for {subject} EXPIRED on {expires}."
    elif days == 0:
        head = f"TLS certificate for {subject} expires TODAY."
    else:
        head = f"TLS certificate for {subject} expires in {days} day(s), on {expires}."

    if info.get("auto_renewing"):
        if days <= _AUTO_RENEW_EXPECTED_BY:
            tail = ("This certificate renews automatically and should already have "
                    "done so — the renewal job has likely stopped. Check the host "
                    "timer and run scripts/setup_tls_tailscale.sh --renew.")
        else:
            tail = "Automatic renewal is configured; no action expected."
    elif info.get("managed_by") == "self_signed":
        tail = ("Self-signed certificate — rotate it with "
                "scripts/setup_tls_mkcert.sh or gen_selfsigned_cert.py.")
    else:
        tail = "Issued by an external CA — renew it through whatever issued it."

    return f"{head} {tail}"


async def run_cert_expiry_check(notifier: Optional[Any] = None) -> dict:
    """Check the serving certificate and notify once per threshold crossed.

    Module-level rather than a method so SQLAlchemyJobStore can serialise the
    reference — the same constraint documented on persona re-detection.

    `notifier` exists for tests; production passes None and the real sender is
    imported lazily to keep this module importable without the HA stack.
    """
    cert_path = await db.get_config_value("tls_cert_path", None) or "/data/ssl/server.crt"

    if not os.path.exists(cert_path):
        logger.debug(f"tls_monitor: no certificate at {cert_path} — nothing to check.")
        return {"checked": False, "reason": "no certificate"}

    info = parse_cert_info(cert_path)
    if not info.get("valid"):
        # Unreadable is worse than expiring: TLS is already broken or about to
        # be, and the usual expiry arithmetic cannot run at all.
        logger.error(f"tls_monitor: certificate at {cert_path} is unreadable: {info.get('error')}")
        return {"checked": False, "reason": "unreadable", "error": info.get("error")}

    days = int(info.get("days_remaining", 0))
    state = await _load_state()

    # A changed expiry means the certificate was rotated — forget what we warned
    # about, so the next cycle starts clean rather than staying silent.
    if state.get("expires_at") != info.get("expires_at"):
        if state.get("expires_at"):
            logger.info(
                f"tls_monitor: certificate rotated "
                f"({state.get('expires_at')} → {info.get('expires_at')}) — resetting warnings."
            )
        state = {"expires_at": info.get("expires_at"), "notified": []}

    notified = list(state.get("notified") or [])
    crossed = [t for t in WARN_THRESHOLDS if days <= t]
    fresh = [t for t in crossed if t not in notified]

    if not fresh:
        logger.debug(f"tls_monitor: {days} day(s) remaining — nothing to warn about.")
        return {"checked": True, "days_remaining": days, "notified": False}

    message = _compose(info, days)
    logger.warning(f"tls_monitor: {message}")

    sent = False
    try:
        if notifier is None:
            from src.services.notification_sender import send_ha_notification
            notifier = send_ha_notification
        result = await notifier("cert_expiry", {"message": message, "days_remaining": days})
        sent = bool(result and result.get("sent"))
    except Exception as exc:
        # Never let a delivery failure stop us recording the crossing — but do
        # not mark it notified either, so the next run retries.
        logger.error(f"tls_monitor: could not send expiry notification: {exc!r}")
        return {"checked": True, "days_remaining": days, "notified": False, "error": str(exc)}

    state["notified"] = sorted(set(notified) | set(crossed), reverse=True)
    await db.set_config_value(_STATE_KEY, state)

    return {"checked": True, "days_remaining": days, "notified": True,
            "delivered": sent, "message": message}
