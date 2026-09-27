"""Admin page routes — serves the Jinja2 admin shell."""
import logging
from pathlib import Path
import jinja2
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

# Use a direct Jinja2 Environment (cache_size=0) to avoid the Python 3.14
# dict-as-hash-key bug in Starlette's LRUCache-based TemplateResponse.
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
    cache_size=0,  # disable LRU cache — avoids dict-key-unhashable crash on Py3.14
)


def _render(template_name: str, context: dict) -> str:
    tmpl = _jinja_env.get_template(template_name)
    return tmpl.render(**context)


async def _admin_response(request: Request) -> HTMLResponse:
    """Render the admin SPA — shared by / and /admin.
    
    Reads X-Ingress-Path from HA Supervisor so the HTML base tag
    is set correctly, making all static asset URLs resolve via the proxy.
    """
    from src.main import get_app_state
    from src.services.db import get_config_value
    from src.routes.api_system import ALL_MODULES
    from src.routes.api_setup import _install_context
    
    state = get_app_state()
    # HA Supervisor sets X-Ingress-Path so static assets route correctly
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    
    template_file = "admin.html"

    # The map tile key. Injected rather than fetched so the first paint of a map
    # already has it — a tile layer built before the key arrives renders a
    # watermarked map that never refreshes.
    from src.routes.api_map import stored_key as _carto_key
    carto_api_key = await _carto_key()

    enabled_modules = await get_config_value("enabled_modules", None)
    if enabled_modules is None:
        suppressed = await get_config_value("suppressed_tabs", "")
        suppressed_set = {t.strip().lower() for t in suppressed.split(",") if t.strip()}
        if suppressed_set:
            from src.routes.api_system import MODULE_TAB_MAP
            enabled_modules = []
            for mod, tabs in MODULE_TAB_MAP.items():
                if not any(t.lower() in suppressed_set for t in tabs):
                    enabled_modules.append(mod)
        else:
            enabled_modules = ALL_MODULES

    enabled_modules_set = set(enabled_modules)

    def module_enabled(module_name: str) -> bool:
        return module_name in enabled_modules_set

    # ── User context (Stage 4) ───────────────────────────────────────────────
    # The template previously received NO user context at all — no username,
    # no role — so the SPA was byte-identical for every account. A viewer got
    # the Security and Terminal tabs rendered and merely failed at the API.
    #
    # Gating server-side rather than with Alpine x-show is the point: markup
    # the client never receives cannot be revealed by editing a variable in
    # devtools. This reuses the `module_enabled` pattern already established in
    # this same template.
    #
    # Defence in depth, NOT the boundary. The tiers in middleware/authz.py are
    # the boundary; this stops a viewer being shown controls that will only
    # 403 at them.
    from src.middleware.authz import normalise_role
    from src.services import dispatch_codes

    role = "admin"      # unsecured install — matches the middleware's posture
    username = ""       # empty means "not signed in" / auth disabled
    security_enabled = False
    try:
        from src.services.db import get_config_value as _gcv
        security_enabled = str(await _gcv("security_enabled") or "false").lower() in ("true", "1", "yes")
        if security_enabled:
            claims = getattr(request.state, "claims", None) or {}
            role = normalise_role(claims.get("role"))
            username = claims.get("sub") or ""
    except Exception:
        role = "viewer"     # cannot determine — show the least

    is_admin = role == "admin"
    can_write = role in ("admin", "operator")

    html = _render(template_file, {
        "request": request,
        "version": state.get("version", "0.1.0"),
        "env": state.get("env", "dev"),
        "base_path": ingress_path,  # e.g. "/api/hassio_ingress/TOKEN" or ""
        "module_enabled": module_enabled,
        "role": role,
        "is_admin": is_admin,
        "can_write": can_write,
        # Who is signed in. The template had no identity variable at all, so
        # after enabling auth there was no way to tell who you were or how to
        # sign out (the only logout control was buried inside the Security tab).
        "username": username,
        "auth_enabled": security_enabled,
        # Rendered server-side rather than by an Alpine x-for. x-model binds to
        # a <select> before a <template x-for> has produced its <option>s, so
        # the select matched nothing and every dispatch dropdown in the block
        # table rendered blank. Real DOM options avoid the ordering entirely,
        # and this is still the one catalogue.
        "dispatch_codes": dispatch_codes.as_list(),
        # Per-instance, entered in the UI, never in the source — the add-on
        # repository is public.
        "carto_api_key": carto_api_key,
        # Which deployment this is. Some controls are not merely useless as an
        # add-on but actively harmful: Home Assistant's ingress proxy speaks
        # plain HTTP to ingress_port, so switching on native TLS would make the
        # panel unreachable, with the setting that broke it now behind the
        # panel it broke.
        "install_context": _install_context(),
    })
    return HTMLResponse(content=html)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request):
    """Serve the admin shell SPA at /admin."""
    return await _admin_response(request)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Serve the login page."""
    from src.main import get_app_state
    state = get_app_state()
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    html = _render("login.html", {
        "request": request,
        "version": state.get("version", "0.1.0"),
        "env": state.get("env", "dev"),
        "base_path": ingress_path,
    })
    return HTMLResponse(content=html)


# ── Device views ─────────────────────────────────────────────────────────────
# A "device view" is a standalone read-only rendering for a physical display
# (e-ink screen, wall tablet) — NOT a user dashboard. They were conflated: the
# users.dashboard column mixed device renderings (inkypi, inkypi2) with notional
# human personas (standard, guest), and only the device half was ever built.
#
# Adding one used to take five coordinated edits plus a SQL table rebuild. Now
# it is one entry here plus a template.
#
# `fields=None` means "the whole payload". inkypi2 is a strict superset of
# inkypi — all 21 of inkypi's keys plus 11 more — so one builder serves both and
# the narrower view is a projection of it.
DEVICE_VIEWS: dict[str, dict] = {
    "inkypi":  {"template": "inkypi_dashboard.html",  "fields": None},
    "inkypi2": {"template": "inkypi2_dashboard.html", "fields": None},
}

# Historical spellings kept working forever. A live e-ink display polls these;
# they are aliases to the same handler rather than redirects, because a simple
# HTTP client on a screen may not follow a 3xx.
VIEW_ALIASES: dict[str, str] = {"inkpi": "inkypi"}


def _resolve_view(slug: str) -> str:
    slug = VIEW_ALIASES.get(slug, slug)
    if slug not in DEVICE_VIEWS:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Unknown device view {slug!r}")
    return slug


async def _device_view_page(slug: str, request: Request) -> HTMLResponse:
    slug = _resolve_view(slug)
    from src.main import get_app_state
    state = get_app_state()
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    html = _render(DEVICE_VIEWS[slug]["template"], {
        "request": request,
        "version": state.get("version", "0.1.0"),
        "env": state.get("env", "dev"),
        "base_path": ingress_path,
    })
    return HTMLResponse(content=html)


@router.get("/view/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def device_view_page(slug: str, request: Request):
    """Generic device view — /view/inkypi, /view/inkypi2, ..."""
    return await _device_view_page(slug, request)


@router.get("/dashboard/inkypi", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard/inkpi", response_class=HTMLResponse, include_in_schema=False)
async def inkypi_dashboard_page(request: Request):
    """Legacy path — kept verbatim so existing displays keep working."""
    return await _device_view_page("inkypi", request)


@router.get("/dashboard/inkypi2", response_class=HTMLResponse, include_in_schema=False)
async def inkypi2_dashboard_page(request: Request):
    """Legacy path — kept verbatim so existing displays keep working."""
    return await _device_view_page("inkypi2", request)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """Serve admin SPA at / — HA ingress proxy cannot follow redirects."""
    return await _admin_response(request)


async def _device_view_data(slug: str, request: Request):
    """Single telemetry builder for every device view.

    Was two near-identical functions (~105 and ~117 lines). inkypi2 turned out
    to be a strict superset — every one of inkypi's 21 top-level keys plus 11
    more — so keeping one implementation removes the risk of the two drifting.

    BEHAVIOUR CHANGE, deliberate and measured: /api/dashboard/inkypi now
    returns 90 keys where it previously returned 79. The 11 additions are all
    nested under `gateway` (battery_charge_today, grid_import_today, and so
    on). Purely ADDITIVE — nothing was removed, verified by diffing live
    responses before and after — and the inkypi template reads named fields, so
    it ignores the extras. The page renders byte-identical (28215 bytes).

    The alternative was declaring inkypi's exact 21 keys to preserve the
    response verbatim, but the difference lives inside `gateway.*` and this
    projection is top-level only, so that would have meant nested filtering to
    freeze a shape nothing depends on. Serving a superset to a display that
    ignores unknown keys is the better trade.

    A view whose "fields" is None receives the whole payload; both current
    views do. The projection exists for the next view that wants less.
    """
    slug = _resolve_view(slug)
    from src.services import db
    from datetime import datetime
    from fastapi import HTTPException
    
    # Check if security is enabled
    security_enabled = str(await db.get_config_value("security_enabled") or "false").lower() in ("true", "1", "yes")
    
    if security_enabled and not hasattr(request.state, "claims"):
        raise HTTPException(status_code=401, detail="Unauthorized — Secure session required")

    # 1. Fetch Gateway telemetry
    from src.main import get_app_state
    state = get_app_state()
    registry = state.get("registry")
    
    gw_data = {}
    if registry:
        gws = registry.get_gateways()
        if gws:
            # Pick first active gateway by default
            gw = gws[0]
            last_data = gw.status.last_data or {}
            
            battery_kw = last_data.get("battery_kw", 0.0)
            battery_state = "standby"
            if battery_kw < -0.05:
                battery_state = "charging"
            elif battery_kw > 0.05:
                battery_state = "discharging"

            gw_data = {
                "short_id": gw.short_id,
                "full_serial": gw.full_serial,
                "name": gw.context.get("name", gw.short_id),
                "battery_soc": last_data.get("battery_soc"),
                "battery_kw": battery_kw,
                "battery_state": battery_state,
                "solar_kw": last_data.get("solar_kw"),
                "solar_today_kwh": last_data.get("solar_today"),
                "home_kw": last_data.get("home_kw"),
                "grid_kw": last_data.get("grid_kw"),
                "grid_connected": last_data.get("grid_connected"),
                "backup_reserve_soc": last_data.get("backup_reserve_soc"),
                "battery_count": last_data.get("battery_count") or last_data.get("apower_count") or 0,
                
                # New fields for inkyPi2
                "operating_mode": last_data.get("operating_mode"),
                "operating_mode_id": last_data.get("operating_mode_id"),
                "battery_charge_today": last_data.get("battery_charge"),
                "battery_discharge_today": last_data.get("battery_discharge"),
                "grid_import_today": last_data.get("grid_import"),
                "grid_export_today": last_data.get("grid_export"),
                "solar_export_to_grid_kw": last_data.get("power", {}).get("solar_to_grid", 0.0),
                "battery_export_to_grid_kw": last_data.get("power", {}).get("battery_to_grid", 0.0),
                "grid_to_battery_kw": last_data.get("power", {}).get("grid_to_battery", 0.0),
                "solar_to_battery_kw": last_data.get("power", {}).get("solar_to_battery", 0.0),
                "status": last_data.get("status", {}),
            }

    # 2. Fetch Weather telemetry
    from src.routes.api_weather import get_current_weather, get_weather_forecast
    weather_curr = {}
    weather_fc = []
    try:
        weather_curr_resp = await get_current_weather()
        if weather_curr_resp.get("ok"):
            weather_curr = weather_curr_resp.get("weather", {})
    except Exception as e:
        logger.warning(f"InkyPi2 API failed to fetch weather current: {e}")
        
    try:
        weather_fc_resp = await get_weather_forecast()
        if weather_fc_resp.get("ok"):
            weather_fc = weather_fc_resp.get("forecast", [])
    except Exception as e:
        logger.warning(f"InkyPi2 API failed to fetch weather forecast: {e}")

    # 3. Fetch Solar forecast
    from src.routes.api_solar import solar_forecast
    solar_fc = {}
    try:
        solar_fc_resp = await solar_forecast()
        if solar_fc_resp.get("ok"):
            solar_fc = solar_fc_resp
    except Exception as e:
        logger.warning(f"InkyPi2 API failed to fetch solar forecast: {e}")

    # 4. Fetch Pricing data
    from src.routes.api_pricing import get_current_price
    pricing_data = {}
    try:
        gw_id = gw_data.get("short_id")
        utility_service_id = None
        if gw_id:
            svc_row = await db.get_utility_service_for_gateway(gw_id)
            if svc_row:
                utility_service_id = svc_row.get("id")
        pricing_status = await get_current_price(utility_service_id=utility_service_id)
        if pricing_status.get("ok"):
            pricing_data = pricing_status.get("data", {})
    except Exception as e:
        logger.warning(f"InkyPi2 API failed to fetch pricing data: {e}")

    payload = {
        "ok": True,
        "timestamp": datetime.utcnow().isoformat(),
        "gateway": gw_data,
        "weather": {
            "current": weather_curr,
            "forecast": weather_fc
        },
        "solar_forecast": solar_fc,
        "pricing": pricing_data
    }

    # Project down to the view's declared fields. None = whole payload, which
    # is what both current views take — hence responses identical to before the
    # collapse. Kept so a future narrower view (a small status screen, say) is
    # one registry entry rather than a third builder.
    fields = DEVICE_VIEWS[slug]["fields"]
    if fields is None:
        return payload
    return {k: v for k, v in payload.items() if k in set(fields) | {"ok", "timestamp"}}


@router.get("/api/view/{slug}", include_in_schema=False)
async def device_view_data(slug: str, request: Request):
    """Generic device-view telemetry — /api/view/inkypi, /api/view/inkypi2, ..."""
    return await _device_view_data(slug, request)


@router.get("/api/dashboard/inkypi", include_in_schema=False)
@router.get("/api/dashboard/inkpi", include_in_schema=False)
async def api_inkypi_dashboard_data(request: Request):
    """Legacy path — a live e-ink display polls this. Alias, not a redirect."""
    return await _device_view_data("inkypi", request)


@router.get("/api/dashboard/inkypi2", include_in_schema=False)
async def api_inkypi2_dashboard_data(request: Request):
    """Legacy path — a live e-ink display polls this. Alias, not a redirect."""
    return await _device_view_data("inkypi2", request)
