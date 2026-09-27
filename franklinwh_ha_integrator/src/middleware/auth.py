"""
Tiered authentication middleware for the admin UI and API.

Enforces:
  1. Greenfield / Default-Unsecured bypass: If security_enabled=False in DB/config,
     auth is bypassed (or falls back to legacy HTTP Basic Auth if configured).
  2. mTLS client cert validation if mtls_enabled=True.
  3. Header-based JWT token (Bearer JWT).
  4. Header-based Long-Lived API Token (Bearer Token).
  5. Cookie-based JWT token (fhai_session) for standard browser navigation.
  6. Exempt routes (/static/*, /api/health, /api/ws/*, /api/security/setup) are always allowed.

Usage:
  app.add_middleware(AdminAuthMiddleware)
"""
import base64
import logging
import os
import hashlib
from typing import Optional
import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.services.db import get_config_value, verify_api_token

logger = logging.getLogger(__name__)

# Routes that bypass auth entirely.
#
# This is the one irreducible hand-maintained list in the auth path, so it is
# pinned by a test (tests/test_auth_exempt_paths.py) — growing it must be a
# deliberate, reviewed act rather than something that accretes.
#
# Entries ending in "/" are treated as directory prefixes; everything else must
# match the path exactly or be followed by "/". Plain str.startswith was wrong:
# "/login" also matched "/loginfoo", so any future route whose name merely
# began with an exempt entry would silently bypass authentication.
AUTH_EXEMPT_PREFIXES = (
    "/static/",
    "/login",
    "/api/health",
    "/api/ws/",
    "/api/security/setup",
    "/api/security/login",
    "/api/security/logout",
    # Home Assistant notification callback. HA cannot easily carry a session,
    # so this stays exempt — but the handler requires the per-request uuid4
    # request_id as a capability token before executing anything.
    "/api/automation/notifications/override",
    # The retired path the UI used to advertise. Same handler, same capability
    # check; exempt for the same reason, and logged so it gets corrected.
    "/api/automation/notifications/callback",
)


def is_auth_exempt(path: str) -> bool:
    """True when `path` is exempt from authentication.

    Directory-style entries (trailing "/") match by prefix. All others match
    the exact path, or the path plus a "/" separator — so "/login" exempts
    "/login" and "/login/callback" but NOT "/loginfoo".
    """
    for pfx in AUTH_EXEMPT_PREFIXES:
        if pfx.endswith("/"):
            if path.startswith(pfx):
                return True
        elif path == pfx or path.startswith(pfx + "/"):
            return True
    return False



def _check_basic_credentials(authorization: Optional[str], username: str, password: str) -> bool:
    """Validate HTTP Basic Auth header against stored credentials."""
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        provided_user, _, provided_pass = decoded.partition(":")
        return provided_user == username and provided_pass == password
    except Exception:
        return False


def _unauthorized_response(message: str = "Unauthorized") -> Response:
    return Response(
        content=message,
        status_code=401,
        headers={
            "Content-Type": "text/plain",
        },
    )


async def _safe_get_config_value(key: str) -> Optional[str]:
    """Helper to safely query DB configuration, returning None if DB path is not initialised."""
    try:
        from src.services.db import _db_path
        if _db_path is None:
            return None
        return await get_config_value(key)
    except Exception:
        return None


