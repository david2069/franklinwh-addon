"""
pin_service.py — One-time PIN generation and validation for UI service toggles.

Security model:
  - UI inconvenience gate only (NOT an authentication mechanism).
  - Separate PIN slot per service: 'ha' and 'mqtt'.
  - 6-digit numeric PIN, single-use, 10-minute TTL.
  - After a successful PIN, a 24-hour session token is issued.
    Subsequent actions within 24h present the session token and bypass re-prompting.
  - Scripted/headless installs bypass PIN entirely via env vars — no ENABLE_PIN needed.
  - SHA-256 + random salt; stored in app_config DB (never logged or returned after generation).
"""
import hashlib
import logging
import secrets
from datetime import datetime, timezone, timedelta

from src.services import db

logger = logging.getLogger(__name__)

PIN_TTL_MINUTES = 10
SESSION_TTL_HOURS = 24
_VALID_SERVICES = ("ha", "mqtt", "smart_dispatch")


def _hash_pin(raw_pin: str, salt: str) -> str:
    """SHA-256 of salt+pin. Not cryptographically sensitive — this is a UI gate, not auth."""
    return hashlib.sha256(f"{salt}{raw_pin}".encode()).hexdigest()


async def generate_pin(service: str) -> str:
    """
    Generate a fresh 6-digit PIN for the given service ('ha' or 'mqtt').
    Stores hash + salt + expiry in DB app_config. Invalidates any existing PIN for this service.
    Returns the raw 6-digit string — display to user ONCE only.
    """
    if service not in _VALID_SERVICES:
        raise ValueError(f"Unknown service '{service}'. Valid: {_VALID_SERVICES}")

    raw_pin = "".join(str(secrets.randbelow(10)) for _ in range(6))
    salt = secrets.token_hex(16)
    pin_hash = _hash_pin(raw_pin, salt)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PIN_TTL_MINUTES)).isoformat()

    await db.set_config_value(f"{service}_pin_hash", pin_hash)
    await db.set_config_value(f"{service}_pin_salt", salt)
    await db.set_config_value(f"{service}_pin_expires_at", expires_at)

    logger.info(f"PIN generated for service '{service}' — expires in {PIN_TTL_MINUTES}min")
    return raw_pin


async def validate_pin(service: str, submitted: str) -> tuple[bool, str]:
    """
    Validate a submitted PIN against the stored hash.
    Returns (True, "") on success or (False, human-readable-reason) on failure.
    On success, the PIN is immediately invalidated (single-use).
    """
    if service not in _VALID_SERVICES:
        return False, "Unknown service"

    stored_hash = await db.get_config_value(f"{service}_pin_hash")
    stored_salt = await db.get_config_value(f"{service}_pin_salt")
    expires_at_str = await db.get_config_value(f"{service}_pin_expires_at")

    if not stored_hash or not stored_salt or not expires_at_str:
        return False, "No PIN active — click Generate PIN first"

    # Expiry check
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) > expires_at:
            await invalidate_pin(service)
            return False, "PIN expired — generate a new one"
    except Exception:
        return False, "PIN expiry data corrupt — generate a new one"

    # Hash comparison (constant-time to avoid timing attacks)
    submitted_hash = _hash_pin(submitted.strip(), stored_salt)
    if not secrets.compare_digest(submitted_hash, stored_hash):
        logger.warning(f"Incorrect PIN submitted for service '{service}'")
        return False, "Incorrect PIN"

    # Single-use: invalidate immediately on success
    await invalidate_pin(service)
    logger.info(f"PIN validated for service '{service}' — consumed")
    return True, ""


async def invalidate_pin(service: str) -> None:
    """Clear the PIN hash, salt, and expiry for a service."""
    await db.set_config_value(f"{service}_pin_hash", None)
    await db.set_config_value(f"{service}_pin_salt", None)
    await db.set_config_value(f"{service}_pin_expires_at", None)


async def get_pin_status(service: str) -> dict:
    """Return PIN liveness info — safe to return to the frontend (no hash exposed)."""
    expires_at_str = await db.get_config_value(f"{service}_pin_expires_at")
    if not expires_at_str:
        return {"active": False, "expires_at": None, "seconds_remaining": 0}
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        now = datetime.now(timezone.utc)
        if now > expires_at:
            return {"active": False, "expires_at": expires_at_str, "seconds_remaining": 0}
        return {
            "active": True,
            "expires_at": expires_at_str,
            "seconds_remaining": int((expires_at - now).total_seconds()),
        }
    except Exception:
        return {"active": False, "expires_at": None, "seconds_remaining": 0}


# ── 24-hour session token ─────────────────────────────────────────────────────

async def generate_session_token(service: str) -> str:
    """
    Issue a 24-hour session token after a successful PIN verification.
    Stored in DB — lets the frontend skip the PIN modal for 24h.
    Returns the raw URL-safe token. Client stores it in localStorage.
    """
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    await db.set_config_value(f"{service}_session_token", token)
    await db.set_config_value(f"{service}_session_expires_at", expires_at)
    logger.info(f"24h session token issued for service '{service}'")
    return token


async def validate_session_token(service: str, token: str) -> bool:
    """
    Validate a previously issued session token.
    Returns True if it matches DB and has not expired.
    """
    if service not in _VALID_SERVICES:
        return False
    stored_token = await db.get_config_value(f"{service}_session_token")
    expires_at_str = await db.get_config_value(f"{service}_session_expires_at")
    if not stored_token or not expires_at_str:
        return False
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at_str):
            logger.info(f"Session token expired for service '{service}'")
            return False
    except Exception:
        return False
    return secrets.compare_digest(stored_token, token)


async def validate_pin_or_session(
    service: str, pin: str, session_token: str | None
) -> tuple[bool, str]:
    """
    Accept either a valid 24h session token or a fresh OTP PIN.
    On success returns (True, new_session_token) — client should store this in localStorage.
    On failure returns (False, human-readable-reason).
    """
    # Fast path: valid session token → no PIN challenge needed
    if session_token and await validate_session_token(service, session_token):
        logger.info(f"Session token accepted for service '{service}' — bypassing PIN")
        new_token = await generate_session_token(service)
        return True, new_token

    # OTP PIN path
    valid, reason = await validate_pin(service, pin)
    if not valid:
        return False, reason
    new_token = await generate_session_token(service)
    return True, new_token
