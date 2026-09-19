"""Router-level authorization tiers (GH: users/roles Stage 1).

Companion to `middleware/auth.py`, which answers *"is this request
authenticated?"*. This module answers *"is this principal allowed to do this?"*
— a question the codebase previously never asked. Before this, exactly one
authorization branch existed (`role != "admin"`) on 8 of ~310 routes, and the
multi-role helper that looked like the enforcement mechanism had zero call
sites.

Why tiers on routers rather than the obvious alternatives
---------------------------------------------------------
* **Not in the auth middleware.** `BaseHTTPMiddleware` runs *before* routing,
  so `request.scope["route"]` is unpopulated and the only available tool is
  string-matching the path against a hand-maintained list. That is precisely
  the failure mode of GH #33 (a block-list that silently broke whenever
  something new was added), and the existing `AUTH_EXEMPT_PREFIXES` had that
  bug too.

* **Not per-route decorators.** 310 opt-in sites, of which 8 were ever filled
  in. That is the empirical answer to whether opt-in works here.

* **Not method-alone (GET=read, else=write).** Structurally tidy but wrong:
  GET is not safe in this codebase. `GET /api/system/db/tables/users` returns
  bcrypt hashes, and `GET /api/security/status` returns TLS paths and token
  metadata. A method-only rule hands those to a viewer.

So: a tier is declared once per router at mount time, and the method decides
read-vs-write *within* that tier. 19 declarations instead of 310, new routes
inside a router inherit automatically, and `mount()` refuses to mount a router
with no tier — a loud startup failure in development rather than a silent hole
in production.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Optional

from starlette.requests import HTTPConnection
from fastapi import Depends, HTTPException, Request, status

logger = logging.getLogger(__name__)

# Ordered least → most privileged. Membership is inclusive: an admin satisfies
# any requirement, an operator satisfies viewer.
ROLE_ORDER: tuple[str, ...] = ("viewer", "operator", "admin")

# Legacy role values still present in the schema CHECK. Mapped here so
# enforcement is meaningful before the Stage 2 migration collapses them.
# `supervisor` maps to admin deliberately: it was labelled "Limited Admin" and
# never enforced, so those accounts have had full access all along — demoting
# them silently at enforcement time would be a surprise, not a fix.
LEGACY_ROLE_MAP: dict[str, str] = {
    "admin": "admin",
    "supervisor": "admin",
    "control": "operator",
    "operator": "operator",
    "user": "viewer",
    "guest": "viewer",
    "viewer": "viewer",
    "inkypi": "viewer",
    "inkpi": "viewer",
    "inkypi2": "viewer",
}

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Tier(str, Enum):
    """What a router's routes require.

    ADMIN_ONLY   — admin for everything. Config, security, terminal, raw DB.
    OPERATIONAL  — viewer may read, operator may act. Battery/dispatch control.
    TELEMETRY    — viewer may read, admin may mutate. Read-mostly surfaces.
    SELF_SERVICE — any authenticated principal, acting only on themselves.

    SELF_SERVICE exists so "change my own password" and "set up my own MFA" do
    not have to be per-route exceptions carved out of an ADMIN_ONLY router.
    Those handlers derive the target user from the caller's own claims, never
    from a path parameter, so the file boundary IS the policy boundary — an
    exception list cannot drift, because there is no exception list.
    """
    ADMIN_ONLY = "admin_only"
    OPERATIONAL = "operational"
    TELEMETRY = "telemetry"
    SELF_SERVICE = "self_service"


def _required_role(tier: Tier, method: str) -> str:
    is_read = method.upper() in READ_METHODS
    if tier is Tier.ADMIN_ONLY:
        return "admin"
    if tier is Tier.SELF_SERVICE:
        return "viewer"                            # authenticated is enough
    if tier is Tier.OPERATIONAL:
        return "viewer" if is_read else "operator"
    return "viewer" if is_read else "admin"       # TELEMETRY


def normalise_role(raw: Optional[str]) -> str:
    """Map a stored role value onto the enforced set, failing closed."""
    if not raw:
        return "viewer"
    return LEGACY_ROLE_MAP.get(str(raw).strip().lower(), "viewer")


def role_satisfies(actual: Optional[str], required: str) -> bool:
    norm = normalise_role(actual)
    try:
        return ROLE_ORDER.index(norm) >= ROLE_ORDER.index(required)
    except ValueError:
        return False


async def _security_enabled() -> bool:
    """Read the posture at REQUEST time, not import time.

    `src.main.app` is a module-level singleton and router dependencies bind at
    import, so caching this would freeze whatever the value happened to be when
    the module first loaded — and would break the ~758-test suite, which runs
    with FWH_DISABLE_SECURITY set. Mirrors how
    api_security.get_current_user_claims already behaves.
    """
    if os.environ.get("FWH_DISABLE_SECURITY", "").lower() in ("true", "1", "yes"):
        return False
    try:
        from src.services.db import get_config_value
        raw = await get_config_value("security_enabled")
    except Exception:
        # Fail OPEN here, deliberately, and only here: this mirrors the
        # existing middleware, which treats an unreadable posture as
        # "unconfigured install". Failing closed would brick a fresh instance
        # before setup. The authenticated paths below still fail closed.
        return False
    return str(raw).lower() in ("true", "1", "yes")


def require_tier(tier: Tier):
    """Build the dependency enforcing `tier` for one router."""

    async def _dependency(conn: HTTPConnection) -> None:
        # HTTPConnection, not Request: it is the base class of both Request and
        # WebSocket, and FastAPI can inject it for either kind of route.
        #
        # Declared as `request: Request`, this dependency could not be solved on
        # a WebSocket route at all — FastAPI raised
        #   TypeError: _dependency() missing 1 required positional argument
        # and answered the handshake with 500 before any of our own auth ran.
        # That is why the MQTT Explorer never connected: it was rejected by the
        # authorisation layer failing, not by the authorisation layer deciding.
        # getattr, not conn.scope: the tier tests pass a light stub with just
        # the attributes an HTTP request needs, and a missing scope means "not a
        # websocket" rather than an error.
        if getattr(conn, "scope", {}).get("type") == "websocket":
            # WebSocket handshakes never traverse BaseHTTPMiddleware, so there
            # are no claims here to check, and there is no HTTP method to map to
            # a role. Those routes authenticate themselves — see
            # api_mqtt._websocket_authorised, which mirrors these same tiers.
            return

        request = conn
        if not await _security_enabled():
            return                      # unsecured install — matches auth.py

        # Auth-exempt paths must also be AUTHZ-exempt.
        #
        # AUTH_EXEMPT_PREFIXES only governs the middleware. Router-level tiers
        # apply to every route in the router, so /login — which lives in
        # admin.router (TELEMETRY) — was demanding a session in order to render
        # the page you use to get a session. A 401 on /login with no way out.
        #
        # Any path deliberately reachable without credentials must be reachable,
        # full stop; there is no coherent tier for "no principal".
        from src.middleware.auth import is_auth_exempt
        if is_auth_exempt(request.url.path):
            return

        claims = getattr(request.state, "claims", None)
        if not claims:
            # Authentication is the middleware's job; if it let an
            # unauthenticated request through to here on a secured install,
            # refuse rather than assume.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized — authentication required.",
            )

        required = _required_role(tier, request.method)
        actual = claims.get("role")
        if not role_satisfies(actual, required):
            logger.warning(
                "authz: denied %s %s — role %r (normalised %r) < required %r",
                request.method, request.url.path, actual, normalise_role(actual), required,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden — this action requires the '{required}' role.",
            )

    return _dependency


def mount(app, router, tier: Optional[Tier], *, prefix: Optional[str] = None) -> None:
    """Attach `router` to `app` under `tier`.

    Refusing a missing tier is the whole point: it converts "someone forgot to
    protect this router" from an invisible production gap into a startup crash
    that cannot reach a release.
    """
    if tier is None or not isinstance(tier, Tier):
        raise RuntimeError(
            f"Router {getattr(router, 'prefix', router)!r} was mounted without an "
            f"authorization Tier. Every router must declare one — pick the "
            f"closest of {[t.name for t in Tier]} and add it in src/main.py."
        )
    kwargs = {"dependencies": [Depends(require_tier(tier))]}
    if prefix:
        kwargs["prefix"] = prefix
    app.include_router(router, **kwargs)
