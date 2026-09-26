"""
api_solar.py — REST endpoints for the Solar Forecast Service.

Endpoints:
  GET  /api/solar/config              — read solar forecast config
  PUT  /api/solar/config              — save solar forecast config
  GET  /api/solar/status              — active source + cache status
  POST /api/solar/test                — test active source connection
  GET  /api/solar/forecast            — 48-slot solar profile (normalised)
  POST /api/solar/enphase/fetch-token — fetch/renew JWT from Enlighten cloud
  GET  /api/solar/enphase/live        — live production watts from local Envoy

  -- Gateway Solar Sources (Ph-3) --
  GET    /api/solar/gateways/{gw_id}/sources           — list solar sources for gateway
  POST   /api/solar/gateways/{gw_id}/sources           — add a solar source
  PUT    /api/solar/gateways/{gw_id}/sources/{src_id}  — update a solar source
  DELETE /api/solar/gateways/{gw_id}/sources/{src_id}  — delete a manual solar source

  -- Ph-SL: Solar ↔ Utility Service link --
  GET   /api/solar/sources                    — all sources across all gateways + utility link
  PATCH /api/solar/sources/{src_id}/link      — assign/unassign utility_service_id
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

from src.services import db
from src.services.solar import SolarForecastManager
from src.app_state import get_app_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/solar", tags=["solar"])

# ── Envoy log de-spam state ────────────────────────────────────────────────────
# When the local Envoy is unreachable, every UI poll prints a WARNING. Cap that
# to one WARN per host per window; track consecutive failures so we can log a
# single recovery message when it comes back.
_ENVOY_LOG_WINDOW_S = 600  # 10 min
_envoy_failure_state: dict[str, dict] = {}  # host → {count, first_ts, last_warn_ts}


# ── Pydantic models ────────────────────────────────────────────────────────────

class SolarConfigUpdate(BaseModel):
    enabled:                  Optional[bool]  = None
    # Explicit source — no 'auto' fallback exposed in UI
    source:                   Optional[str]   = Field(None, pattern="^(ha_entities|forecast_solar|solcast|openmeteo)$")
    # HA entity source
    ha_solar_actual_entity:   Optional[str]   = None
    ha_solar_forecast_entity: Optional[str]   = None
    ha_solar_curtail_entity:  Optional[str]   = None
    # Enphase control mode (Solar Setup tab)
    enphase_mode:               Optional[str]   = Field(None, pattern="^(none|ha_entity|direct_envoy)$")
    enphase_token:              Optional[str]   = None   # JWT for firmware v7+
    enphase_token_expiry:       Optional[str]   = None   # ISO datetime — display only
    enphase_serial:             Optional[str]   = None   # Envoy serial number (needed for JWT fetch)
    # Enlighten cloud credentials — used by fetch-token route only; stored for auto-renewal
    enphase_enlighten_user:     Optional[str]   = None
    enphase_enlighten_password: Optional[str]   = None
    # Enphase DPEL direct settings (existing direct_envoy path)
    enphase_enabled:            Optional[int]   = None
    enphase_host:               Optional[str]   = None
    enphase_user:               Optional[str]   = None
    enphase_password:           Optional[str]   = None
    enphase_slew_rate:          Optional[int]   = None
    enphase_export_limit_w:     Optional[int]   = None
    # Site location (shared by all built-in providers)
    lat:                        Optional[float] = Field(None, ge=-90, le=90)
    lng:                        Optional[float] = Field(None, ge=-180, le=180)
    # Installation specs (shared)
    azimuth:                    Optional[float] = Field(None, ge=0, le=360)
    tilt:                       Optional[float] = Field(None, ge=0, le=90)
    kwp:                        Optional[float] = Field(None, ge=0)
    # Provider credentials
    forecast_solar_api_key:         Optional[str]   = None
    forecast_solar_rate_limit_mins: Optional[int]   = Field(None, ge=1)
    solcast_api_key:            Optional[str]   = None
    solcast_site_id:            Optional[str]   = None
    # SOC projection
    home_load_assumption_kw:    Optional[float] = Field(None, ge=0)




# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_ha_creds() -> dict:
    """Pull HA host + token from the global config table (same source as automation routes)."""
    ha_host  = await db.get_config_value("ha_host",  "") or os.environ.get("HA_HOST",  "")
    ha_token = await db.get_config_value("ha_token", "") or os.environ.get("HA_TOKEN", "") \
               or os.environ.get("SUPERVISOR_TOKEN", "")
    return {"ha_url": ha_host, "ha_token": ha_token}


def _build_manager(cfg: dict, ha_creds: dict) -> SolarForecastManager:
    """Construct SolarForecastManager from DB row + HA credentials."""
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    return SolarForecastManager(
        config={
            "enabled": cfg.get("enabled", False),
            "source":  cfg.get("source", "auto"),
            "ha": {
                "ha_url":                ha_creds.get("ha_url", ""),
                "ha_token":              ha_creds.get("ha_token", ""),
                "solar_actual_entity":   cfg.get("ha_solar_actual_entity") or "",
                "solar_forecast_entity": cfg.get("ha_solar_forecast_entity") or "",
            },
            "installation": {
                "lat":     cfg.get("lat"),
                "lng":     cfg.get("lng"),
                "azimuth": cfg.get("azimuth", 180.0),
                "tilt":    cfg.get("tilt", 22.5),
                "kwp":     cfg.get("kwp", 5.0),
            },
            "providers": {
                "forecast_solar": {
                    "api_key":           cfg.get("forecast_solar_api_key") or "",
                    "sync_interval_mins": 60,
                },
                "solcast": {
                    "api_key":   cfg.get("solcast_api_key") or "",
                    "site_id":   cfg.get("solcast_site_id") or "",
                    "sync_interval_mins": 60,
                    "_data_dir": str(data_dir),   # for call-log persistence
                },
                "openmeteo": {
                    "sync_interval_mins": 60,
                },
            },
            "home_load_assumption_kw": cfg.get("home_load_assumption_kw", 0.5),
            "forecast_solar_rate_limit_mins": cfg.get("forecast_solar_rate_limit_mins", 30),
        },
        data_dir=data_dir,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_solar_config():
    """Return current solar forecast configuration."""
    cfg = await db.get_solar_forecast_config()
    safe = dict(cfg)
    # Redact secrets
    if safe.get("solcast_api_key"):
        safe["solcast_api_key"] = "***"
    if safe.get("forecast_solar_api_key"):
        safe["forecast_solar_api_key"] = "***"
    return {"ok": True, "config": safe}


@router.put("/config")
async def update_solar_config(body: SolarConfigUpdate):
    """Persist solar forecast configuration."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        await db.upsert_solar_forecast_config(**updates)
        logger.info(f"☀️  Solar forecast config saved: {list(updates.keys())}")
    cfg = await db.get_solar_forecast_config()
    return {"ok": True, "config": cfg, "updated": len(updates)}


@router.get("/status")
async def solar_status():
    """Return active source, last sync time, availability flags, and Solcast call count."""
    cfg = await db.get_solar_forecast_config()
    ha_creds = await _get_ha_creds()
    mgr = _build_manager(cfg, ha_creds)
    status = mgr.get_status()
    # Augment with Solcast call count if Solcast is configured
    if cfg.get("solcast_api_key") and mgr._provider:
        try:
            from src.services.solar.solcast import SolcastProvider
            if isinstance(mgr._provider, SolcastProvider):
                status["solcast_calls_today"] = mgr._provider.get_calls_today()
                status["solcast_calls_limit"] = SolcastProvider.DAILY_CALL_LIMIT
        except Exception:
            pass
    return {"ok": True, **status}


@router.post("/test")
async def solar_test():
    """Test the active solar forecast source connection."""
    cfg = await db.get_solar_forecast_config()
    ha_creds = await _get_ha_creds()
    mgr = _build_manager(cfg, ha_creds)
    success = mgr.test_connection()
    status = mgr.get_status()
    return {
        "ok":      success,
        "source":  status.get("active_source"),
        "message": "Connection successful" if success else "Connection failed — check config",
    }


