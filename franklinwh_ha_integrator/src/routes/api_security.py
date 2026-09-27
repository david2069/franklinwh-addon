"""API routes for security management (BKL-SEC-01)"""
import os
import secrets
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path
import jwt

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Response, Cookie, status, Depends, Request
from pydantic import BaseModel

from src.services.db import (
    get_config_value,
    set_config_value,
    insert_api_token,
    revoke_api_token,
    get_active_api_tokens,
    log_security_event,
    get_user,
    insert_user,
    delete_user,
    update_user_profile,
    list_users,
    get_user_count,
)
from src.services.crypto import hash_password, verify_password
from src.services.security_checker import parse_cert_info, update_security_snapshot, run_tamper_check
from src.config.environment import get_data_dir, detect_environment

logger = logging.getLogger(__name__)

# Enforced role set (schema v56). Collapsed from the 8 aspirational values —
# supervisor/control/user/guest/inkypi/inkpi/inkypi2 — which were stored but
# never checked. Single source of truth for both the create and update
# validators, which previously carried duplicate literal tuples that had
# already drifted apart from the two UI dropdowns.
VALID_ROLES = ("admin", "operator", "viewer")


async def get_or_create_jwt_secret() -> str:
    """Return the persisted JWT signing secret, generating one if absent.

    Never falls back to a shared literal. Both call sites (this module's login
    handler and middleware.auth.verify_jwt) previously defaulted to a hardcoded
    placeholder secret committed to this public repo. Anyone who could reach the
    port could sign {"sub": "x", "role": "admin"} with it and be admin, which
    made every other access-control measure decorative.

    Generation lives here (the write path) rather than in verification: a
    verifier that mints secrets would happily accept the first token it sees
    after a secret goes missing.
    """
    secret = await get_config_value("jwt_secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        await set_config_value("jwt_secret_key", secret)
        logger.warning(
            "jwt_secret_key was absent — generated a new one. Any previously "
            "issued session tokens are now invalid and users must log in again."
        )
    return secret
router = APIRouter(tags=["security"])

class SecuritySetupRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    security_enabled: Optional[bool] = None
    tls_enabled: Optional[bool] = None
    mtls_enabled: Optional[bool] = None
    tls_cert_path: Optional[str] = None
    tls_key_path: Optional[str] = None
    client_ca_path: Optional[str] = None

class TokenCreateRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = None

class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str
    dashboard: str = "standard"
    email: Optional[str] = None

class UserUpdateRequest(BaseModel):
    role: str
    dashboard: str = "standard"
    email: Optional[str] = None

class UserChangePasswordRequest(BaseModel):
    username: str
    new_password: str

class TotpVerifyRequest(BaseModel):
    code: str

# ── Route Authentication Dependencies ────────────────────────────────
async def get_current_user_claims(request: Request) -> dict:
    security_enabled = str(await get_config_value("security_enabled") or "false").lower() in ("true", "1", "yes")
    if not security_enabled:
        return {"sub": "admin", "role": "admin", "dashboard": "standard", "must_change_pw": 0}
        
    if not hasattr(request.state, "claims"):
        raise HTTPException(status_code=401, detail="Unauthorized — Authentication required")
    return request.state.claims

def require_admin(claims: dict = Depends(get_current_user_claims)):
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden — Administrator role required")
    return claims

# NOTE: `require_roles(...)` used to live here. Removed — it had zero call
# sites for its entire life, while appearing (in code review and in
# docs/security_guide.md) to be the mechanism enforcing the documented role
# matrix. Nothing was enforced. Multi-role authorization is being reintroduced
# as router-level tiers, applied at mount time so new routes are covered by
# default rather than by remembering to opt in.


@router.get("/security/status")
async def get_security_status():
    """Retrieve current security configuration, certificates metadata, and API tokens."""
    try:
        security_enabled = str(await get_config_value("security_enabled") or "false").lower() in ("true", "1", "yes")
        tls_enabled = str(await get_config_value("tls_enabled") or "false").lower() in ("true", "1", "yes")
        mtls_enabled = str(await get_config_value("mtls_enabled") or "false").lower() in ("true", "1", "yes")
        
        tls_cert_path = await get_config_value("tls_cert_path") or "/data/ssl/server.crt"
        tls_key_path = await get_config_value("tls_key_path") or "/data/ssl/server.key"
        client_ca_path = await get_config_value("client_ca_path") or "/data/ssl/ca.crt"
        
        cert_info = parse_cert_info(tls_cert_path) if os.path.exists(tls_cert_path) else None
        client_ca_info = parse_cert_info(client_ca_path) if os.path.exists(client_ca_path) else None
        
        tokens = await get_active_api_tokens()
        # Redact token hashes for security
        for t in tokens:
            if "token_hash" in t:
                del t["token_hash"]
                
        admin_username = await get_config_value("admin_username") or "admin"
        admin_password_hash = await get_config_value("admin_password_hash")
        setup_complete = admin_password_hash is not None
        
        tamper_check_passed = await run_tamper_check()
        
        # Check if there's HA token configured
        ha_token = await get_config_value("ha_token")
        ha_token_configured = ha_token is not None and len(ha_token) > 0
        
        return {
            # Lets the UI say when a control is inert here — the TLS switch is
            # ignored under add-on ingress, and silently doing nothing is how
            # that switch went unnoticed for as long as it did.
            "env": detect_environment(),
            "security_enabled": security_enabled,
            "tls_enabled": tls_enabled,
            "mtls_enabled": mtls_enabled,
            "tls_cert_path": tls_cert_path,
            "tls_key_path": tls_key_path,
            "client_ca_path": client_ca_path,
            "cert_info": cert_info,
            "client_ca_info": client_ca_info,
            "active_api_tokens": tokens,
            "admin_username": admin_username,
            "setup_complete": setup_complete,
            "tamper_check_passed": tamper_check_passed,
            "ha_token_configured": ha_token_configured,
        }
    except Exception as e:
        logger.error(f"Failed to fetch security status: {e}")
        raise HTTPException(500, f"Failed to fetch security status: {e}")

async def _warn_if_callback_scheme_now_wrong(tls_enabled: bool, actor: str) -> None:
    """Flag a callback URL whose scheme no longer matches the transport.

    fhai_host is a second copy of "how do you reach this instance", and Home
    Assistant holds a third in its rest_command. Flipping TLS invalidates both
    without touching either, so approvals stop working with nothing to say why
    — this has now bitten three times in one afternoon.

    It warns rather than rewrites, because a mismatch is not always wrong: an
    instance behind a reverse proxy that terminates TLS is correctly
    https://outside and plain HTTP within (security_guide.md 4a.4). Rewriting
    would break that topology to fix the simpler one. A warning is right for
    both, and says exactly what to change.
    """
    configured = await get_config_value("fhai_host")
    if not configured:
        return  # derived at call time; nothing stale to go wrong

    expected = "https://" if tls_enabled else "http://"
    if str(configured).startswith(expected):
        return

    corrected = expected + str(configured).split("://", 1)[-1]
    message = (
        f"fhai_host is {configured!r} but TLS is now "
        f"{'on' if tls_enabled else 'off'}. Home Assistant callbacks will fail "
        f"until it is {corrected!r} — and HA's own rest_command holds a second "
        f"copy that must match. Ignore this if a reverse proxy terminates TLS "
        f"in front of this instance."
    )
    await log_security_event("CALLBACK_URL_SCHEME_MISMATCH", f"{message} (noticed after change by {actor})", "web_ui")
    logger.warning("SECURITY CALLBACK_URL_SCHEME_MISMATCH — %s", message)


async def _audit_setting(key: str, requested, event: str, actor: str = "unknown") -> None:
    """Write a security setting, recording old -> new when it actually changes.

    Returns early on None (field absent from the request) and on a no-op write,
    so the audit trail carries decisions rather than the wizard's habit of
    re-posting every field whenever anything is saved.

    Booleans are stored as the strings "true"/"false" to match how the rest of
    this module reads them back.
    """
    if requested is None:
        return

    new_value = ("true" if requested else "false") if isinstance(requested, bool) else requested
    previous = await get_config_value(key)

    await set_config_value(key, new_value)

    if str(previous) == str(new_value):
        return

    detail = f"{key}: {previous!r} -> {new_value!r} (by {actor})"

    await log_security_event(event, detail, "web_ui")

    # Mirror into the application log as well. The two stores fail differently:
    # the audit table is a row someone with database access can remove, the app
    # log is a stream that has usually already been shipped elsewhere. A change
    # to how the instance is reachable should be awkward to erase from both, and
    # it also means the Logs tab and its JSON/CSV export show it — which is
    # where people look first, having no reason to know the audit trail is a
    # separate tab reading a separate table.
    logger.warning("SECURITY %s — %s", event, detail)


@router.post("/security/setup")
async def setup_security(
    req: SecuritySetupRequest,
    claims: dict = Depends(get_current_user_claims),
):
    """Wizard setup endpoint to configure administrator credentials and security modes.

    Takes claims purely to attribute the audit entries. "someone, via the web
    UI, turned HTTPS off" is not an audit trail — on a multi-account instance it
    names no one, and every account with the admin role looks identical in the
    record.
    """
    actor = (claims or {}).get("sub") or "unknown"
    try:
        # 1. Update Username if provided
        current_admin_username = await get_config_value("admin_username") or "admin"
        username = current_admin_username
        if req.username is not None:
            username = req.username.strip()
            if not username:
                raise HTTPException(400, "Username cannot be empty")
            await set_config_value("admin_username", username)
            
        # 2. Update Password if provided
        current_admin = await get_user(current_admin_username)
        password_hash = current_admin["password_hash"] if current_admin else ""
        if req.password is not None:
            password = req.password
            if len(password) < 8:
                raise HTTPException(400, "Password must be at least 8 characters long")
            password_hash = hash_password(password)
            await set_config_value("admin_password_hash", password_hash)
            await log_security_event(
                "ADMIN_PASSWORD_UPDATED", f"Administrator password updated (by {actor})", "web_ui"
            )
            logger.warning("SECURITY ADMIN_PASSWORD_UPDATED — by %s", actor)

        # Only touch the account when the request actually carries credentials.
        # This endpoint is shared: the setup wizard posts every field, but the
        # transport toggle posts tls_enabled alone. The branch below rewrites the
        # profile with role="admin" and dashboard="standard" hardcoded, so
        # running it unconditionally meant flipping HTTPS could silently reset a
        # customised dashboard and clear must_change_pw. Nothing to update if
        # neither a username nor a password was supplied.
        if req.username is None and req.password is None:
            pass
        elif req.username is not None and username != current_admin_username:
            await delete_user(current_admin_username)
            await insert_user(username, password_hash, role="admin", dashboard="standard", must_change_pw=0)
        else:
            if current_admin:
                await update_user_profile(
                    username,
                    role="admin",
                    dashboard="standard",
                    totp_secret=current_admin.get("totp_secret"),
                    totp_enabled=current_admin.get("totp_enabled"),
                    must_change_pw=0,
                    password_hash=password_hash,
                    email=current_admin.get("email")
                )
            else:
                await insert_user(username, password_hash, role="admin", dashboard="standard", must_change_pw=0)

        # 3. Handle security enable/disable toggle
        if req.security_enabled is not None:
            admin_password_hash = await get_config_value("admin_password_hash")
            if req.security_enabled and not admin_password_hash:
                raise HTTPException(400, "Cannot enable security mode without setting an administrator password first.")
                
            await set_config_value("security_enabled", "true" if req.security_enabled else "false")
            
            # Generate JWT Secret Key if enabled and not already present
            if req.security_enabled:
                await get_or_create_jwt_secret()
                    
            event_name = "SECURITY_ENABLED" if req.security_enabled else "SECURITY_DISABLED"
            await log_security_event(
                event_name, f"Security mode toggled to {req.security_enabled} (by {actor})", "web_ui"
            )
            logger.warning("SECURITY %s — by %s", event_name, actor)

        # 4. Handle other TLS / mTLS configuration parameters
        #
        # These are audited for the same reason security_enabled is: they decide
        # how the instance is reachable. Only security_enabled used to be logged,
        # so a TLS change left no trace anywhere — the value simply differed from
        # what someone remembered setting, with nothing to confirm or deny it.
        # "Did I turn that off?" was unanswerable from inside the app.
        #
        # Logged only on an actual change: the wizard re-posts every field on
        # every save, so logging each write would bury real changes in noise.
        await _audit_setting("tls_enabled", req.tls_enabled, "TLS_SETTING_CHANGED", actor)
        if req.tls_enabled is not None:
            await _warn_if_callback_scheme_now_wrong(req.tls_enabled, actor)
        await _audit_setting("mtls_enabled", req.mtls_enabled, "MTLS_SETTING_CHANGED", actor)
        await _audit_setting("tls_cert_path", req.tls_cert_path, "TLS_PATH_CHANGED", actor)
        await _audit_setting("tls_key_path", req.tls_key_path, "TLS_PATH_CHANGED", actor)
        await _audit_setting("client_ca_path", req.client_ca_path, "TLS_PATH_CHANGED", actor)

        # 5. Re-key snapshot signatures to reflect new settings
        await update_security_snapshot()
        
        return {"ok": True, "message": "Security settings successfully updated."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during security setup: {e}")
        raise HTTPException(500, f"Error during security setup: {e}")

@router.post("/security/login")
async def login(req: LoginRequest, response: Response, request: Request):
    """Authenticate credentials, issuing a JWT session cookie."""
    try:
        # Verify credentials
        user = await get_user(req.username)
        if not user or not verify_password(req.password, user["password_hash"]):
            await log_security_event("LOGIN_FAILED", f"Failed login attempt for username: {req.username}", "auth_api")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
            
        # Check if TOTP is enabled
        if user.get("totp_enabled") == 1:
            if not req.totp_code:
                return {
                    "ok": False,
                    "mfa_required": True,
                    "message": "Two-factor authentication code is required."
                }
            import pyotp
            totp = pyotp.TOTP(user["totp_secret"])
            if not totp.verify(req.totp_code.strip()):
                await log_security_event("LOGIN_MFA_FAILED", f"MFA verification failed for username: {req.username}", "auth_api")
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid two-factor authentication code")
            
        # Generate JWT
        secret_key = await get_or_create_jwt_secret()
        jwt_expire_minutes_str = await get_config_value("jwt_expire_minutes")
        jwt_expire_minutes = int(jwt_expire_minutes_str) if jwt_expire_minutes_str else 1440
        
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user["username"],
            "role": user["role"],
            "dashboard": user["dashboard"],
            "must_change_pw": user["must_change_pw"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=jwt_expire_minutes)).timestamp()),
        }
        token = jwt.encode(payload, secret_key, algorithm="HS256")
        
        # Set JWT session cookie (Secure and HTTPOnly)
        # `secure` must match the actual transport, not an aspiration.
        #
        # This was hardcoded True. A Secure cookie is only ever sent back over
        # HTTPS, so on a plain-HTTP install the browser discarded it: login
        # returned 200 with a token, the cookie never came back, `/` saw no
        # session and 303'd to /login — an infinite login loop with no error
        # message anywhere. It went unnoticed because nobody had enabled the
        # security perimeter before; every install ran unauthenticated.
        #
        # Derived from OBSERVED transport only — the scheme this request
        # actually arrived on, or X-Forwarded-Proto when TLS terminates at a
        # reverse proxy or HA Ingress.
        #
        # Deliberately NOT the `tls_enabled` config flag. That flag is an
        # intent, not a fact, and in the Docker image it is not even wired up:
        # the Dockerfile CMD runs `uvicorn src.main:app` directly, so the
        # ssl_keyfile/ssl_certfile block under `if __name__ == "__main__"` in
        # main.py never executes. Trusting the flag would mark the cookie
        # Secure while the server still spoke plain HTTP — the browser would
        # discard it and login would loop forever, which is precisely the bug
        # this function was just fixed for. A flag cannot be allowed to
        # re-create it.
        _fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        _is_https = _fwd == "https" or request.url.scheme == "https"
        if not _is_https:
            logger.info(
                "login: issuing session cookie without Secure — connection is plain HTTP. "
                "Enable TLS (Security tab) to protect the session token in transit."
            )
        # Name scoped to transport — see middleware/auth.py. A plain-HTTP
        # response cannot overwrite a Secure cookie of the same name, so reusing
        # one name stranded anyone who had signed in before TLS was switched off.
        from src.middleware.auth import session_cookie_name
        response.set_cookie(
            key=session_cookie_name(_is_https),
            value=token,
            httponly=True,
            samesite="lax",
            secure=_is_https,
            max_age=jwt_expire_minutes * 60,
        )
        
        await log_security_event("LOGIN_SUCCESS", f"User {user['username']} logged in successfully", "auth_api")
        return {
            "ok": True,
            "token": token,
            "role": user["role"],
            "dashboard": user["dashboard"],
            "must_change_pw": user["must_change_pw"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(500, f"Authentication failed: {e}")

@router.post("/security/logout")
async def logout(response: Response):
    """Clear administrator JWT session cookie."""
    # Clear both: the session may have been issued on the other transport, and
    # signing out must not leave a cookie behind that still authenticates.
    from src.middleware.auth import SESSION_COOKIE_SECURE, SESSION_COOKIE_PLAIN
    response.delete_cookie(SESSION_COOKIE_SECURE)
    response.delete_cookie(SESSION_COOKIE_PLAIN)
    await log_security_event("LOGOUT", "User logged out", "auth_api")
    return {"ok": True, "message": "Successfully logged out."}

@router.post("/security/tokens")
async def generate_token(req: TokenCreateRequest):
    """Generate a long-lived API Token for external automated integrations (e.g. Home Assistant)."""
    try:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "Token name cannot be empty")
            
        raw_token = "fhai_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        
        expires_at = None
        if req.expires_in_days:
            now = datetime.utcnow()
            expires_at = (now + timedelta(days=req.expires_in_days)).isoformat()
            
        token_id = await insert_api_token(name, token_hash, expires_at)
        await log_security_event("API_TOKEN_CREATED", f"API Token '{name}' generated", "web_ui")
        
        return {
            "id": token_id,
            "name": name,
            "token": raw_token,
            "expires_at": expires_at,
        }
    except Exception as e:
        logger.error(f"Failed to generate API token: {e}")
        raise HTTPException(500, f"Failed to generate API token: {e}")

@router.delete("/security/tokens/{token_id}")
async def delete_token(token_id: int):
    """Revoke and delete a long-lived API Token."""
    try:
        await revoke_api_token(token_id)
        await log_security_event("API_TOKEN_REVOKED", f"API Token ID {token_id} revoked", "web_ui")
        return {"ok": True, "message": "Token successfully revoked."}
    except Exception as e:
        logger.error(f"Failed to revoke token: {e}")
        raise HTTPException(500, f"Failed to revoke token: {e}")

@router.post("/security/certificates")
async def upload_certificates(
    tls_cert: Optional[UploadFile] = File(None),
    tls_key: Optional[UploadFile] = File(None),
    client_ca: Optional[UploadFile] = File(None),
):
    """Upload custom server certificates, keys, or client CAs to persistence ssl folder."""
    try:
        ssl_dir = get_data_dir() / "ssl"
        ssl_dir.mkdir(parents=True, exist_ok=True)
        
        files_written = []
        
        if tls_cert:
            cert_path = ssl_dir / "server.crt"
            content = await tls_cert.read()
            with open(cert_path, "wb") as f:
                f.write(content)
            files_written.append("server.crt")
            await set_config_value("tls_cert_path", str(cert_path))
            
        if tls_key:
            key_path = ssl_dir / "server.key"
            content = await tls_key.read()
            with open(key_path, "wb") as f:
                f.write(content)
            try:
                os.chmod(key_path, 0o600)  # Restrict private key file access
            except Exception as e:
                logger.warning(f"Could not set chmod 600 on uploaded key: {e}")
            files_written.append("server.key")
            await set_config_value("tls_key_path", str(key_path))
            
        if client_ca:
            ca_path = ssl_dir / "ca.crt"
            content = await client_ca.read()
            with open(ca_path, "wb") as f:
                f.write(content)
            files_written.append("ca.crt")
            await set_config_value("client_ca_path", str(ca_path))
            
        if files_written:
            await log_security_event("CERTIFICATE_UPLOADED", f"Uploaded cert files: {', '.join(files_written)}", "web_ui")
            # Update snapshot since files changed
            await update_security_snapshot()
            return {"ok": True, "message": f"Successfully uploaded: {', '.join(files_written)}"}
            
        return {"ok": False, "message": "No files uploaded."}
    except Exception as e:
        logger.error(f"Certificate upload failed: {e}")
        raise HTTPException(500, f"Certificate upload failed: {e}")


# ── User and Profile Management APIs ─────────────────────────────────

@router.get("/security/users", dependencies=[Depends(require_admin)])
async def get_users():
    """List all registered users (passwords redacted). Only for admin."""
    users = await list_users()
    for u in users:
        if "password_hash" in u:
            del u["password_hash"]
    return users


@router.post("/security/users", dependencies=[Depends(require_admin)])
async def create_user_endpoint(req: UserCreateRequest):
    """Create a new user. Only for admin."""
    username = req.username.strip()
    if not username:
        raise HTTPException(400, "Username cannot be empty")
    
    # Check duplicate
    existing = await get_user(username)
    if existing:
        raise HTTPException(400, f"User '{username}' already exists")
        
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters long")
        
    hashed = hash_password(req.password)
    # Validate role
    valid_roles = VALID_ROLES
    if req.role not in valid_roles:
        raise HTTPException(400, f"Invalid role. Must be one of {valid_roles}")
        
    valid_dbs = ('standard', 'inkypi', 'guest', 'inkypi2')
    if req.dashboard not in valid_dbs:
        raise HTTPException(400, f"Invalid dashboard. Must be one of {valid_dbs}")

    email = req.email.strip() if req.email else None
    if email:
        if "@" not in email or "." not in email:
            raise HTTPException(400, "Invalid email address format")

    await insert_user(username, hashed, req.role, req.dashboard, email=email)
    await log_security_event("USER_CREATED", f"User {username} created with role {req.role}", "admin_api")
    return {"ok": True}

@router.put("/security/users/{username}", dependencies=[Depends(require_admin)])
async def update_user_endpoint(
    username: str,
    req: UserUpdateRequest,
    claims: dict = Depends(get_current_user_claims)
):
    """Update user profile. Only for admin."""
    user = await get_user(username)
    if not user:
        raise HTTPException(404, "User not found")
        
    # Safeguard lockout check
    current_admin = claims.get("sub")
    if username == current_admin or username == "admin":
        if req.role != "admin":
            raise HTTPException(400, "Cannot change your own administrative role to prevent lockout")
        if req.dashboard != "standard":
            raise HTTPException(400, "Cannot change your own default dashboard away from standard to prevent lockout")

    # Last-admin guard. The check above only protects the CALLER's own account,
    # so with two admins A and B, A could demote B and B could demote A — or
    # more simply, an admin acting through an API token could demote the only
    # human admin and leave the instance administrable by nobody. The delete
    # path already refuses to remove the last admin; the update path did not.
    if user["role"] == "admin" and req.role != "admin":
        remaining = [u for u in (await list_users() or [])
                     if u["role"] == "admin" and u["username"] != username]
        if not remaining:
            raise HTTPException(
                400,
                "Cannot demote the last administrator — the instance would have no "
                "admin. Promote another account first."
            )
            
    # Validate inputs
    valid_roles = VALID_ROLES
    if req.role not in valid_roles:
        raise HTTPException(400, f"Invalid role. Must be one of {valid_roles}")
        
    valid_dbs = ('standard', 'inkypi', 'guest', 'inkypi2')
    if req.dashboard not in valid_dbs:
        raise HTTPException(400, f"Invalid dashboard. Must be one of {valid_dbs}")
        
    email = req.email.strip() if req.email else None
    if email:
        if "@" not in email or "." not in email:
            raise HTTPException(400, "Invalid email address format")
            
    await update_user_profile(
        username=username,
        role=req.role,
        dashboard=req.dashboard,
        totp_secret=user["totp_secret"],
        totp_enabled=user["totp_enabled"],
        must_change_pw=user["must_change_pw"],
        password_hash=None,
        email=email
    )
    await log_security_event("USER_UPDATED", f"User {username} updated by administrator", "admin_api")
    return {"ok": True}


@router.delete("/security/users/{username}", dependencies=[Depends(require_admin)])
async def delete_user_endpoint(username: str, claims: dict = Depends(get_current_user_claims)):
    """Delete a user. Only for admin."""
    if username == claims.get("sub"):
        raise HTTPException(400, "Cannot delete your own active administrator account")
        
    user_to_delete = await get_user(username)
    if not user_to_delete:
        raise HTTPException(404, "User not found")
        
    # Prevent deleting the last admin
    all_users = await list_users()
    admins = [u for u in all_users if u["role"] == "admin"]
    if user_to_delete["role"] == "admin" and len(admins) <= 1:
        raise HTTPException(400, "Cannot delete the last administrator account")
        
    await delete_user(username)
    await log_security_event("USER_DELETED", f"User {username} deleted", "admin_api")
    return {"ok": True}


@router.post("/security/users/{username}/change-password", dependencies=[Depends(require_admin)])
async def admin_change_user_password(username: str, req: UserChangePasswordRequest):
    """Change another user's password. Only for admin."""
    user = await get_user(username)
    if not user:
        raise HTTPException(404, "User not found")
        
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
        email=user.get("email")
    )
    await log_security_event("USER_PASSWORD_CHANGED", f"Password changed for user {username} by administrator", "admin_api")
    return {"ok": True}


