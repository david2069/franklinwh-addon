"""Self-service account endpoints — actions a user performs on themselves.

Split out of `api_security.py` (GH: users/roles Stage 1). Those handlers must
work for any authenticated principal, but they lived in a router that is
ADMIN_ONLY. Rather than carve per-route exceptions out of the tier — which is
the kind of list that drifts until someone exempts the wrong thing — the file
boundary IS the policy boundary: this module is mounted SELF_SERVICE, and it
may only ever contain endpoints that act on the caller's own account.

The invariant that makes that safe: every handler here derives its target user
from `claims["sub"]`, never from a path or body parameter. Anything taking a
`{username}` belongs in api_security.py under ADMIN_ONLY. Paths are unchanged
from before the move, so no frontend or API consumer is affected.
"""
import io
import logging

from fastapi import APIRouter, Depends, HTTPException

from src.services.db import get_user, update_user_profile, log_security_event
from src.services.crypto import hash_password, verify_password
from src.routes.api_security import (
    ChangePasswordRequest,
    TotpVerifyRequest,
    get_current_user_claims,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["account"])


@router.post("/security/change-password")
async def change_own_password(req: ChangePasswordRequest, claims: dict = Depends(get_current_user_claims)):
    """Allow logged-in user to change their own password."""
    username = claims.get("sub")
    user = await get_user(username)
    if not user or not verify_password(req.current_password, user["password_hash"]):
        raise HTTPException(401, "Invalid current password")

    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters long")

    hashed = hash_password(req.new_password)
    await update_user_profile(
        username,
        role=user["role"],
        dashboard=user["dashboard"],
        totp_secret=user["totp_secret"],
        totp_enabled=user["totp_enabled"],
        must_change_pw=0,
        password_hash=hashed,
        email=user.get("email"),
    )
    await log_security_event("USER_PASSWORD_CHANGED", f"User {username} successfully updated their own password", "auth_api")
    return {"ok": True}


# ── TOTP (MFA) APIs ───────────────────────────────────────────────────

def _qr_svg(provisioning_uri: str) -> str | None:
    """Render the enrolment URI as an inline SVG, or None if that is not possible.

    None is a supported outcome, not a failure. segno is a pure-Python optional
    dependency; where it is absent the UI falls back to typing the secret into
    the authenticator by hand, which every TOTP app supports. Enrolment must not
    depend on a QR library being present — losing MFA because a package is
    missing would be a worse failure than a slightly clumsier setup screen.

    Inline SVG rather than a PNG data URI: no Pillow, no binary, and it stays
    sharp on the phone screens this gets photographed from.
    """
    try:
        import segno
    except ImportError:
        logger.info("segno not installed — MFA enrolment will offer manual key entry only")
        return None

    try:
        # BytesIO, not StringIO: segno's SVG writer emits encoded bytes.
        buf = io.BytesIO()
        segno.make(provisioning_uri, error="m").save(
            buf, kind="svg", scale=5, border=2, dark="#0f172a", light="#ffffff", xmldecl=False
        )
        return buf.getvalue().decode("utf-8")
    except Exception:
        logger.exception("QR generation failed — falling back to manual key entry")
        return None


@router.post("/security/totp/setup")
async def setup_totp(claims: dict = Depends(get_current_user_claims)):
    """Generate TOTP setup details for the logged-in user."""
    import pyotp
    username = claims.get("sub")
    user = await get_user(username)
    if not user:
        raise HTTPException(404, "User not found")

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=username, issuer_name="FranklinWH_HA_Integrator")

    # Save the secret temporarily, totp_enabled remains 0 until verification
    await update_user_profile(
        username,
        role=user["role"],
        dashboard=user["dashboard"],
        totp_secret=secret,
        totp_enabled=0,
        must_change_pw=user["must_change_pw"],
        email=user.get("email"),
    )
    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_svg": _qr_svg(provisioning_uri),
        "issuer": "FranklinWH_HA_Integrator",
        "account": username,
    }


@router.post("/security/totp/verify")
async def verify_totp(req: TotpVerifyRequest, claims: dict = Depends(get_current_user_claims)):
    """Verify TOTP setup and enable MFA."""
    import pyotp
    username = claims.get("sub")
    user = await get_user(username)
    if not user or not user.get("totp_secret"):
        raise HTTPException(400, "TOTP is not configured.")

    totp = pyotp.TOTP(user["totp_secret"])
    if totp.verify(req.code.strip()):
        await update_user_profile(
            username,
            role=user["role"],
            dashboard=user["dashboard"],
            totp_secret=user["totp_secret"],
            totp_enabled=1,
            must_change_pw=user["must_change_pw"],
            email=user.get("email"),
        )
        await log_security_event("TOTP_ENABLED", f"TOTP two-factor authentication enabled for user {username}", "auth_api")
        return {"ok": True}
    else:
        raise HTTPException(400, "Invalid verification code.")


@router.post("/security/totp/disable")
async def disable_totp(claims: dict = Depends(get_current_user_claims)):
    """Disable TOTP MFA for the logged-in user."""
    username = claims.get("sub")
    user = await get_user(username)
    if not user:
        raise HTTPException(404, "User not found")

    await update_user_profile(
        username,
        role=user["role"],
        dashboard=user["dashboard"],
        totp_secret=None,
        totp_enabled=0,
        must_change_pw=user["must_change_pw"],
        email=user.get("email"),
    )
    await log_security_event("TOTP_DISABLED", f"TOTP two-factor authentication disabled for user {username}", "auth_api")
    return {"ok": True}