@router.get("/forecast")
async def solar_forecast(refresh: bool = Query(False)):
    """
    Return a 48-slot (30-min) solar production forecast.
    Used by the Amber forecast engine to project SOC trajectory.
    """
    cfg = await db.get_solar_forecast_config()
    ha_creds = await _get_ha_creds()
    mgr = _build_manager(cfg, ha_creds)
    slots = mgr.get_forecast(force_refresh=refresh)
    status = mgr.get_status()
    return {
        "ok":        True,
        "enabled":   cfg.get("enabled", False),
        "source":    status.get("active_source"),
        "last_sync": status.get("last_sync"),
        "slots":     slots,
    }


@router.get("/actuals")
async def solar_actuals(
    short_id: str = Query(..., description="Gateway short_id"),
    date: str     = Query(None, description="Date YYYY-MM-DD (default: today)"),
    bucket_mins: int = Query(30, ge=5, le=60, description="Resampling bucket size in minutes"),
):
    """
    Fetch actual solar PV production from FranklinWH Cloud API (5-min data),
    resampled into fixed-size buckets suitable for overlaying on the forecast chart.

    Uses the same data source as the Reporting tab:
      powerSolarHomeArray + powerSolarGirdArray + powerSolarFhpArray = total solar kW
      Each 5-min sample is in kW; energy per slot = kW / 12 (kWh per 5-min period).

    Returns: { date, source, buckets: [{timestamp, pv_kw, wh, samples}], total_kwh }
    """
    from datetime import datetime, timezone, timedelta
    from fastapi import HTTPException

    state = get_app_state()
    registry = state.get("registry")
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")

    gw = registry.get_gateway(short_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {short_id} not found")

    target_date = date or datetime.now().strftime("%Y-%m-%d")

    try:
        client = await gw._get_or_create_client()
        # get_power_by_day is the 5-min day view — same as Reporting tab type=1 / '1_5m' mode
        raw = await client.get_power_by_day(dayTime=target_date)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FranklinWH Cloud API error: {exc}")

    if not raw:
        raise HTTPException(status_code=502, detail="Empty response from Cloud API")

    # Extract 5-min time indices and solar power arrays
    # Solar total = to home + to grid + to battery
    time_arr   = raw.get("deviceTimeArray", [])      # ["HH:MM", ...] 288 entries for 5-min day
    solar_home = raw.get("powerSolarHomeArray", [])   # kW
    solar_grid = raw.get("powerSolarGirdArray", [])   # kW  (typo 'Gird' is FranklinWH API)
    solar_bat  = raw.get("powerSolarFhpArray", [])    # kW

    if not time_arr:
        return {
            "date": target_date,
            "source": "franklinwh_cloud",
            "buckets": [],
            "total_kwh": 0,
            "note": "No 5-min data available for this date",
        }

    # Parse into (minute_of_day, pv_kw) pairs
    # deviceTimeArray format: 'YYYY-MM-DD HH:MM:SS' or 'HH:MM' depending on firmware
    points: list[tuple[int, float]] = []
    for i, t in enumerate(time_arr):
        try:
            ts = str(t).strip()
            if " " in ts:
                # Full datetime: '2026-04-30 06:00:00' — extract the time part
                time_part = ts.split(" ")[1]          # 'HH:MM:SS'
                h, m = time_part.split(":")[:2]
            elif ":" in ts:
                # Plain 'HH:MM'
                h, m = ts.split(":")[:2]
            else:
                # Fractional hour integer
                minute = int(float(ts) * 60)
                sh = float(solar_home[i]) if i < len(solar_home) else 0.0
                sg = float(solar_grid[i]) if i < len(solar_grid) else 0.0
                sb = float(solar_bat[i])  if i < len(solar_bat)  else 0.0
                pv_kw = max(0.0, sh + sg + sb)
                points.append((minute, pv_kw))
                continue
            minute = int(h) * 60 + int(m)
            sh = float(solar_home[i]) if i < len(solar_home) else 0.0
            sg = float(solar_grid[i]) if i < len(solar_grid) else 0.0
            sb = float(solar_bat[i])  if i < len(solar_bat)  else 0.0
            pv_kw = max(0.0, sh + sg + sb)
            points.append((minute, pv_kw))
        except (ValueError, IndexError):
            continue

    # Resample into fixed buckets
    bucket_m = bucket_mins
    bucket_data: dict[int, list[float]] = {}
    for minute, kw in points:
        bucket = (minute // bucket_m) * bucket_m
        bucket_data.setdefault(bucket, []).append(kw)

    # Build date object in local time for timestamp construction
    date_obj = datetime.strptime(target_date, "%Y-%m-%d")

    buckets = []
    total_wh = 0.0
    for bkt_minute in sorted(bucket_data.keys()):
        vals = bucket_data[bkt_minute]
        avg_kw = sum(vals) / len(vals)
        # Energy per bucket: kW × (bucket_mins / 60) = kWh → × 1000 = Wh
        wh = avg_kw * (bucket_m / 60) * 1000
        total_wh += wh
        # Build local datetime string (naive — no tz, avoids browser confusion)
        ts = date_obj.replace(hour=bkt_minute // 60, minute=bkt_minute % 60, second=0)
        buckets.append({
            "timestamp": ts.isoformat(),
            "pv_kw":     round(avg_kw, 4),
            "wh":        round(wh, 1),
            "samples":   len(vals),
        })

    return {
        "date":       target_date,
        "source":     "franklinwh_cloud",
        "buckets":    buckets,
        "total_kwh":  round(total_wh / 1000, 3),
        "raw_points": len(points),
    }

class EnphaseTestRequest(BaseModel):
    host: str
    username: str = "installer"
    password: str
    token: Optional[str] = None   # JWT for firmware v7+


class EnphaseTokenRequest(BaseModel):
    """Body for POST /api/solar/enphase/fetch-token."""
    host:               str            # Local Envoy IP or hostname
    serial:             str            # Envoy serial number (required for JWT scoping)
    enlighten_user:     str            # Enlighten cloud email
    enlighten_password: str            # Enlighten cloud password


@router.post("/enphase/fetch-token")
async def enphase_fetch_token(body: EnphaseTokenRequest):
    """
    Fetch or renew an Enphase Envoy JWT token via the 3-step Enlighten cloud flow.

    Step 1: POST https://entrez.enphaseenergy.com/login → session token (Installer)
            OR standard owner login/tokens fallback directly from Enphase Cloud
    Step 2: GET  https://{host}/auth/get_jwt            → local JWT (Installer)
    Step 3: GET  https://{host}/auth/check_jwt          → validate + read expiry

    On success, saves token + expiry to solar_forecast_config and returns account type.
    """
    import httpx
    import json as _json

    ENTREZ_LOGIN_URL = "https://entrez.enphaseenergy.com/login"

    logger.info(f"☀️  Initiating Enphase JWT fetch for serial {body.serial} (Envoy host: {body.host}). User: {body.enlighten_user}")

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            # ── Step 1: Enlighten cloud login → manager token ──────────────
            logger.info(f"Step 1: Attempting Entrez cloud login for installer credentials: {body.enlighten_user}")
            login_resp = await client.post(
                ENTREZ_LOGIN_URL,
                data={
                    "username": body.enlighten_user,
                    "password": body.enlighten_password,
                    "codeChallenge": "1234",
                    "redirectUri": "",
                    "client": "iq-gateway",
                    "getTokenResponse": "true",
                    "serial_num": body.serial,
                    "loginToken": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            logger.info(f"Entrez login response code: {login_resp.status_code}")

            if login_resp.status_code not in (200, 302):
                logger.warning(f"Entrez login failed with status code {login_resp.status_code}. Response: {login_resp.text[:200]}")
                return {
                    "ok": False,
                    "error": f"Enlighten login failed (HTTP {login_resp.status_code}) — check email/password",
                }

            # Determine account type from response body
            login_body = login_resp.text
            account_type = "standard"
            if "installer" in login_body.lower() or "manager_token" in login_body.lower():
                account_type = "installer"
            logger.info(f"Detected account type: {account_type}")

            # Extract manager_token from JSON body if present
            manager_token = ""
            try:
                login_json = login_resp.json()
                manager_token = login_json.get("manager_token", "")
            except Exception as e:
                logger.info(f"Entrez response is not JSON or manager_token is not present: {e}")

            jwt_token = ""
            if manager_token:
                logger.info(f"Step 2: manager_token found. Fetching JWT locally from Envoy at {body.host}...")
                jwt_url = f"https://{body.host}/auth/get_jwt"
                jwt_params = {"serial_num": body.serial, "manager_token": manager_token}
                jwt_resp = await client.get(jwt_url, params=jwt_params)
                logger.info(f"Envoy local get_jwt response code: {jwt_resp.status_code}")
                if jwt_resp.status_code != 200:
                    err_msg = f"JWT fetch from Envoy failed (HTTP {jwt_resp.status_code})"
                    logger.warning(err_msg)
                    return {"ok": False, "error": err_msg}
                jwt_token = jwt_resp.text.strip()
            else:
                # Fallback: standard owner account cloud token fetch flow
                logger.info("manager_token not found in Entrez response; attempting Standard Owner account cloud login fallback...")
                
                # Fetch session_id from enlighten login.json
                login_url = "https://enlighten.enphaseenergy.com/login/login.json"
                logger.info(f"Fallback Step 1.1: POST to {login_url} for user {body.enlighten_user}")
                enlighten_resp = await client.post(
                    login_url,
                    data={
                        "user[email]": body.enlighten_user,
                        "user[password]": body.enlighten_password,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                logger.info(f"Enlighten login.json response code: {enlighten_resp.status_code}")
                if enlighten_resp.status_code != 200:
                    err_msg = f"Enlighten cloud login failed (HTTP {enlighten_resp.status_code})"
                    logger.warning(err_msg)
                    return {"ok": False, "error": err_msg}
                
                try:
                    enlighten_json = enlighten_resp.json()
                except Exception as e:
                    err_msg = f"Failed to parse Enlighten login JSON: {e}. Response: {enlighten_resp.text[:200]}"
                    logger.warning(err_msg)
                    return {"ok": False, "error": err_msg}
                
                session_id = enlighten_json.get("session_id")
                if not session_id:
                    err_msg = enlighten_json.get("message") or "Enlighten login failed - no session_id returned"
                    logger.warning(f"Enlighten login failed: {err_msg}")
                    return {"ok": False, "error": err_msg}
                
                # Retrieve JWT from entrez tokens
                tokens_url = "https://entrez.enphaseenergy.com/tokens"
                logger.info(f"Fallback Step 1.2: POST to {tokens_url} to retrieve JWT token...")
                tokens_resp = await client.post(
                    tokens_url,
                    json={
                        "session_id": session_id,
                        "serial_num": body.serial,
                        "username": body.enlighten_user,
                    }
                )
                logger.info(f"Entrez tokens response code: {tokens_resp.status_code}")
                if tokens_resp.status_code != 200:
                    err_msg = f"Entrez tokens request failed (HTTP {tokens_resp.status_code}). Response: {tokens_resp.text[:200]}"
                    logger.warning(err_msg)
                    return {"ok": False, "error": err_msg}
                
                jwt_token = tokens_resp.text.strip()
                logger.info("Successfully fetched JWT from Entrez cloud tokens endpoint.")

            if not jwt_token or len(jwt_token) < 20:
                err_msg = "Empty or invalid JWT token received — check credentials and gateway serial number"
                logger.warning(err_msg)
                return {"ok": False, "error": err_msg}

            # ── Step 3: Validate JWT on Envoy, read expiry ─────────────────
            logger.info(f"Step 3: Validating JWT on local Envoy at {body.host}...")
            check_url = f"https://{body.host}/auth/check_jwt"
            check_resp = await client.get(
                check_url,
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            logger.info(f"Envoy check_jwt response code: {check_resp.status_code}")
            
            expiry_iso = None
            if check_resp.status_code == 200:
                try:
                    check_json = check_resp.json()
                    # Envoy returns {"is_valid": true, "expire_time": <unix_ts>}
                    expire_ts = check_json.get("expire_time")
                    if expire_ts:
                        from datetime import datetime, timezone
                        expiry_iso = datetime.fromtimestamp(expire_ts, tz=timezone.utc).isoformat()
                        logger.info(f"JWT validated successfully. Expiry: {expiry_iso}")
                    else:
                        logger.warning(f"check_jwt succeeded but no expire_time found. JSON: {check_json}")
                except Exception as e:
                    logger.warning(f"Failed to parse check_jwt JSON response: {e}. Body: {check_resp.text[:200]}")
            else:
                logger.warning(f"check_jwt returned non-200 status code: {check_resp.status_code}. Response: {check_resp.text[:200]}")

            # ── Save token + expiry to DB ───────────────────────────────────
            logger.info("Saving Enphase token, expiry, and credentials to database...")
            await db.upsert_solar_forecast_config(
                enphase_token=jwt_token,
                enphase_token_expiry=expiry_iso,
                enphase_serial=body.serial,
                enphase_enlighten_user=body.enlighten_user,
                # Store password for auto-renewal — same store as enphase_password
                enphase_enlighten_password=body.enlighten_password,
            )

            logger.info(
                f"☀️  Enphase JWT fetched and saved for serial {body.serial[:4]}... "
                f"account_type={account_type} expiry={expiry_iso}"
            )
            return {
                "ok": True,
                "account_type": account_type,
                "expiry": expiry_iso,
                "message": (
                    "Installer account — DPEL export control available"
                    if account_type == "installer"
                    else "Standard account — curtailment relay only"
                ),
            }

    except httpx.ConnectError as ce:
        err_msg = f"Cannot reach Envoy at {body.host} — check IP/hostname and local network: {ce}"
        logger.warning(err_msg)
        return {"ok": False, "error": err_msg}
    except Exception as exc:
        logger.error(f"Enphase JWT fetch failed: {exc}", exc_info=True)
        return {"ok": False, "error": str(exc)}


@router.post("/enphase/test")
async def test_enphase_connection(body: EnphaseTestRequest):
    """Test connection to an Enphase Envoy and detect DPEL capabilities."""
    logger.info(f"🔌 Testing connection to Envoy at host: {body.host}, user: {body.username}, using token: {'Yes' if body.token else 'No'}")
    try:
        from src.services.enphase_envoy import DPELController
        import asyncio
        ctrl = DPELController(host=body.host, username=body.username, password=body.password, token=body.token)
        caps = await asyncio.to_thread(ctrl.initialize)
        logger.info(
            f"✅ Envoy capabilities detected successfully:\n"
            f"   - Serial: {caps.serial_number}\n"
            f"   - Firmware: {caps.firmware_version}\n"
            f"   - Region: {caps.region.value if caps.region else 'unknown'}\n"
            f"   - DPEL Capable: {caps.dpel_capable.value if caps.dpel_capable else 'unknown'} ({caps.dpel_reason})\n"
            f"   - Active Power Control: {caps.has_active_power_control}\n"
            f"   - Endpoints Probed: {caps.dpel_endpoints}"
        )
        if caps.dpel_capable.value == "unknown" or not caps.dpel_endpoints:
            logger.warning(f"⚠️  Envoy test succeeded but capabilities are limited/unknown or no DPEL endpoints found.")

        # Every probe inside detect_capabilities is wrapped in `except:
        # continue`, so reaching nothing at all still returned a full
        # capabilities object with "unknown" in every field — and this endpoint
        # reported ok, so the card showed "✓ Envoy connected" above a serial
        # and firmware of "unknown". Not reaching the device is a failed test.
        if not caps.reachable:
            return {
                "ok": False,
                "error": (
                    f"No response from the Envoy at {body.host} — every probe "
                    "(/info, /home, /api/v1/production, grid profile) failed. "
                    "Check the host, and that the JWT is current."
                ),
            }

        return {
            "ok": True,
            "probes_succeeded": caps.probes_succeeded,
            "capabilities": {
                "serial_number": caps.serial_number,
                "firmware_version": caps.firmware_version,
                "region": caps.region.value,
                "dpel_capable": caps.dpel_capable.value,
                "dpel_reason": caps.dpel_reason,
                "endpoints_found": len(caps.dpel_endpoints)
            }
        }
    except Exception as e:
        logger.warning(f"❌ Enphase connection test failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@router.get("/enphase/live")
async def enphase_live_production():
    """
    Poll current solar production watts directly from the local Enphase Envoy.
    Uses stored host + credentials from solar_forecast_config.
    Supports firmware v5/v6 (installer creds) and v7+ (JWT token).
    """
    import httpx
    cfg = await db.get_solar_forecast_config()
    host = cfg.get("enphase_host") or ""
    token = cfg.get("enphase_token") or ""
    username = cfg.get("enphase_user") or "installer"
    password = cfg.get("enphase_password") or ""

    if not host:
        return {"ok": False, "error": "Envoy host not configured — set it in Solar Setup → Enphase"}

    # v7+ firmware serves HTTPS only and rejects plain HTTP, which is why this
    # client already passed verify=False — a setting that means nothing over
    # http://. The scheme was the reason Live Status read "Unreachable" while
    # Test Connection, which uses the HTTPS-defaulting capability detector,
    # reported the same Envoy as connected. Try TLS first, fall back for the
    # v5/v6 firmwares that only speak HTTP.
    schemes = ["https", "http"] if token else ["http", "https"]
    headers: dict = {}
    auth = None

    if token:
        # Firmware v7+ JWT auth
        headers["Authorization"] = f"Bearer {token}"
    elif username and password:
        # Legacy installer credentials
        auth = (username, password)
    else:
        return {"ok": False, "error": "No Envoy credentials configured"}

    import time
    last_error: Exception | None = None
    data = None
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            for scheme in schemes:
                try:
                    resp = await client.get(
                        f"{scheme}://{host}/api/v1/production",
                        headers=headers, auth=auth,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    last_error = e
                    continue
        if data is None:
            raise last_error or httpx.RequestError("Envoy unreachable on http and https")
        production = data.get("production", [{}])
        # Envoy v1 API returns a list — first item is the inverter aggregation
        current_w = None
        today_wh = None
        if isinstance(production, list) and production:
            current_w = production[0].get("activePower")
            today_wh  = production[0].get("whToday")
        elif isinstance(production, dict):
            current_w = production.get("activePower")
            today_wh  = production.get("whToday")

        prev = _envoy_failure_state.pop(host, None)
        if prev:
            logger.info(
                f"Enphase Envoy recovered after {prev['count']} consecutive failures (host={host})"
            )
        return {
            "ok": True,
            "current_w": current_w,
            "today_wh":  today_wh,
            "host":      host,
        }
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code} from Envoy — check credentials/token"}
    except Exception as e:
        now = time.time()
        st = _envoy_failure_state.setdefault(host, {"count": 0, "first_ts": now, "last_warn_ts": 0.0})
        st["count"] += 1
        if now - st["last_warn_ts"] >= _ENVOY_LOG_WINDOW_S:
            logger.warning(
                f"Enphase live poll failed (host={host}, total_failures={st['count']}): {e} "
                f"— suppressing further warnings for {_ENVOY_LOG_WINDOW_S // 60} min"
            )
            st["last_warn_ts"] = now
        else:
            logger.debug(f"Enphase live poll failed (host={host}, suppressed #{st['count']}): {e}")
        return {"ok": False, "error": str(e)}


@router.post("/diagnostics")
async def solar_diagnostics():
    """
    Run a structured diagnostics probe on the Solar Forecast pipeline.

    Returns a timestamped log of each probe step so the UI modal can display
    a human-readable trace of what is working and what is failing.

    Steps:
      1. Config loaded
      2. HA credentials present
      3. Lat / Lng configured
      4. Provider initialised
      5. Cache status
      6. Connection test
      7. Force-refresh attempt
    """
    from datetime import datetime, timezone as _tz

    entries: list[dict] = []
    run_ts = datetime.now(_tz.utc).isoformat()
    mgr = None

    def _log(step: str, status: str, message: str, detail: str = "") -> None:
        entries.append({
            "ts":     datetime.now(_tz.utc).isoformat(),
            "step":   step,
            "status": status,   # ok | warn | error | info
            "msg":    message,
            "detail": detail,
        })

    # ── Step 1: Load config ───────────────────────────────────────────────────
    try:
        cfg = await db.get_solar_forecast_config()
        enabled = cfg.get("enabled", False)
        source  = cfg.get("source", "auto")
        _log("config", "ok" if enabled else "warn",
             f"Config loaded — enabled={enabled}, source={source}",
             f"lat={cfg.get('lat')}, lng={cfg.get('lng')}, kwp={cfg.get('kwp')}")
    except Exception as exc:
        _log("config", "error", f"Config load failed: {exc}")
        return {"ok": False, "run_ts": run_ts, "entries": entries}

    # ── Step 2: HA credentials (only checked when source=ha_entities) ───────────
    ha_creds: dict = {}
    if source == "ha_entities":
        try:
            ha_creds = await _get_ha_creds()
            ha_url   = ha_creds.get("ha_url", "")
            ha_token = ha_creds.get("ha_token", "")
            if ha_url and ha_token:
                _log("ha_creds", "ok", f"HA credentials present — URL: {ha_url}")
            elif ha_url:
                _log("ha_creds", "error", "HA URL set but token missing — ha_entities source requires a valid token",
                     "Set the HA Long-Lived Access Token in Home Automation settings")
            else:
                _log("ha_creds", "error", "No HA credentials configured — ha_entities source cannot fetch data",
                     "Add HA URL and token in Home Automation settings, or switch to a different source")
        except Exception as exc:
            _log("ha_creds", "error", f"HA creds lookup failed: {exc}")
    else:
        # Still fetch silently so manager can be built, but don't surface in the report
        try:
            ha_creds = await _get_ha_creds()
        except Exception:
            ha_creds = {}

    # ── Step 3: Lat / Lng ─────────────────────────────────────────────────────
    lat = cfg.get("lat")
    lng = cfg.get("lng")
    if lat is not None and lng is not None:
        # Latitude sign check, against a source that has a sign rather than
        # against the longitude. The previous rule read 100 < lng < 180 as
        # "southern", which reports a sign error to every correctly configured
        # user in Tokyo, Seoul, Beijing, Taipei or Manila.
        from src.services import site_location

        reference = await site_location.from_home_assistant()
        contradicts_ha = bool(
            reference
            and abs(abs(float(lat)) - abs(reference["lat"])) < 0.5
            and (float(lat) >= 0) != (reference["lat"] >= 0)
        )
        if contradicts_ha:
            _log("location", "error",
                 f"Latitude sign error — lat={lat}, but Home Assistant has "
                 f"{reference['lat']} for this home",
                 f"Fix: enter {reference['lat']} in Solar Setup → PV System. "
                 f"A wrong-signed latitude is why Forecast.Solar returns "
                 f"'No valid location' (error 600).")
        else:
            _log("location", "ok", f"Location configured — lat={lat}, lng={lng}",
                 f"azimuth={cfg.get('azimuth', 180)}, tilt={cfg.get('tilt', 22.5)}, kwp={cfg.get('kwp', 5.0)}")
    else:
        sev = "error" if source in ("forecast_solar", "solcast", "openmeteo") else "warn"
        _log("location", sev,
             "Lat/Lng not configured — built-in providers require coordinates",
             "Set Latitude and Longitude in Solar Setup → PV System")

    # ── Step 4: Manager + provider init ──────────────────────────────────────
    try:
        mgr = _build_manager(cfg, ha_creds)
        status_obj = mgr.get_status()
        active_source  = status_obj.get("active_source", "none")
        provider_ready = status_obj.get("provider_ready", False)

        if active_source == "none":
            _log("provider", "error", "No active forecast source — check config and credentials",
                 "Set source to Forecast.Solar (free) or configure HA entities in Solar Setup")
        elif active_source == "ha_entities":
            ha_avail = status_obj.get("ha_available", False)
            _log("provider", "ok" if ha_avail else "warn",
                 f"Active source: ha_entities — available={ha_avail}")
        else:
            # Show the actual API endpoint that will be called (with correct azimuth conversion)
            api_url = None
            if active_source == "forecast_solar" and lat and lng:
                try:
                    api_url = mgr._provider.get_raw_api_url(lat, lng, mgr.installation)
                    ha_az  = cfg.get("azimuth", 180)
                    from src.services.solar.forecast_solar import _ha_azimuth_to_api
                    api_az = _ha_azimuth_to_api(float(ha_az))
                    api_url += f"  [HA az={ha_az}° → API az={api_az}° (0=South)]"
                except Exception:
                    api_url = f"GET https://api.forecast.solar/estimate/{lat}/{lng}/{cfg.get('tilt', 22.5)}/{cfg.get('azimuth', 180)}/{cfg.get('kwp', 5.0)}"
            elif active_source == "openmeteo" and lat and lng:
                try:
                    ha_az  = cfg.get("azimuth", 180)
                    from src.services.solar.open_meteo import _ha_azimuth_to_api
                    api_az = _ha_azimuth_to_api(float(ha_az))
                    api_url = f"GET https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}&hourly=global_tilted_irradiance&tilt={cfg.get('tilt', 22.5)}&azimuth={api_az}&timezone=UTC"
                except Exception:
                    api_url = f"GET https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}&hourly=global_tilted_irradiance"
            elif active_source == "solcast":
                site_id = cfg.get("solcast_site_id", "")
                api_url = f"GET https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts?format=json" if site_id else "solcast: site_id not configured"
            _log("provider", "ok" if provider_ready else "warn",
                 f"Active source: {active_source} — provider_ready={provider_ready}",
                 api_url or "")
    except Exception as exc:
        _log("provider", "error", f"Manager build failed: {exc}")
        return {"ok": False, "run_ts": run_ts, "entries": entries}

    # ── Step 4b: HA entity config check (ONLY for source=ha_entities) ────────
    if source == "ha_entities":
        actual_entity = cfg.get("ha_solar_actual_entity", "").strip()
        forecast_entity = cfg.get("ha_solar_forecast_entity", "").strip()
        if not actual_entity and not forecast_entity:
            _log("ha_entities_config", "error",
                 "No HA entity IDs configured — source=ha_entities requires at least one entity",
                 "Go to Solar Setup → HA Entities and enter the entity IDs explicitly. "
                 "Check HA → Developer Tools → States to find the correct IDs.")
        else:
            import requests as _req
            _ha_url   = ha_creds.get("ha_url", "").rstrip("/")
            _ha_token = ha_creds.get("ha_token", "")
            _headers  = {"Authorization": f"Bearer {_ha_token}", "Content-Type": "application/json"}

            def _check_entity(eid: str, label: str) -> str:
                if not eid:
                    return f"{label}: not configured"
                try:
                    r = _req.get(f"{_ha_url}/api/states/{eid}", headers=_headers, timeout=5)
                    if r.status_code == 200:
                        val  = r.json().get("state", "?")
                        unit = r.json().get("attributes", {}).get("unit_of_measurement", "")
                        return f"{label} ({eid}): {val} {unit}".strip()
                    return f"{label} ({eid}): HTTP {r.status_code} — entity not found"
                except Exception as ex:
                    return f"{label} ({eid}): error — {ex}"

            results = []
            if actual_entity:
                results.append(_check_entity(actual_entity, "live_kw"))
            if forecast_entity:
                results.append(_check_entity(forecast_entity, "remaining_kwh"))

            all_ok = all("HTTP" not in r and "error" not in r and "not configured" not in r
                         for r in results)
            _log("ha_entities_config",
                 "ok" if all_ok else "error",
                 f"Configured HA entities — {'all reachable' if all_ok else 'one or more not found'}",
                 " · ".join(results))

    # ── Step 5: Cache status ──────────────────────────────────────────────────
    cache = mgr._load_cache()
    if cache:
        last_sync  = cache.get("last_sync", "unknown")
        slot_count = len(cache.get("data", []))
        cache_src  = cache.get("source", "unknown")
        _log("cache", "ok",
             f"Cache valid — {slot_count} slots from source '{cache_src}'",
             f"Last sync: {last_sync} · File: {str(mgr.cache_file)}")
    else:
        _log("cache", "warn", "No cache file found — first fetch not yet completed",
             f"Expected at: {str(mgr.cache_file)} — Use Refresh to populate it.")

    # ── Step 6: Connection test ───────────────────────────────────────────────
    try:
        conn_ok = mgr.test_connection()
        _log("connection_test", "ok" if conn_ok else "error",
             f"Connection test: {'PASS' if conn_ok else 'FAIL'}",
             f"Source: {active_source} · Validates the active source is reachable and returning data")
    except Exception as exc:
        _log("connection_test", "error", f"Connection test raised exception: {exc}")
        conn_ok = False

    # ── Step 7: Force-refresh (calls provider directly — exceptions propagate) ──
    has_location = lat is not None and lng is not None
    can_refresh  = enabled and (has_location or active_source == "ha_entities")
    if can_refresh:
        try:
            if active_source == "ha_entities" and mgr._ha_source:
                slots = mgr._ha_source.get_forecast()
            elif mgr._provider and has_location:
                slots = mgr._provider.get_forecast(lat, lng, mgr.installation)
            else:
                slots = mgr.get_forecast(force_refresh=True)

            if slots:
                peak_kw   = max((s.get("pv_kw", 0) for s in slots), default=0)
                total_kwh = sum(s.get("pv_kw", 0) * 0.5 for s in slots)
                mgr._save_cache(slots, source=active_source)
                _log("refresh", "ok",
                     f"Force-refresh succeeded — {len(slots)} slots fetched",
                     f"Total today ≈ {total_kwh:.1f} kWh · Peak slot: {peak_kw:.2f} kW · Source: {active_source}")
            else:
                _log("refresh", "error",
                     "Refresh returned 0 slots — provider returned no data",
                     f"Source: {active_source} · Forecast.Solar free tier: ~10 req/day. Try again in ~1h, or switch to HA entities source.")
        except Exception as exc:
            _log("refresh", "error", f"Force-refresh failed: {exc}",
                 f"Source: {active_source} · Check Docker logs for full traceback")
    else:
        reason = "Solar Forecast is disabled" if not enabled else "no location configured"
        _log("refresh", "info", f"Force-refresh skipped — {reason}")

    has_error = any(e["status"] == "error" for e in entries)
    n_ok   = sum(1 for e in entries if e["status"] == "ok")
    n_warn = sum(1 for e in entries if e["status"] == "warn")
    n_err  = sum(1 for e in entries if e["status"] == "error")

    return {
        "ok":      not has_error,
        "run_ts":  run_ts,
        "summary": f"{n_ok} OK · {n_warn} WARN · {n_err} ERR",
        "entries": entries,
        "status":  mgr.get_status() if mgr else {},
    }


# ---------------------------------------------------------------------------
# Ph-3 — Gateway Solar Sources CRUD
# ---------------------------------------------------------------------------

# FranklinWH Installer App aligned field names (Ph-2)
SOURCE_TYPE_LABELS = {
    # Legacy coarse types kept for backward display compat (v21 migration removes them from DB)
    "pv_port":      "PV Input (Legacy)",
    "dc_coupled":   "MPPT / DC Input (Legacy)",
    "remote_pv":    "Remote PV (Legacy)",
    # Mode-specific types — aligned to FranklinWH Installer App naming
    "pv_port_1":    "PV Input 1",
    "pv_port_2":    "PV Input 2",
    "mppt_1":       "MPPT / DC Input 1",
    "mppt_2":       "MPPT / DC Input 2",
    "remote_pv_1":  "Remote Solar PV 1",
    "remote_pv_2":  "Remote Solar PV 2",
    "split_ct":     "Split-CT Solar",
}

INVERTER_TYPE_LABELS = {
    "string":    "Dedicated String",   # FW Installer App naming
    "micro":     "Microinverter",
    "optimiser": "Optimiser (DC)",
}

SOLAR_METERING_MODE_LABELS = {
    "single_phase_internal": "Single-Phase Internal CT (default)",
    "three_phase_ct_kit":    "Three-Phase CT Kit (RS485 meter)",
    "split_ct_external":     "External Split-CT Clamp",
    "rs485_meter":           "RS485 Energy Meter (Enphase/Fronius API)",
}

GRID_TYPE_LABELS = {
    "single_phase":    "Single Phase (230/240V AU • 120V US)",
    "split_phase_240": "Split Phase 240V (US 120/240V)",
    "split_phase_208": "Split Phase 208V (US 120/208V wye — 3-phase service)",
    "three_phase_230": "Three Phase 230V (AU/EU 3×230V wye)",
    "three_phase_415": "Three Phase 415V (AU 3×240V — 415V line-to-line)",
    "off_grid":        "Off-Grid Site (No Grid Service)",
}

# AU solar breaker limit per AS/NZS 60898 (from FranklinWH SLD)
_AU_MAX_SOLAR_AMPS = 63
_US_MAX_SOLAR_AMPS = 40  # NEC 690 typical residential

# Display-friendly label for any stored source (handles legacy + new)
def _source_display_label(source_type: str, port: int | None = None) -> str:
    """Map source_type (+ optional port) to a user-facing label. Handles legacy rows."""
    if source_type == "pv_port":
        return f"PV Input {port}" if port else "PV Input (Legacy)"
    if source_type == "dc_coupled":
        return "MPPT / DC Input (Legacy)"
    if source_type == "remote_pv":
        return "Remote Solar PV (Legacy)"
    return SOURCE_TYPE_LABELS.get(source_type, source_type)


def _amps_warning(kwp: float, ac_voltage: int | None, max_amps: int) -> str | None:
    """Return an amperage warning string if kWp exceeds breaker rating, else None."""
    v = ac_voltage or 230  # default to AU 230V
    implied_amps = (kwp * 1000) / v
    if implied_amps > max_amps:
        return (
            f"{kwp:.1f} kWp implies {implied_amps:.0f} A at {v}V — "
            f"exceeds {max_amps}A solar breaker limit (AS/NZS 60898). "
            "Verify with installer before saving."
        )
    return None


class SolarSourceCreate(BaseModel):
    source_type: str = Field(
        ..., pattern=r"^(pv_port|pv_port_1|pv_port_2|dc_coupled|mppt_1|mppt_2|remote_pv|remote_pv_1|remote_pv_2|ahub_pv_1|ahub_pv_2|apbox_pv_1|apbox_pv_2|split_ct)$",
    )
    kwp:          float          = Field(..., ge=0)
    label:        Optional[str] = None
    source_name:  Optional[str] = None
    port:         Optional[int] = Field(None, ge=1, le=2)
    accessory_id: Optional[str] = None
    # Inverter metadata
    brand:              Optional[str] = None
    inverter_type:      Optional[str] = Field(None, pattern=r"^(string|micro|optimiser)$" if True else None)
    phase_count:        Optional[int] = Field(None, ge=1, le=3)
    ac_voltage:         Optional[int] = Field(None)
    ac_hz:              Optional[int] = Field(None)
    pv_control:         Optional[int] = Field(None, ge=0, le=1)
    pv_control_type:    Optional[str] = None
    pv_control_entity:  Optional[str] = None
    # Ph-2: Amperage safety
    max_amps:           Optional[int] = Field(None, ge=1, le=200)
    # Ph-3: Three-phase metering
    solar_metering_mode: Optional[str] = Field(None, pattern=r"^(single_phase_internal|three_phase_ct_kit|split_ct_external|rs485_meter)$" if True else None)
    # Ph-6: Capability toggles
    off_grid_capable:   Optional[int] = Field(None, ge=0, le=1)
    pv_data_api:        Optional[int] = Field(None, ge=0, le=1)


class SolarSourceUpdate(BaseModel):
    kwp:          Optional[float] = Field(None, ge=0)
    label:        Optional[str]   = None
    source_name:  Optional[str]   = None
    port:         Optional[int]   = Field(None, ge=1, le=2)
    accessory_id: Optional[str]   = None
    enabled:      Optional[int]   = Field(None, ge=0, le=1)
    brand:              Optional[str] = None
    inverter_type:      Optional[str] = None
    phase_count:        Optional[int] = Field(None, ge=1, le=3)
    ac_voltage:         Optional[int] = None
    ac_hz:              Optional[int] = None
    pv_control:         Optional[int] = Field(None, ge=0, le=1)
    pv_control_type:    Optional[str] = None
    pv_control_entity:  Optional[str] = None
    max_amps:           Optional[int] = Field(None, ge=1, le=200)
    solar_metering_mode: Optional[str] = None
    off_grid_capable:   Optional[int] = Field(None, ge=0, le=1)
    pv_data_api:        Optional[int] = Field(None, ge=0, le=1)


class GatewayTopologyUpdate(BaseModel):
    """Ph-4: Installer-configurable grid topology for a gateway."""
    service_amps:         Optional[int] = Field(None, ge=1, le=400,
                                                description="Installer service derating in Amps")
    grid_type:            Optional[str] = Field(None,
                                                pattern=r"^(single_phase|split_phase_240|split_phase_208|three_phase_230|three_phase_415|off_grid)$" if True else None)
    gateway_phase:        Optional[str] = Field(None, pattern=r"^(L1|L2|L3|split)$" if True else None)
    three_phase_group_id: Optional[str] = None


class SolarSourceLinkRequest(BaseModel):
    """Body for PATCH /api/solar/sources/{src_id}/link."""
    utility_service_id: Optional[str] = None   # None = unlink


@router.get("/gateways/{gw_id}/sources")
async def list_solar_sources(gw_id: str):
    """List all solar sources for a gateway with topology labels and total kWp."""
    sources = await db.get_gateway_solar_sources(gw_id)
    total_kwp = sum(s["kwp"] for s in sources if s.get("enabled"))
    for s in sources:
        s["source_type_label"] = _source_display_label(s.get("source_type", ""), s.get("port"))
    return {
        "ok":         True,
        "gateway_id": gw_id,
        "sources":    sources,
        "total_kwp":  round(total_kwp, 2),
        "source_type_options": [{"value": k, "label": v} for k, v in SOURCE_TYPE_LABELS.items()],
    }


@router.post("/gateways/{gw_id}/sources")
async def add_solar_source(gw_id: str, body: SolarSourceCreate):
    """Add a new solar source to a gateway with amperage safety validation.

    Enforces: each source_type (+ port for legacy pv_port) can only appear once
    per gateway — a solar inverter may only be assigned to ONE gateway port slot.
    """
    # Q2: API-layer uniqueness — one source_type per gateway slot
    existing = await db.get_gateway_solar_sources(gw_id)
    for s in existing:
        if s.get("source_type") == body.source_type:
            # For legacy pv_port type, also match on port number
            if body.source_type != "pv_port" or s.get("port") == body.port:
                label = _source_display_label(body.source_type, body.port)
                return {"ok": False, "error": f"{label} is already configured on this gateway. A solar inverter can only be assigned to one gateway port slot."}


    # Ph-2: Amperage warning (non-blocking, advisory only)
    max_amps = body.max_amps or _AU_MAX_SOLAR_AMPS
    amps_warn = _amps_warning(body.kwp, body.ac_voltage, max_amps)

    row = await db.add_gateway_solar_source(
        gateway_id=gw_id,
        source_type=body.source_type,
        kwp=body.kwp,
        label=body.label,
        source_name=body.source_name,
        port=body.port,
        accessory_id=body.accessory_id,
        detected_by="manual",
        brand=body.brand,
        inverter_type=body.inverter_type,
        phase_count=body.phase_count or 1,
        ac_voltage=body.ac_voltage,
        ac_hz=body.ac_hz,
        pv_control=body.pv_control or 0,
        pv_control_type=body.pv_control_type,
        pv_control_entity=body.pv_control_entity,
        max_amps=max_amps,
        solar_metering_mode=body.solar_metering_mode or "single_phase_internal",
        off_grid_capable=body.off_grid_capable or 0,
        pv_data_api=body.pv_data_api or 0,
    )
    row["source_type_label"] = _source_display_label(row.get("source_type", ""), row.get("port"))
    row["inverter_type_label"] = INVERTER_TYPE_LABELS.get(row.get("inverter_type", ""), "")
    row["solar_metering_mode_label"] = SOLAR_METERING_MODE_LABELS.get(row.get("solar_metering_mode", ""), "")
    if amps_warn:
        logger.warning(f"⚠️  Solar source amps warning: gateway={gw_id} — {amps_warn}")
    logger.info(f"☀️  Solar source added: gateway={gw_id} type={body.source_type} kwp={body.kwp} name={body.source_name!r}")
    return {"ok": True, "source": row, "warning": amps_warn}


@router.patch("/gateways/{gw_id}/topology")
async def update_gateway_topology(gw_id: str, body: GatewayTopologyUpdate):
    """Ph-4: Update installer grid topology fields (service_amps, grid_type, gateway_phase, three_phase_group_id)."""
    gw = await db.get_gateway(gw_id)
    if not gw:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Gateway {gw_id} not found")
    updated = await db.update_gateway_topology(
        short_id=gw_id,
        service_amps=body.service_amps,
        grid_type=body.grid_type,
        gateway_phase=body.gateway_phase,
        three_phase_group_id=body.three_phase_group_id,
    )
    gw_fresh = await db.get_gateway(gw_id)
    logger.info(f"🔌 Grid topology updated: gateway={gw_id} type={body.grid_type} phase={body.gateway_phase} amps={body.service_amps}")
    return {
        "ok": True,
        "gateway_id": gw_id,
        "service_amps":         gw_fresh.get("service_amps"),
        "grid_type":            gw_fresh.get("grid_type"),
        "grid_type_label":      GRID_TYPE_LABELS.get(gw_fresh.get("grid_type", ""), ""),
        "gateway_phase":        gw_fresh.get("gateway_phase"),
        "three_phase_group_id": gw_fresh.get("three_phase_group_id"),
    }


@router.patch("/gateways/{gw_id}/utility-link")
async def set_gateway_utility_link(gw_id: str, body: dict):
    """Link (or unlink) a gateway to a Utility Service from the Solar Setup card.
    Body: { "utility_service_id": "<id>" | null }
    """
    try:
        gw = await db.get_gateway(gw_id)
        if not gw:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Gateway {gw_id} not found")
        svc_id = body.get("utility_service_id") or None
        if svc_id:
            await db.link_gateway_to_utility_service(gw_id, svc_id)
            logger.info(f"🔗 Gateway {gw_id} linked to utility service {svc_id}")
        else:
            await db.unlink_gateway_from_utility_service(gw_id)  # unlinks all for this gw
            logger.info(f"🔗 Gateway {gw_id} unlinked from utility service")
        # Return current link state (first link for backward compat with solar setup dropdown)
        links = await db.get_gateway_utility_links_flat()
        return {"ok": True, "utility_service_id": links.get(gw_id)}
    except Exception as exc:
        logger.exception("Failed to set utility link")
        return {"ok": False, "error": str(exc)}


@router.put("/gateways/{gw_id}/sources/{src_id}")
async def update_solar_source(gw_id: str, src_id: str, body: SolarSourceUpdate):
    """Update a solar source (kwp, label, port, accessory_id, enabled)."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": False, "error": "No fields to update"}
    updated = await db.update_gateway_solar_source(src_id, gw_id, **updates)
    if not updated:
        return {"ok": False, "error": "Source not found or no changes applied"}
    sources = await db.get_gateway_solar_sources(gw_id)
    total_kwp = sum(s["kwp"] for s in sources if s.get("enabled"))
    return {"ok": True, "src_id": src_id, "total_kwp": round(total_kwp, 2)}


@router.delete("/gateways/{gw_id}/sources/{src_id}")
async def delete_solar_source(gw_id: str, src_id: str):
    """Delete a manual solar source (Discover-detected sources can only be disabled via PUT)."""
    deleted = await db.delete_gateway_solar_source(src_id, gw_id)
    if not deleted:
        return {
            "ok": False,
            "error": "Source not found, not owned by this gateway, or is Discover-detected — use PUT enabled=0 to disable",
        }
    sources = await db.get_gateway_solar_sources(gw_id)
    total_kwp = sum(s["kwp"] for s in sources if s.get("enabled"))
    logger.info(f"☀️  Solar source deleted: gateway={gw_id} src_id={src_id}")
    return {"ok": True, "src_id": src_id, "total_kwp": round(total_kwp, 2)}


@router.get("/gateways/{gw_id}/capabilities")
async def get_solar_capabilities(gw_id: str, mock_hardware: str = ""):
    """Return hardware-derived solar PV topology capabilities for a gateway.

    Reads profile_json from the DB (no live Cloud API call).
    Used by the Solar Setup UI to filter the aGate Input Port dropdown
    to only show topologies valid for the installed hardware.
    """
    import json as _json
    from fastapi import HTTPException

    gw = await db.get_gateway(gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {gw_id} not found")

    profile: dict = _json.loads(gw.get("profile_json") or "{}")

    # ── Hardware flags ─────────────────────────────────────────────────────────
    has_solar    = profile.get("has_solar", False) or (mock_hardware == "apbox")
    has_mppt     = profile.get("has_mppt", False) or profile.get("mppt_enabled", False) or (mock_hardware == "apbox")
    has_apbox    = profile.get("has_apbox", False) or (mock_hardware == "apbox")
    has_ahub     = profile.get("has_ahub", False) or (mock_hardware == "apbox" or mock_hardware == "ahub")
    remote_solar = profile.get("remote_solar", False) or (mock_hardware == "apbox")
    has_split_ct = profile.get("has_split_ct", False) or profile.get("ct_split_pv", False) or (mock_hardware == "apbox")
    model        = profile.get("model", "") or gw.get("model", "")
    model_name   = profile.get("model_name", "") or model

    # ── Derive allowed topologies ──────────────────────────────────────────────
    pv_port_ok    = has_solar and (not has_mppt or mock_hardware == "apbox")   # AC-coupled PV port (aPower X/2)
    dc_coupled_ok = has_mppt                      # DC-coupled MPPT (aPower S)
    remote_pv_ok  = has_apbox or has_ahub or remote_solar
    # split_ct always available — external CT meter, hw-independent

    allowed  = []
    excluded = []

    if pv_port_ok:
        # FW Installer App naming: "PV Input 1 / PV Input 2"
        allowed.append({
            "value":  "pv_port_1",
            "label":  "PV Input 1",
            "reason": f"has_solar=true on {model_name or 'gateway'} — aGate PV Input Port 1",
            "icon":   "fa-solar-panel",
            "colour": "#fbbf24",
        })
        allowed.append({
            "value":  "pv_port_2",
            "label":  "PV Input 2",
            "reason": f"has_solar=true on {model_name or 'gateway'} — aGate PV Input Port 2",
            "icon":   "fa-solar-panel",
            "colour": "#f59e0b",
        })
    else:
        reason = (
            "DC-coupled (aPower S) — PV Input ports not applicable"
            if has_mppt else
            "has_solar=false — no PV Input ports detected on this gateway"
        )
        excluded.append({"value": "pv_port_1", "label": "PV Input 1", "reason": reason})
        excluded.append({"value": "pv_port_2", "label": "PV Input 2", "reason": reason})

    if dc_coupled_ok:
        # FW Installer App naming: "MPPT / DC Input"
        allowed.append({
            "value":  "mppt_1",
            "label":  "MPPT / DC Input 1",
            "reason": f"mppt_enabled=true on {model_name or 'gateway'} — MPPT channel 1 (aPower S)",
            "icon":   "fa-bolt",
            "colour": "#34d399",
        })
        allowed.append({
            "value":  "mppt_2",
            "label":  "MPPT / DC Input 2",
            "reason": f"mppt_enabled=true on {model_name or 'gateway'} — MPPT channel 2 (aPower S)",
            "icon":   "fa-bolt",
            "colour": "#10b981",
        })
    else:
        excluded.append({"value": "mppt_1", "label": "MPPT / DC Input 1",
                         "reason": "has_mppt=false — requires aPower S with built-in MPPT"})
        excluded.append({"value": "mppt_2", "label": "MPPT / DC Input 2",
                         "reason": "has_mppt=false — requires aPower S with built-in MPPT"})

    if has_apbox or remote_solar:
        allowed.append({
            "value":       "apbox_pv_1",
            "label":       "aPBox Remote PV 1",
            "reason":      "Accessory detected: aPBox — remote PV port 1",
            "icon":        "fa-tower-broadcast",
            "colour":      "#60a5fa",
            "accessories": {"has_apbox": True},
        })
        allowed.append({
            "value":       "apbox_pv_2",
            "label":       "aPBox Remote PV 2",
            "reason":      "Accessory detected: aPBox — remote PV port 2",
            "icon":        "fa-tower-broadcast",
            "colour":      "#3b82f6",
            "accessories": {"has_apbox": True},
        })
    else:
        excluded.append({"value": "apbox_pv_1", "label": "aPBox Remote PV 1", "reason": "No aPBox accessory detected"})
        excluded.append({"value": "apbox_pv_2", "label": "aPBox Remote PV 2", "reason": "No aPBox accessory detected"})

    if has_ahub:
        allowed.append({
            "value":       "ahub_pv_1",
            "label":       "aHub PV Input 1",
            "reason":      "Accessory detected: aHub — PV port 1",
            "icon":        "fa-tower-broadcast",
            "colour":      "#818cf8",
            "accessories": {"has_ahub": True},
        })
        allowed.append({
            "value":       "ahub_pv_2",
            "label":       "aHub PV Input 2",
            "reason":      "Accessory detected: aHub — PV port 2",
            "icon":        "fa-tower-broadcast",
            "colour":      "#6366f1",
            "accessories": {"has_ahub": True},
        })
    else:
        excluded.append({"value": "ahub_pv_1", "label": "aHub PV Input 1", "reason": "No aHub accessory detected"})
        excluded.append({"value": "ahub_pv_2", "label": "aHub PV Input 2", "reason": "No aHub accessory detected"})

    if has_split_ct:
        allowed.append({
            "value":    "split_ct",
            "label":    "Split-CT Solar",
            "reason":   "External CT meter detected on gateway.",
            "icon":     "fa-scissors",
            "colour":   "#c084fc",
            "advisory": "Not included in SD forecast unless toggled on per source.",
            "detected": True,
        })
    else:
        excluded.append({"value": "split_ct", "label": "Split-CT Solar", "reason": "No Split-CT hardware detected on this gateway"})


    # Ph-UC: current utility service link (first link for solar dropdown compat)
    utility_links = await db.get_gateway_utility_links_flat()
    linked_utility_service_id = utility_links.get(gw_id)

    return {
        "ok":                    True,
        "gateway_id":            gw_id,
        "model":                 model,
        "model_name":            model_name,
        "allowed_source_types":  allowed,
        "excluded_source_types": excluded,
        "solar_metering_mode_options": [
            {"value": k, "label": v} for k, v in SOLAR_METERING_MODE_LABELS.items()
        ],
        "inverter_type_options": [
            {"value": k, "label": v} for k, v in INVERTER_TYPE_LABELS.items()
        ],
        # Ph-4: current gateway grid topology
        "grid_topology": {
            "service_amps":         gw.get("service_amps"),
            "grid_type":            gw.get("grid_type"),
            "grid_type_label":      GRID_TYPE_LABELS.get(gw.get("grid_type", ""), ""),
            "gateway_phase":        gw.get("gateway_phase"),
            "three_phase_group_id": gw.get("three_phase_group_id"),
        },
        # Ph-UC: utility service link
        "utility_service_id": linked_utility_service_id,
        "flags": {
            "has_solar":    has_solar,
            "has_mppt":     has_mppt,
            "has_apbox":    has_apbox,
            "has_ahub":     has_ahub,
            "remote_solar": remote_solar,
            "has_split_ct": has_split_ct,
        },
    }


# ────────────────────────────────────────────────────────────────────────────────
# Ph-SL — Solar ↔ Utility Service cross-service endpoints
# ────────────────────────────────────────────────────────────────────────────────

@router.get("/sources")
async def list_all_solar_sources():
    """List every solar source across all gateways with gateway name and linked utility service.

    Used by the Utility Service edit modal to present a checklist of solar sources
    that can be assigned to the electricity service being edited.
    """
    rows = await db.get_all_solar_sources_with_gateway()
    for r in rows:
        r["source_type_label"] = SOURCE_TYPE_LABELS.get(r.get("source_type", ""), r.get("source_type", ""))
    return {"ok": True, "sources": rows, "count": len(rows)}


@router.patch("/sources/{src_id}/link")
async def link_solar_source(src_id: str, body: SolarSourceLinkRequest):
    """Assign or unassign a solar source to/from a utility service.

    Pass utility_service_id=null to unlink. A solar source may only be linked
    to one utility service at a time (FK constraint enforced at application layer).
    """
    updated = await db.link_solar_source_to_utility_service(src_id, body.utility_service_id)
    if not updated:
        return {"ok": False, "error": f"Solar source {src_id!r} not found"}
    action = "linked" if body.utility_service_id else "unlinked"
    logger.info(f"☀️  Solar source {src_id} {action} to utility_service={body.utility_service_id!r}")
    return {"ok": True, "src_id": src_id, "utility_service_id": body.utility_service_id, "action": action}


class SyncLocationRequest(BaseModel):
    gateway_id: Optional[str] = None


@router.post("/sync_location")
async def sync_location(req: Optional[SyncLocationRequest] = None):
    """Query active gateway equipment location coordinates from FranklinWH Cloud, update db, and return coords."""
    state = get_app_state()
    registry = state.get("registry")
    if not registry:
        raise HTTPException(status_code=500, detail="Registry not loaded")

    gateway_id = req.gateway_id if req else None
    if not gateway_id:
        gateways = await db.get_all_gateways()
        if not gateways:
            raise HTTPException(status_code=400, detail="No registered gateways to sync location from")
        gateway_id = gateways[0]["short_id"]

    gw = registry.get_gateway(gateway_id)
    if not gw:
        raise HTTPException(status_code=404, detail=f"Gateway {gateway_id} not found in registry")

    # Home Assistant first, the cloud second. The cloud call used to go through
    # `client.raw(...)`, which is not a method the client has, so this endpoint
    # returned 502 every time it was pressed.
    from src.services import site_location

    loc = await site_location.resolve(gw)
    if not loc:
        raise HTTPException(
            status_code=502,
            detail="No location available — Home Assistant has none set and the "
                   "FranklinWH cloud returned none for this gateway.",
        )

    lat, lng = loc["lat"], loc["lng"]
    loc_resp = loc

    # One write, every store — see docs/geolocation.md. The weather config kept
    # its own copy and only read through to solar's while blank, so a latitude
    # typed on the Weather tab diverged from the forecast permanently.
    from src.services import site_location as _loc

    await _loc.propagate(lat, lng, short_id=gateway_id)
    
    logger.info(
        f"Location synced from {loc['source']} for gateway {gateway_id}: "
        f"lat={lat}, lng={lng}"
    )

    return {
        "ok": True,
        "lat": lat,
        "lng": lng,
        "source": loc["source"],
        "city": loc_resp.get("city"),
        "country": loc_resp.get("country"),
        "timezone": loc_resp.get("timezone"),
    }