@router.post("/security/users/{username}/reset-mfa", dependencies=[Depends(require_admin)])
async def admin_reset_user_mfa(username: str):
    """Clear another user's MFA enrolment. Admin only.

    The self-service `/security/totp/disable` requires the user to be signed
    in, which is precisely what they cannot do once the authenticator is lost.
    Without this, a mislaid phone locks an account out permanently and the only
    remaining route is shell access to the host.

    Clearing the secret as well as the flag is deliberate: leaving a stale
    secret behind means re-enrolment silently reuses a code sequence the old
    device can still generate.
    """
    user = await get_user(username)
    if not user:
        raise HTTPException(404, "User not found")

    if not user["totp_enabled"] and not user["totp_secret"]:
        return {"ok": True, "changed": False, "message": f"{username} has no MFA enrolment."}

    await update_user_profile(
        username,
        role=user["role"],
        dashboard=user["dashboard"],
        totp_secret=None,
        totp_enabled=0,
        must_change_pw=user["must_change_pw"],
        email=user.get("email"),
    )
    await log_security_event(
        "TOTP_RESET_BY_ADMIN",
        f"MFA enrolment cleared for user {username} by administrator",
        "admin_api",
    )
    return {"ok": True, "changed": True, "message": f"MFA cleared for {username}. They can re-enrol at next sign-in."}


# ── Self-service account endpoints moved out ─────────────────────────────
# change-password (own), totp/setup, totp/verify and totp/disable now live in
# src/routes/api_account.py. They must work for ANY authenticated principal,
# but this router is mounted ADMIN_ONLY — and carving per-route exceptions out
# of a tier is the kind of list that drifts. The file boundary is the policy
# boundary instead: api_account.py is mounted SELF_SERVICE and may only hold
# endpoints that act on the caller's own account (target derived from
# claims["sub"], never from a path parameter).
#
# Paths are unchanged, so no frontend or API consumer is affected.