async def verify_jwt(token: str) -> Optional[dict]:
    """Verify an incoming JWT token using the stored secret key.

    Fails closed when no secret is stored. This previously fell back to a
    hardcoded placeholder secret committed to this public repo, so any attacker
    could sign their own admin token. Rejecting is correct rather than merely
    inconvenient: if no secret exists, this instance has never issued a token,
    so every presented token is forged.

    Deliberately does NOT generate a secret. Minting one here would make the
    verifier accept the next token it sees; generation belongs on the issuing
    side (api_security.get_or_create_jwt_secret).
    """
    secret_key = await _safe_get_config_value("jwt_secret_key")
    if not secret_key:
        logger.warning(
            "verify_jwt: no jwt_secret_key stored — rejecting token "
            "(no session can legitimately exist yet)"
        )
        return None
    try:
        return jwt.decode(token, secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


SESSION_COOKIE_SECURE = "fhai_session"
SESSION_COOKIE_PLAIN = "fhai_session_http"

# Two names, chosen by transport, because a browser will not let the plain-HTTP
# one replace the Secure one.
#
# RFC 6265bis "Leave Secure Cookies Alone": a non-secure origin may not set a
# cookie whose name matches an existing Secure cookie. So after TLS is switched
# off, the browser keeps the Secure `fhai_session` from the HTTPS era, refuses
# the new plain one, and never sends the old one over http. Login returns 200,
# the next request looks signed-out, and the user loops through the login page
# with no error anywhere — the server is behaving perfectly the whole time.
#
# Giving each transport its own name removes the collision entirely. Both are
# accepted on read, so a session survives the switch in whichever direction it
# already had a cookie for.


def session_cookie_name(is_https: bool) -> str:
    return SESSION_COOKIE_SECURE if is_https else SESSION_COOKIE_PLAIN


def session_cookie_candidates(cookies) -> list[str]:
    """Every session token the client offered, in preference order.

    A list, not a preference. Returning only the Secure one when both are
    present reinstates the bug this exists to fix: after TLS goes off, the stale
    Secure cookie is still in the jar, and picking it means a valid plain-HTTP
    session is never even tried. Try each; accept whichever verifies.
    """
    return [c for c in (cookies.get(SESSION_COOKIE_SECURE),
                        cookies.get(SESSION_COOKIE_PLAIN)) if c]


def read_session_cookie(cookies) -> str | None:
    """First offered token. For callers that verify it themselves."""
    candidates = session_cookie_candidates(cookies)
    return candidates[0] if candidates else None


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces tiered authentication (JWT, tokens, mTLS) on all routes.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always allow exempt paths
        if is_auth_exempt(path):
            return await call_next(request)

        from src.app_state import get_app_state
        state = get_app_state()
        config = state.get("config")

        # 1. Check if security_enabled is active in DB or AppConfig
        db_security_enabled_str = await _safe_get_config_value("security_enabled")
        if db_security_enabled_str is not None:
            security_enabled = str(db_security_enabled_str).lower() in ("true", "1", "yes")
        elif config:
            security_enabled = getattr(config, "security_enabled", False)
        else:
            security_enabled = False

        # If FWH_DISABLE_SECURITY override env is active, force security off
        if os.environ.get("FWH_DISABLE_SECURITY", "").lower() in ("true", "1", "yes"):
            security_enabled = False

        # ── TIER A: Security Core INACTIVE (Fallback/Bypass Mode) ──
        if not security_enabled:
            # Resolve legacy basic auth credentials
            username = ""
            password = ""
            if config:
                username = getattr(config, "admin_username", "") or ""
                password = getattr(config, "admin_password", "") or ""
            if not username:
                username = os.environ.get("ADMIN_USERNAME", "")
            if not password:
                password = os.environ.get("ADMIN_PASSWORD", "")

            # If basic auth credentials are set, enforce legacy basic auth
            if username and password and os.environ.get("ADMIN_AUTH", "1") != "0":
                if not _check_basic_credentials(request.headers.get("authorization"), username, password):
                    return Response(
                        content="Unauthorized — please provide valid admin credentials.",
                        status_code=401,
                        headers={
                            "WWW-Authenticate": 'Basic realm="FranklinWH HA Integrator"',
                            "Content-Type": "text/plain",
                        },
                    )

            # Otherwise, allow bypass (unsecured open mode)
            return await call_next(request)

        # ── TIER B: Security Core ACTIVE (All-or-Nothing Policy) ──
        
        # 1. Verify Mutual TLS (mTLS) if enabled
        db_mtls_enabled_str = await _safe_get_config_value("mtls_enabled")
        if db_mtls_enabled_str is not None:
            mtls_enabled = str(db_mtls_enabled_str).lower() in ("true", "1", "yes")
        elif config:
            mtls_enabled = getattr(config, "mtls_enabled", False)
        else:
            mtls_enabled = False

        if mtls_enabled:
            # Check ASGI client cert (standalone Uvicorn mTLS termination)
            client_cert = None
            if "extensions" in request.scope and "tls" in request.scope["extensions"]:
                client_cert = request.scope["extensions"]["tls"].get("client_cert_peercert")
            
            # Also support reverse proxy headers
            if not client_cert:
                client_cert = request.headers.get("X-Client-Cert") or request.headers.get("X-SSL-Client-Cert")
                
            if not client_cert:
                logger.warning("mTLS validation failed: Client certificate missing")
                return _unauthorized_response("Unauthorized — mTLS client certificate required.")

        # 2. Verify Authorization header or query parameters (JWT or Long-Lived Token)
        token = None
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        else:
            token = request.query_params.get("token") or request.query_params.get("api_key")
            
        if token:
            token = token.strip()
            # (a) Try validating as a Long-Lived API Token
            # Long-Lived tokens are stored as SHA-256 hashes of the raw secret
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            verified_token = await verify_api_token(token_hash)
            if verified_token:
                # Check expiration date if set
                expires_at = verified_token.get("expires_at")
                if expires_at:
                    from datetime import datetime
                    try:
                        if datetime.fromisoformat(expires_at) < datetime.utcnow():
                            return _unauthorized_response("Unauthorized — API Token expired.")
                    except Exception:
                        pass
                # Valid API Token. Its role comes from the token row (schema
                # v56) — this used to be hardcoded to "admin", so a token
                # minted for an e-ink display or a Prometheus scraper could do
                # anything, including delete users. Tokens issued before v56
                # were backfilled to admin so live integrations keep working;
                # new ones default to viewer.
                token_role = (verified_token.get("role") or "viewer") if hasattr(verified_token, "get") else "viewer"
                request.state.user = verified_token.get("name", "api_token")
                request.state.role = token_role
                request.state.claims = {
                    "sub": request.state.user,
                    "role": token_role,
                    "dashboard": "standard",
                    "must_change_pw": 0,
                }
                return await call_next(request)

            # (b) Try validating as a JWT Session Token
            jwt_payload = await verify_jwt(token)
            if jwt_payload:
                request.state.user = jwt_payload.get("sub")
                request.state.role = jwt_payload.get("role") or "viewer"   # fail closed
                request.state.claims = jwt_payload
                return await call_next(request)

            logger.warning("Unauthorized Token attempt from %s", request.client.host if request.client else "unknown")
            return _unauthorized_response("Unauthorized — Invalid token.")

        # 3. Verify Cookie-based JWT session token.
        #
        # Every offered cookie is tried, not just the first. A jar can hold both
        # names — a Secure one left over from when TLS was on, and the plain one
        # issued since — and the stale one must not veto the valid one.
        for jwt_cookie in session_cookie_candidates(request.cookies):
            jwt_payload = await verify_jwt(jwt_cookie)
            if jwt_payload:
                request.state.user = jwt_payload.get("sub")
                request.state.role = jwt_payload.get("role") or "viewer"   # fail closed
                request.state.claims = jwt_payload
                return await call_next(request)

        # 4. If all validations fail, return 401 for APIs or redirect to /login for pages
        logger.warning(
            "Unauthorized access attempt to %s from %s",
            path,
            request.client.host if request.client else "unknown",
        )
        if path.startswith("/api/"):
            return _unauthorized_response("Unauthorized — Secure session required.")
        
        from starlette.responses import RedirectResponse
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        redirect_url = f"{ingress_path}/login" if ingress_path else "/login"
        return RedirectResponse(url=redirect_url, status_code=303)
