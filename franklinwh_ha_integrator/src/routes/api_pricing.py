"""
Dynamic Pricing API routes.

GET  /api/pricing/status            → service status (last_fetch_seconds_ago)
GET  /api/pricing/current           → live price snapshot
GET  /api/pricing/forecast          → 24h forecast array
GET  /api/pricing/history           → rolling 48h history
GET  /api/pricing/config            → provider config (creds redacted)
POST /api/pricing/config            → save config + restart service
POST /api/pricing/test              → test provider connection
POST /api/pricing/refresh           → force immediate poll
GET  /api/pricing/utility           → utility/bill config
POST /api/pricing/utility           → save utility/bill config
GET  /api/pricing/dashboard         → aggregated dashboard summary

GET  /api/pricing/amber/site        → Amber site metadata (NMI, network, channels)
GET  /api/pricing/amber/usage       → Usage data by date range
GET  /api/pricing/amber/prices      → Historical price data by date range
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any

from src.services import db
from src.services.pricing.service import pricing_registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pricing"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PricingConfigRequest(BaseModel):
    provider: str        # amber | localvolts | comed | flat
    region: str = "AU"
    enabled: bool = True
    credentials: dict = {}
    settings: dict = {}


class PricingModelUpdateRequest(BaseModel):
    credentials: dict = {}
    settings: dict = {}


class UtilityConfigRequest(BaseModel):
    # Utility identity
    utility_name: Optional[str] = None
    account_number: Optional[str] = None
    nmi_id: Optional[str] = None
    meter_serial: Optional[str] = None
    meter_type: Optional[str] = None          # smart | interval | accumulation
    # Connection
    service_type: Optional[str] = None        # residential | commercial | industrial
    ac_type: Optional[str] = None             # single_phase | three_phase
    voltage_v: Optional[int] = None
    max_demand_kva: Optional[float] = None
    network_area: Optional[str] = None
    # Bill cycle
    bill_frequency: Optional[str] = "quarterly"  # monthly | quarterly | bimonthly
    bill_start_day: Optional[int] = 1
    bill_period_days: Optional[int] = None
    # Fixed charges
    supply_charge_day: Optional[float] = 0.0    # c/day — every rate field is cents
    metering_fee: Optional[float] = 0.0         # $ per period
    network_fixed_fee: Optional[float] = 0.0    # $ per period
    # Demand charges
    demand_charge_kw: Optional[float] = 0.0     # legacy; see utility_service_windows.rate (cents)
    demand_window_start: Optional[str] = None   # HH:MM
    demand_window_end: Optional[str] = None     # HH:MM
    demand_window_days: Optional[str] = "weekdays"
    # Solar / FiT
    fit_rate_c_kwh: Optional[float] = None
    fit_provider: Optional[str] = None
    # Notes
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Pricing config routes
# ---------------------------------------------------------------------------

@router.get("/pricing/status")
async def get_pricing_status(utility_service_id: str = None):
    """Return lightweight pricing service status for the Overview checklist."""
    from datetime import datetime, timezone
    
    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        
    if svc:
        snap = svc.get_snapshot()
        if snap and snap.fetched_at:
            now = datetime.now(timezone.utc)
            fetched = snap.fetched_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            seconds_ago = int((now - fetched).total_seconds())
            return {"ok": True, "last_fetch_seconds_ago": seconds_ago, "provider": snap.provider, "enabled": True}
            
    # No in-memory snapshot — check DB for latest row
    row = await db.get_latest_price(utility_service_id)
    if row and row.get("timestamp"):
        try:
            ts = datetime.fromisoformat(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            seconds_ago = int((datetime.now(timezone.utc) - ts).total_seconds())
            return {"ok": True, "last_fetch_seconds_ago": seconds_ago, "provider": row.get("provider", "unknown"), "enabled": True}
        except Exception:
            pass
    return {"ok": True, "last_fetch_seconds_ago": None, "provider": "none", "enabled": False}


@router.get("/pricing/current")
async def get_current_price(utility_service_id: str = None):
    """Return live in-memory price snapshot, falling back to DB."""
    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        
    if svc:
        snap = svc.get_snapshot()
        if snap:
            return {
                "ok": True,
                "data": snap.to_dict(),
                "utility_service_id": svc.utility_service_id
            }
            
    # Fallback to most recent DB record
    row = await db.get_latest_price(utility_service_id)
    if row:
        from src.services.pricing.base import PriceSnapshot, PricePeriod
        from datetime import datetime
        
        f_list = []
        try:
            arr = json.loads(row.get("forecast_json", "[]"))
            for j in arr:
                f_list.append(PricePeriod(
                    start=datetime.fromisoformat(j["start"]),
                    end=datetime.fromisoformat(j["end"]),
                    import_c_kwh=j.get("import_c_kwh", 0),
                    export_c_kwh=j.get("export_c_kwh"),
                    tariff_type=j.get("tariff_type", "UNKNOWN"),
                    renewables_pct=j.get("renewables_pct")
                ))
        except Exception as exc:
            logger.warning(f"Pricing: Failed to decode fallback forecast_json: {exc}")

        reconstructed = PriceSnapshot(
            provider=row.get("provider", "flat"),
            import_c_kwh=row.get("import_c_kwh", 0),
            export_c_kwh=row.get("export_c_kwh"),
            tariff_type=row.get("tariff_type", "UNKNOWN"),
            spike_status=row.get("spike_status", "NONE"),
            demand_window=bool(row.get("demand_window", 0)),
            renewables_pct=row.get("renewables_pct"),
            interval_min=row.get("interval_min", 30),
            valid_until=datetime.fromisoformat(row["valid_until"]) if row.get("valid_until") else None,
            fetched_at=datetime.fromisoformat(row["timestamp"]) if row.get("timestamp") else datetime.utcnow(),
            forecast=f_list,
        )
        return {
            "ok": True,
            "data": reconstructed.to_dict(),
            "source": "db",
            "utility_service_id": row.get("utility_service_id")
        }
    return {"ok": False, "error": "No price data available — configure a pricing provider"}


@router.get("/pricing/forecast")
async def get_forecast(utility_service_id: str = None):
    """Return the 24h forecast from the latest snapshot."""
    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        
    if svc:
        snap = svc.get_snapshot()
        if snap and snap.forecast:
            return {
                "ok": True,
                "provider": snap.provider,
                "forecast": snap.to_dict()["forecast"],
            }
    # Try DB
    row = await db.get_latest_price(utility_service_id)
    if row:
        try:
            forecast = json.loads(row.get("forecast_json", "[]"))
            return {"ok": True, "provider": row["provider"], "forecast": forecast, "source": "db"}
        except Exception:
            pass
    return {"ok": True, "provider": "none", "forecast": []}


@router.get("/pricing/history")
async def get_history(hours: int = 24, utility_service_id: str = None):
    """Return rolling price history from DB."""
    rows = await db.get_price_history(min(hours, 48), utility_service_id)
    return {"ok": True, "count": len(rows), "data": rows}


@router.get("/pricing/config")
async def get_pricing_config():
    """Return provider config with credentials redacted."""
    cfg = await db.get_pricing_config()
    if not cfg:
        return {"ok": True, "data": {"provider": "flat", "region": "AU", "enabled": False, "settings": {}}}
    # Redact credentials
    safe = dict(cfg)
    try:
        creds = json.loads(safe.get("credentials", "{}"))
    except Exception:
        creds = {}
    redacted = {k: "***" if v else "" for k, v in creds.items()}
    safe["credentials"] = redacted
    try:
        safe["settings"] = json.loads(safe.get("settings", "{}"))
    except Exception:
        pass
    return {"ok": True, "data": safe}


@router.post("/pricing/config")
async def save_pricing_config(req: PricingConfigRequest, utility_service_id: str = None):
    """DEPRECATED: Use /api/utility-services endpoint instead."""
    return {"ok": False, "error": "Deprecated. Use /api/utility-services endpoint instead."}


@router.post("/pricing/test")
async def test_pricing_connection(req: PricingConfigRequest):
    """Test the given provider configuration without saving.

    Ph-6C fix: if an emulation_mode flag is set in settings, return mock
    response immediately — no real API call is made. This makes the developer
    emulation checkbox functional during Test Connection.
    """
    from src.services.pricing.service import _build_adapter
    try:
        settings = dict(req.settings or {})

        # ── Emulation short-circuit (SC#1 fix) ─────────────────────────────
        if req.provider == "amber" and settings.get("amber_emulation_mode"):
            logger.info("pricing/test [amber]: emulation mode — returning mock response")
            return {"ok": True, "data": {
                "ok":           True,
                "message":      f"\u2713 Emulation mode — connected to mock site {_EMULATION_SITE['site_id']}",
                "site_id":      _EMULATION_SITE["site_id"],
                "nmi":          _EMULATION_SITE["nmi"],
                "network":      _EMULATION_SITE["network"],
                "import_c_kwh": 16.0,
                "export_c_kwh": -6.5,
                "emulation":    True,
            }}

        if req.provider == "localvolts" and settings.get("localvolts_emulation_mode"):
            logger.info("pricing/test [localvolts]: emulation mode — returning mock response")
            return {"ok": True, "data": {
                "ok":           True,
                "message":      "\u2713 Emulation mode — LocalVolts P2P mock active",
                "import_c_kwh": _LV_EMULATION_SNAPSHOT["import_c_kwh"],
                "export_c_kwh": _LV_EMULATION_SNAPSHOT["export_c_kwh"],
                "region":       "AU",
                "emulation":    True,
            }}

        if req.provider == "aemo" and settings.get("aemo_emulation_mode"):
            logger.info("pricing/test [aemo]: emulation mode — returning mock response")
            return {"ok": True, "data": {
                "ok":           True,
                "message":      "\u2713 Emulation mode — AEMO NEM mock active (NSW1)",
                "region":       settings.get("region", "NSW1"),
                "import_c_kwh": _AEMO_EMULATION_SNAPSHOT["import_c_kwh"],
                "emulation":    True,
            }}

        if req.provider == "comed" and settings.get("comed_emulation_mode"):
            logger.info("pricing/test [comed]: emulation mode — returning mock response")
            return {"ok": True, "data": {
                "ok":           True,
                "message":      "\u2713 Emulation mode — ComEd Hourly mock active",
                "region":       "COMED",
                "import_c_kwh": _COMED_EMULATION_SNAPSHOT["import_c_kwh"],
                "emulation":    True,
            }}
        # ───────────────────────────────────────────────────────────────────

        # AEMO: ensure region is always set even if frontend sends empty settings
        if req.provider == "aemo" and not settings.get("region"):
            settings["region"] = "nsw"
        cfg = {
            "provider": req.provider,
            "credentials": req.credentials,
            "settings": settings,
        }
        adapter = _build_adapter(cfg)
        result = await adapter.test_connection()
        logger.info(f"pricing/test [{req.provider}]: ok={result['ok']} msg={result.get('message','')[:120]}")
        return {"ok": result["ok"], "data": result}
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        logger.warning(f"pricing/test [{req.provider}] exception: {err_msg}")
        return {"ok": False, "error": err_msg}


@router.post("/pricing/refresh")
async def force_refresh(utility_service_id: str = None):
    """Force an immediate price poll."""
    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        
    if svc:
        snap = await svc.force_refresh()
        if snap:
            return {"ok": True, "data": snap.to_dict()}
    return {"ok": False, "error": "Pricing not enabled or provider config missing"}


@router.get("/pricing/models")
async def get_pricing_models():
    """Get all pricing models with credentials, settings, and computed statuses."""
    try:
        models = await db.get_all_pricing_models()
        statuses = await db.get_pricing_model_status_metadata()
        for m in models:
            m_id = m["id"]
            m["status"] = statuses.get(m_id, "Unavailable")
            # Safe json loads for credentials and settings
            for key in ("credentials", "settings"):
                val = m.get(key)
                if isinstance(val, str):
                    try:
                        m[key] = json.loads(val)
                    except Exception:
                        m[key] = {}
        return {"ok": True, "models": models}
    except Exception as exc:
        logger.exception("Failed to fetch pricing models")
        return {"ok": False, "error": str(exc)}


@router.put("/pricing/models/{model_id}")
async def update_pricing_model(model_id: str, req: PricingModelUpdateRequest):
    """Update credentials and settings for a pricing model, then restart dependent pricing services."""
    try:
        existing = await db.get_pricing_model(model_id)
        if not existing:
            return {"ok": False, "error": f"Pricing model '{model_id}' not found"}

        await db.upsert_pricing_model(model_id, req.credentials, req.settings)
        await pricing_registry.restart_services_for_model(model_id)
        return {"ok": True, "message": f"Pricing model '{model_id}' updated"}
    except Exception as exc:
        logger.exception(f"Failed to update pricing model {model_id}")
        return {"ok": False, "error": str(exc)}


@router.post("/pricing/models/{model_id}/test")
async def test_pricing_model_connection(model_id: str, req: PricingModelUpdateRequest):
    """Test connection for a pricing model with transient credentials and settings."""
    test_req = PricingConfigRequest(
        provider=model_id,
        credentials=req.credentials,
        settings=req.settings
    )
    return await test_pricing_connection(test_req)


@router.post("/pricing/models/{model_id}/use-for-gateway/{gateway_id}")
async def use_pricing_model_for_gateway(model_id: str, gateway_id: str):
    """One-click activation: make a gateway use this pricing model.

    Resolves the gateway's current utility-service binding and either:
      (a) updates its `pricing_model_id` to `model_id` (preserving tariff /
          billing / metering details), or
      (b) creates a minimal utility service with this provider and links it
          to the gateway, when no service exists yet.

    Eliminates the 3-tab journey users previously had to make (Setup →
    Services → Overview) to switch active providers.
    """
    try:
        model = await db.get_pricing_model(model_id)
        if not model:
            return {"ok": False, "error": f"Pricing model '{model_id}' not found"}

        links = await db.get_gateway_utility_links()
        existing_svc_ids = links.get(gateway_id, [])

        if existing_svc_ids:
            svc_id = existing_svc_ids[0]
            svc = await db.get_utility_service(svc_id)
            if not svc:
                return {"ok": False, "error": f"Utility service '{svc_id}' not found"}

            old_model = svc.get("pricing_model_id") or svc.get("pricing_provider")
            if old_model == model_id:
                return {
                    "ok": True,
                    "no_change": True,
                    "message": f"{svc.get('name')} already uses {model.get('name', model_id)}",
                    "service_id": svc_id,
                    "service_name": svc.get("name"),
                }

            await db.upsert_utility_service({
                "id": svc_id,
                "name": svc.get("name"),
                "pricing_model_id": model_id,
                "pricing_provider": model_id,
            })
            await pricing_registry.restart_service(svc_id)
            await db.write_utility_service_audit(
                svc_id, "updated", actor="ui",
                detail=f"Switched pricing_model_id: {old_model} → {model_id} (via Use For Gateway)"
            )
            logger.info(f"use-for-gateway: gw={gateway_id} svc={svc_id} model {old_model} → {model_id}")
            return {
                "ok": True,
                "message": f"{svc.get('name')} now uses {model.get('name', model_id)}",
                "service_id": svc_id,
                "service_name": svc.get("name"),
                "switched_from": old_model,
                "switched_to": model_id,
            }

        # No existing service — create a minimal one and link it
        import uuid
        new_id = str(uuid.uuid4())[:8]
        svc_name = f"{model.get('name', model_id.title())} Service"
        await db.upsert_utility_service({
            "id": new_id,
            "name": svc_name,
            "pricing_provider": model_id,
            "pricing_model_id": model_id,
            "pricing_credentials": "{}",
            "pricing_settings": "{}",
        })
        await db.link_gateway_to_utility_service(gateway_id, new_id)
        await pricing_registry.restart_service(new_id)
        await db.write_utility_service_audit(
            new_id, "created", actor="ui",
            detail=f"Auto-created via Use For Gateway (provider={model_id}, gateway={gateway_id})"
        )
        logger.info(f"use-for-gateway: gw={gateway_id} new_svc={new_id} model={model_id} (auto-created)")
        return {
            "ok": True,
            "created": True,
            "message": f"Created '{svc_name}' and linked to {gateway_id}",
            "service_id": new_id,
            "service_name": svc_name,
        }
    except Exception as exc:
        logger.exception(f"use-for-gateway failed: model={model_id} gw={gateway_id}")
        return {"ok": False, "error": str(exc)}


@router.get("/utility-services")
async def get_all_utility_services():
    """Return all utility services with their gateway assignments (multi-link aware)."""
    services = await db.get_all_utility_services()
    # get_gateway_utility_links now returns {gw_id: [svc_id, ...]}
    links = await db.get_gateway_utility_links()
    # Build reverse index: {svc_id: [gw_id, ...]}
    svc_to_gws: dict[str, list[str]] = {}
    for gw, svc_ids in links.items():
        for sid in svc_ids:
            svc_to_gws.setdefault(sid, []).append(gw)
    for svc in services:
        svc["gateways"] = svc_to_gws.get(svc["id"], [])
    return {"ok": True, "data": services}


@router.post("/utility-services")
async def create_utility_service(req: dict):
    """Create a new utility service.

    Accepts the full service payload as a raw dict (same shape as PUT /utility-services/{id}).
    Using dict instead of a Pydantic model prevents field-stripping issues where the model
    silently dropped critical fields such as 'name', 'pricing_provider', 'pricing_credentials',
    and 'pricing_settings' (DEF-PB-02/03).
    """
    try:
        # Pull gateway_ids out before writing — handled separately via link table
        gateway_ids: list = req.pop("gateway_ids", []) or req.pop("gateways", []) or []

        await db.upsert_utility_service(req)

        # Fetch back the created record so we have the auto-generated id
        service_id = req.get("id")
        if not service_id:
            # id was generated inside upsert; try to look it up by name
            all_svcs = await db.get_all_utility_services()
            name = req.get("name") or req.get("utility_name")
            match = next((s for s in all_svcs if s.get("name") == name), None)
            if match:
                service_id = match["id"]

        # Assign gateways if provided in the creation request
        if service_id and gateway_ids:
            links = await db.get_gateway_utility_links()
            for gw in gateway_ids:
                other_svcs = [sid for sid in links.get(gw, []) if sid != service_id]
                if other_svcs:
                    return {"ok": False, "error": f"Gateway '{gw}' already assigned to another Service."}
            for gw in gateway_ids:
                await db.link_gateway_to_utility_service(gw, service_id)

        await pricing_registry.start_all()
        await db.write_utility_service_audit(service_id or "unknown", "created", actor="ui",
                                              detail="Initial creation via UI")
        return {"ok": True, "message": "Utility service created", "id": service_id}
    except Exception as exc:
        logger.exception("Failed to create utility service")
        return {"ok": False, "error": str(exc)}


@router.put("/utility-services/{service_id}")
async def update_utility_service(service_id: str, req: dict):
    """Update a utility service."""
    try:
        # Capture old record for audit diff
        old = await db.get_utility_service(service_id) or {}
        req["id"] = service_id
        await db.upsert_utility_service(req)
        # The v58 tariff fields are written separately — see
        # db.update_utility_service_tariff for why they are kept out of the
        # COALESCE upsert.
        tariff_written = await db.update_utility_service_tariff(service_id, req)
        await pricing_registry.restart_service(service_id)
        # Audit — record which top-level fields changed
        changed = [k for k in req if k not in ("id",) and req.get(k) != old.get(k)]
        detail = f"Fields changed: {', '.join(changed)}" if changed else "No field changes"
        if tariff_written:
            detail += f" | tariff: {', '.join(tariff_written)}"
        await db.write_utility_service_audit(service_id, "updated", actor="ui",
                                              detail=detail)
        return {"ok": True, "message": "Utility service updated"}
    except Exception as exc:
        logger.exception("Failed to update utility service")
        return {"ok": False, "error": str(exc)}


@router.get("/utility-services/{service_id}/standing-charges")
async def get_standing_charges(service_id: str):
    """Standing charges beyond the three fixed columns.

    supply_charge_day, metering_fee and network_fixed_fee could not name a
    membership or connection fee, so plans carrying one had nowhere to put it
    and it silently went uncounted.
    """
    return {"charges": await db.list_standing_charges(service_id)}


@router.post("/utility-services/{service_id}/standing-charges")
async def create_standing_charge(service_id: str, payload: dict):
    label = (payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="A label is required.")
    try:
        amount = float(payload.get("amount_c") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount_c must be a number.")
    basis = payload.get("basis") or "per_day"
    if basis not in ("per_day", "per_period"):
        raise HTTPException(status_code=400, detail="basis must be per_day or per_period.")

    charge_id = await db.add_standing_charge(
        service_id, label, amount, basis, payload.get("months"))
    await db.write_utility_service_audit(
        service_id, "updated", actor="ui",
        detail=f"Standing charge added: {label!r} {amount:g}c {basis}")
    return {"ok": True, "id": charge_id}


@router.delete("/utility-services/{service_id}/standing-charges/{charge_id}")
async def remove_standing_charge(service_id: str, charge_id: int):
    removed = await db.delete_standing_charge(service_id, charge_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Standing charge not found for this service.")
    await db.write_utility_service_audit(
        service_id, "updated", actor="ui",
        detail=f"Standing charge #{charge_id} removed")
    return {"ok": True}


@router.delete("/utility-services/{service_id}")
async def delete_utility_service(service_id: str):
    """Delete a utility service."""
    try:
        old = await db.get_utility_service(service_id) or {}
        await db.delete_utility_service(service_id)
        await pricing_registry.stop_service(service_id)
        await db.write_utility_service_audit(
            service_id, "deleted", actor="ui",
            detail=f"Deleted service '{old.get('name', service_id)}'"
        )
        return {"ok": True, "message": "Utility service deleted"}
    except Exception as exc:
        logger.exception("Failed to delete utility service")
        return {"ok": False, "error": str(exc)}


@router.put("/utility-services/{service_id}/gateways")
async def link_gateways(service_id: str, gateways: list[str]):
    """Set gateways assigned to this utility service (composite PK aware).
    Gateways not in the new list have their link to THIS service removed
    (they may retain links to other services — CT Split Grid support).
    """
    try:
        links = await db.get_gateway_utility_links()  # {gw: [svc_id, ...]}
        # Validation: prevent assigning a gateway already mapped to a different service
        for gw in gateways:
            other_svcs = [sid for sid in links.get(gw, []) if sid != service_id]
            if other_svcs:
                return {"ok": False, "error": f"Gateway '{gw}' already assigned to another Service."}

        # Remove links for gateways that were previously on this service but are no longer
        for gw, svc_ids in links.items():
            if service_id in svc_ids and gw not in gateways:
                await db.unlink_gateway_from_utility_service(gw, service_id)
        # Add / update new assignments
        for gw in gateways:
            await db.link_gateway_to_utility_service(gw, service_id)
        return {"ok": True, "message": "Gateways assigned"}
    except Exception as exc:
        logger.exception("Failed to assign gateways")
        return {"ok": False, "error": str(exc)}


@router.patch("/utility-services/{service_id}/gateways/{gw_id}/phase")
async def set_gateway_phase_for_service(service_id: str, gw_id: str, body: dict):
    """Set the phase assignment (L1/L2/L3) for a gateway within a specific utility service.
    Only meaningful for multi-gateway 3-phase installs where ac_wiring_type=3.
    Body: { "gateway_phase": "L1" | "L2" | "L3" | null }
    """
    try:
        phase = body.get("gateway_phase") or None
        await db.link_gateway_to_utility_service(gw_id, service_id, gateway_phase=phase)
        return {"ok": True, "gateway_phase": phase}
    except Exception as exc:
        logger.exception("Failed to set gateway phase")
        return {"ok": False, "error": str(exc)}


@router.get("/gateway-sites")
async def get_gateway_sites():
    """Group registered gateways by FWH Cloud site_id and group_id.
    Returns a list of sites, each containing groups, each containing gateways.
    Used by Services → Sites/Gateways sub-tab.
    """
    try:
        import json as _json
        gateways = await db.get_all_gateways()
        sites: dict = {}
        for gw in gateways:
            # Enrich from profile_json (mirrors api_gateways.py hydration)
            try:
                profile = _json.loads(gw.get("profile_json") or "{}")
            except Exception:
                profile = {}
            site_name = profile.get("site_name") or f"Site {gw.get('site_id', '?')}"
            # Live telemetry sourced from profile snapshot (last known state)
            live = {
                "grid_connection_state": profile.get("grid_connection_state"),
                "accessories":           profile.get("accessories", []),
                "vpp_enrolled":          profile.get("vpp_enrolled", False),
            }

            sid  = str(gw.get("site_id")  or "unknown")
            gid  = str(gw.get("group_id") or "ungrouped")
            gname = gw.get("group_name") or "(ungrouped)"
            if sid not in sites:
                sites[sid] = {"site_id": sid, "site_name": site_name, "groups": {}}
            grp = sites[sid]["groups"]
            if gid not in grp:
                grp[gid] = {"group_id": gid, "group_name": gname, "gateways": []}
            grp[gid]["gateways"].append({
                "short_id":           gw.get("short_id"),
                "name":               gw.get("name"),
                "model":              gw.get("model"),
                # Profile-sourced flags
                "has_solar":          profile.get("has_solar", False),
                "has_smart_circuits": profile.get("has_smart_circuits", False),
                "has_generator":      profile.get("has_generator", False),
                "has_apbox":          profile.get("has_apbox", gw.get("has_apbox", False)),
                "has_ahub":           profile.get("has_ahub", gw.get("has_ahub", False)),
                # v23 feature flag DB columns
                "has_ct_split_grid":  bool(gw.get("has_ct_split_grid")),
                "has_ct_split_pv":    bool(gw.get("has_ct_split_pv")),
                "has_three_phase":    bool(gw.get("has_three_phase")),
                "has_v2l":            bool(gw.get("has_v2l")),
                # Live
                "grid_connection_state": live.get("grid_connection_state"),
                "accessories":           [acc for acc in live.get("accessories", []) if acc != "mac1"] if gw.get("grid_type") == "off_grid" else live.get("accessories", []),
                "vpp_enrolled":          live.get("vpp_enrolled", False),
                # Wiring (from gateways table)
                "service_amps":         gw.get("service_amps"),
                "grid_type":            gw.get("grid_type"),
                "gateway_phase":        gw.get("gateway_phase"),
                "three_phase_group_id": gw.get("three_phase_group_id"),
            })
        # Attach utility service links
        links    = await db.get_gateway_utility_links()   # {gw_id: [svc_id, ...]}
        all_svcs = await db.get_all_utility_services()
        svc_map  = {s["id"]: s.get("name") or s.get("pricing_provider") for s in all_svcs}
        for site in sites.values():
            for grp in site["groups"].values():
                for gw_entry in grp["gateways"]:
                    gw_links = links.get(gw_entry["short_id"], [])
                    gw_entry["utility_service_ids"]   = gw_links
                    gw_entry["utility_service_names"] = [svc_map.get(s, s) for s in gw_links]
        return {
            "ok": True,
            "sites": [
                {**s, "groups": list(s["groups"].values())}
                for s in sorted(sites.values(), key=lambda x: x["site_id"])
            ]
        }
    except Exception as exc:
        logger.exception("Failed to get gateway sites")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Dashboard summary (for end-user /home view)
# ---------------------------------------------------------------------------

@router.get("/pricing/dashboard")
async def get_dashboard(utility_service_id: str = None):
    """Aggregate current price + utility info for the end-user dashboard."""
    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
        util = await db.get_utility_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        services = await db.get_all_utility_services()
        util = services[0] if services else {}
        
    snap = svc.get_snapshot() if svc else None
    latest = await db.get_latest_price(utility_service_id)

    price_data = snap.to_dict() if snap else (latest or {})
    return {
        "ok": True,
        "price":   price_data,
        "utility": util or {},
    }


# ---------------------------------------------------------------------------
# Amber-specific routes
# ---------------------------------------------------------------------------

_EMULATION_SITE = {
    "site_id":         "01F5A5CRKMZ5BCX9P1S4V990AM",
    "nmi":             "3052282872",
    "network":         "Jemena",
    "status":          "active",
    "interval_length": 5,
    "active_from":     "2022-01-01",
    "channels": [
        {"identifier": "E1", "type": "general",   "tariff": "A100"},
        {"identifier": "B1", "type": "feedIn",     "tariff": "A100"},
    ],
}

_EMULATION_USAGE: dict = {
    "general": [
        {"start_time": "2026-04-17T00:00:00+10:00", "end_time": "2026-04-17T00:30:00+10:00",
         "kwh": 0.35, "cost": 0.06, "renewables": 55, "tariff_type": "OFFPEAK", "quality": "billable"},
        {"start_time": "2026-04-17T00:30:00+10:00", "end_time": "2026-04-17T01:00:00+10:00",
         "kwh": 0.20, "cost": 0.03, "renewables": 60, "tariff_type": "OFFPEAK", "quality": "billable"},
    ],
    "feed_in": [
        {"start_time": "2026-04-17T06:00:00+10:00", "end_time": "2026-04-17T06:30:00+10:00",
         "kwh": -3.9, "cost": -0.02, "renewables": 55, "tariff_type": "OFFPEAK", "quality": "billable"},
    ],
}

_EMULATION_PRICES: dict = {
    "general": [
        {"start_time": f"2026-04-17T{h:02d}:{m:02d}:00+10:00",
         "end_time":   f"2026-04-17T{h:02d}:{(m+5)%60:02d}:00+10:00",
         "per_kwh": 16.0 + (h - 12) * 0.3,
         "spot_per_kwh": 6.12,
         "renewables": 4 + (h % 8),
         "tariff_type": "OFFPEAK" if h < 7 or h > 22 else "SHOULDER",
         "type": "ActualInterval"}
        for h in range(0, 24) for m in range(0, 60, 5)
    ],
    "feed_in": [],
}


def _is_amber_emulation() -> bool:
    """Return True if amber_emulation_mode is enabled in pricing config."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        cfg = loop.run_until_complete(db.get_pricing_config()) if not loop.is_running() else None
        if cfg is None:
            return False
        import json
        settings = json.loads(cfg.get("settings", "{}") or "{}")
        return bool(settings.get("amber_emulation_mode", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ph-6C — LocalVolts emulation data
# ---------------------------------------------------------------------------

# Synthetic 24h forecast (288 × 5-min slots) shaped like a real AU NEM day:
# negative-export midday solar glut, evening peak ramp, off-peak overnight.
# Real LocalVolts API returns forecast records via the same /interval endpoint
# (distinguished by `quality: Forecast`); see references in the
# README/coordinator for github.com/gurrier/localvolts and
# github.com/melvanderwal/HA-Localvolts. That real-API plumbing is tracked
# separately — this synthetic set just gives the UI something to render in
# emulation mode so users can validate the downstream pipeline.
import math as __math_lv

def _lv_import(h: int, m: int) -> float:
    """Approximate AU NEM import shape: low overnight, dip midday (solar glut
    drives wholesale negative), evening peak ~17–21h."""
    t = h + m / 60.0
    base = 8.5
    # Solar midday dip (centred ~12:30, depth ~7¢)
    solar_dip = -7.0 * max(0.0, __math_lv.cos((t - 12.5) * __math_lv.pi / 8))
    # Evening peak hump (centred ~18:30, height ~14¢)
    evening = 14.0 * max(0.0, __math_lv.cos((t - 18.5) * __math_lv.pi / 4))
    return round(base + solar_dip + evening, 2)

def _lv_export(h: int, m: int) -> float:
    """Export earnings — typically lower than import; negative midday during
    solar glut (you pay to export); positive in the evening peak."""
    t = h + m / 60.0
    base = 4.0
    solar_dip = -10.0 * max(0.0, __math_lv.cos((t - 12.5) * __math_lv.pi / 8))
    evening = 8.0 * max(0.0, __math_lv.cos((t - 18.5) * __math_lv.pi / 4))
    return round(base + solar_dip + evening, 2)

def _lv_tariff(imp_c: float) -> str:
    if imp_c < 5:    return "OFFPEAK"
    if imp_c < 15:   return "SHOULDER"
    if imp_c < 30:   return "PEAK"
    return "SPIKE"

# 48 × 30-min slots (matches AEMO/ComEd emulation density). Earlier 5-min
# 288-slot version blew past the SD forecast endpoint's eval loop budget
# (~60s+ with 288 × 7 evaluators × per-period DB lookups). Real LocalVolts
# is 5-min granular, but emulation doesn't need to match — the engine can
# resample if needed. 30-min keeps the UI strip readable too.
_LV_EMULATION_FORECAST = [
    {
        "start_time":    f"2026-06-17T{h:02d}:{m:02d}:00+10:00",
        "end_time":      f"2026-06-17T{(h + (1 if m == 30 else 0)) % 24:02d}:{(m + 30) % 60:02d}:00+10:00",
        "import_c_kwh":  _lv_import(h, m),
        "export_c_kwh":  _lv_export(h, m),
        "tariff_type":   _lv_tariff(_lv_import(h, m)),
        "renewables_pct": None,
        "spike_status":  "SPIKE" if _lv_import(h, m) >= 30 else "NONE",
        "demand_window": False,
        "interval_min":  30,
    }
    for h in range(0, 24) for m in (0, 30)
]

_LV_EMULATION_SNAPSHOT = {
    "provider":        "localvolts",
    "import_c_kwh":    8.44,
    "export_c_kwh":    4.20,
    "tariff_type":     "OFFPEAK",
    "spike_status":    "NONE",
    "renewables_pct":  None,
    "interval_min":    5,
    "forecast":        _LV_EMULATION_FORECAST,
    "region":          "AU",
    "currency":        "AUD",
    "source":          "emulation",
}

_LV_EMULATION_VPP = {
    "vpp_enrolled":     False,
    "network_operator": None,
    "note": "LocalVolts is a P2P pricing provider. VPP is via FWH get_programme_info.",
}


def _is_lv_emulation() -> bool:
    """Return True if localvolts_emulation_mode is enabled in pricing config."""
    import asyncio as _asyncio, json as _json
    try:
        loop = _asyncio.get_event_loop()
        cfg = loop.run_until_complete(db.get_pricing_config()) if not loop.is_running() else None
        if cfg is None:
            return False
        settings = _json.loads(cfg.get("settings", "{}") or "{}")
        return bool(settings.get("localvolts_emulation_mode", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ph-6C — AEMO emulation data (30-min NEM wholesale intervals)
# ---------------------------------------------------------------------------

import math as _math

_AEMO_BASE = 9.13


def _aemo_import(h: int, m: int) -> float:
    return round(
        _AEMO_BASE
        + 8 * _math.sin((h + m / 60.0 - 9) * _math.pi / 8)
        + (3 if 17 <= h <= 20 else 0),
        4,
    )


_AEMO_EMULATION_FORECAST = [
    {
        "start_time":    f"2026-05-02T{h:02d}:{m:02d}:00+10:00",
        "end_time":      f"2026-05-02T{h:02d}:{(m + 30) % 60:02d}:00+10:00",
        "import_c_kwh":  _aemo_import(h, m),
        "export_c_kwh":  None,
        "tariff_type":   "PEAK" if 17 <= h <= 20 else ("SHOULDER" if 7 <= h <= 22 else "OFFPEAK"),
        "renewables_pct": max(0, min(100, 20 + int(40 * _math.sin((h - 6) * _math.pi / 12)))),
        "spike_status":  "NONE",
        "demand_window": False,
        "interval_min":  30,
    }
    for h in range(0, 24) for m in (0, 30)
]

_AEMO_EMULATION_SNAPSHOT = {
    "provider":       "aemo",
    "import_c_kwh":   _AEMO_BASE,
    "export_c_kwh":   None,
    "tariff_type":    "SHOULDER",
    "spike_status":   "NONE",
    "renewables_pct": 42,
    "interval_min":   30,
    "forecast":       _AEMO_EMULATION_FORECAST,
    "region":         "NSW1",
    "currency":       "AUD",
    "source":         "emulation",
}


def _is_aemo_emulation() -> bool:
    """Return True if aemo_emulation_mode is enabled in pricing config."""
    import asyncio as _asyncio, json as _json
    try:
        loop = _asyncio.get_event_loop()
        cfg = loop.run_until_complete(db.get_pricing_config()) if not loop.is_running() else None
        if cfg is None:
            return False
        settings = _json.loads(cfg.get("settings", "{}") or "{}")
        return bool(settings.get("aemo_emulation_mode", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ph-6C — ComEd emulation data (hourly Chicago wholesale)
# ---------------------------------------------------------------------------

_COMED_BASE = 5.70


def _comed_import(h: int) -> float:
    return round(
        _COMED_BASE
        + 4 * _math.sin((h - 8) * _math.pi / 8)
        + (2 if 8 <= h <= 20 else 0),
        4,
    )


_COMED_EMULATION_FORECAST = [
    {
        "start_time":    f"2026-05-02T{h:02d}:00:00-05:00",
        "end_time":      f"2026-05-02T{(h + 1) % 24:02d}:00:00-05:00",
        "import_c_kwh":  _comed_import(h),
        "export_c_kwh":  None,
        "tariff_type":   "PEAK" if 14 <= h <= 19 else ("SHOULDER" if 7 <= h <= 22 else "OFFPEAK"),
        "renewables_pct": None,
        "spike_status":  "NONE",
        "demand_window": False,
        "interval_min":  60,
    }
    for h in range(0, 24)
]

_COMED_EMULATION_SNAPSHOT = {
    "provider":       "comed",
    "import_c_kwh":   _COMED_BASE,
    "export_c_kwh":   None,
    "tariff_type":    "OFFPEAK",
    "spike_status":   "NONE",
    "renewables_pct": None,
    "interval_min":   60,
    "forecast":       _COMED_EMULATION_FORECAST,
    "region":         "COMED",
    "currency":       "USD",
    "source":         "emulation",
}


def _is_comed_emulation() -> bool:
    """Return True if comed_emulation_mode is enabled in pricing config."""
    import asyncio as _asyncio, json as _json
    try:
        loop = _asyncio.get_event_loop()
        cfg = loop.run_until_complete(db.get_pricing_config()) if not loop.is_running() else None
        if cfg is None:
            return False
        settings = _json.loads(cfg.get("settings", "{}") or "{}")
        return bool(settings.get("comed_emulation_mode", False))
    except Exception:
        return False


# Mock VPP context for Amber emulation (Ausgrid NSW — most common Amber VPP network)
_AMBER_EMULATION_VPP = {
    "vpp_enrolled":       False,
    "network_operator":   "Ausgrid",
    "state":              "NSW",
    "amber_vpp_eligible": True,
    "note": "Mock: Amber Electric via Ausgrid NSW. VPP enrolment via FWH get_programme_info.",
}


async def _get_amber_adapter(utility_service_id: str = None):
    """Resolve the live AmberAdapter from the running pricing service.

    Raises RuntimeError if provider is not amber or adapter not initialised.
    """
    from src.services.pricing.service import pricing_registry, _build_adapter
    import json

    if utility_service_id:
        svc = pricing_registry.get_service(utility_service_id)
    else:
        svc = pricing_registry.get_primary_service()
        
    # Use the live adapter if running and is amber
    adapter = getattr(svc, '_adapter', None) if svc else None
    if adapter is not None and hasattr(adapter, 'get_site_info'):
        return adapter

    # Otherwise build a fresh one from DB config
    if utility_service_id:
        cfg = await db.get_utility_service(utility_service_id)
    else:
        services = await db.get_all_utility_services()
        cfg = services[0] if services else None
        
    if not cfg or cfg.get("pricing_provider") != "amber":
        raise RuntimeError("Amber pricing not configured")
    raw_creds = json.loads(cfg.get("pricing_credentials", "{}") or "{}")
    built = _build_adapter({
        "provider":     "amber",
        "credentials":  raw_creds,
        "settings":     json.loads(cfg.get("pricing_settings", "{}") or "{}"),
    })
    return built


@router.get("/pricing/amber/site")
async def get_amber_site():
    """Return Amber site metadata (NMI, network, channels, interval length)."""
    cfg = await db.get_pricing_config()
    import json
    settings = json.loads((cfg or {}).get("settings", "{}") or "{}")
    if settings.get("amber_emulation_mode"):
        return {"ok": True, "data": _EMULATION_SITE, "emulation": True}
    try:
        adapter = await _get_amber_adapter()
        data = await adapter.get_site_info()
        return {"ok": True, "data": data}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("amber/site failed")
        return {"ok": False, "error": str(exc)}


@router.get("/pricing/amber/usage")
async def get_amber_usage(start_date: str, end_date: str):
    """Return Amber usage data for a date range (max 7 days)."""
    cfg = await db.get_pricing_config()
    import json
    settings = json.loads((cfg or {}).get("settings", "{}") or "{}")
    if settings.get("amber_emulation_mode"):
        return {"ok": True, "data": _EMULATION_USAGE, "emulation": True,
                "start_date": start_date, "end_date": end_date}
    try:
        adapter = await _get_amber_adapter()
        data = await adapter.get_usage(start_date, end_date)
        return {"ok": True, "data": data, "start_date": start_date, "end_date": end_date}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("amber/usage failed")
        return {"ok": False, "error": str(exc)}


@router.get("/pricing/amber/prices")
async def get_amber_prices(start_date: str, end_date: str):
    """Return Amber historical price data for a date range (max 7 days)."""
    cfg = await db.get_pricing_config()
    import json
    settings = json.loads((cfg or {}).get("settings", "{}") or "{}")
    if settings.get("amber_emulation_mode"):
        return {"ok": True, "data": _EMULATION_PRICES, "emulation": True,
                "start_date": start_date, "end_date": end_date}
    try:
        adapter = await _get_amber_adapter()
        data = await adapter.get_prices(start_date, end_date)
        return {"ok": True, "data": data, "start_date": start_date, "end_date": end_date}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("amber/prices failed")
        return {"ok": False, "error": str(exc)}


# ── Ph-2c: Utility Service Windows CRUD ─────────────────────────────────────

@router.get("/utility-services/{service_id}/windows")
async def get_utility_windows(service_id: str):
    """Return all time windows for a utility service (demand, export, discharge)."""
    rows = await db.get_utility_service_windows(service_id)
    return {"ok": True, "data": rows}


@router.post("/utility-services/{service_id}/windows")
async def create_utility_window(service_id: str, req: dict):
    """Add a new time window to a utility service.

    Body: { window_type, label, start_time, end_time, day_type, months, rate }
      window_type: 'demand' | 'export' | 'discharge'
      start_time / end_time: 'HH:MM'
      day_type: 'all' | 'weekdays' | 'weekends' | 'everyday'
      months: '10,11,12,1,2' or null for all year
      rate: float (CENTS, always POSITIVE: c/kW for demand, c/kWh for export.
            A demand rate's time dimension comes from the service's
            demand_charge_basis, not from the rate itself.)
      rate_kind: 'credit' | 'charge' — which way the money goes. A two-way
            export tariff is 'charge'. Sign is NOT used: it is reserved for
            dynamic pricing, where a negative price is real market data.
    """
    # Validate required fields
    window_type = req.get("window_type")
    if window_type not in ("demand", "export", "discharge"):
        return {"ok": False, "error": "window_type must be 'demand', 'export', or 'discharge'"}
    if not req.get("start_time") or not req.get("end_time"):
        return {"ok": False, "error": "start_time and end_time are required (HH:MM)"}

    # Direction is a field, never a sign. Sign is reserved for dynamic pricing,
    # where a negative price is real market data — refusing it here stops the
    # two meanings colliding in one column.
    rate_kind = (req.get("rate_kind") or "credit").lower()
    if rate_kind not in ("credit", "charge"):
        return {"ok": False, "error": "rate_kind must be 'credit' or 'charge'"}
    if window_type == "demand":
        rate_kind = "charge"       # a demand window has no credit reading
    req["rate_kind"] = rate_kind

    try:
        rate_value = float(req.get("rate") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "rate must be a number"}
    if rate_value < 0:
        return {
            "ok": False,
            "error": (
                "Enter the rate as it appears on your bill — a positive number. "
                "To make it a charge rather than a credit, set rate_kind to "
                "'charge'. Negative rates are reserved for dynamic pricing, "
                "where the market price itself can go below zero."
            ),
        }
    try:
        window_id = await db.insert_utility_service_window(service_id, req)
        await db.write_utility_service_audit(
            service_id, "updated", actor="ui",
            detail=f"Added {window_type} window '{req.get('label', '')}' {req.get('start_time')}–{req.get('end_time')}"
        )
        return {"ok": True, "id": window_id, "message": "Window added"}
    except Exception as exc:
        logger.exception(f"Failed to add window to service {service_id}")
        return {"ok": False, "error": str(exc)}


@router.delete("/utility-services/{service_id}/windows/{window_id}")
async def delete_utility_window(service_id: str, window_id: int):
    """Delete a specific time window from a utility service."""
    try:
        await db.delete_utility_service_window(window_id, service_id)
        await db.write_utility_service_audit(
            service_id, "updated", actor="ui",
            detail=f"Deleted window #{window_id}"
        )
        return {"ok": True, "message": "Window deleted"}
    except Exception as exc:
        logger.exception(f"Failed to delete window {window_id} from service {service_id}")
        return {"ok": False, "error": str(exc)}


# ── Ph-2d: Utility Service Audit Log ────────────────────────────────────────

@router.get("/utility-services/{service_id}/audit")
async def get_utility_audit(service_id: str, limit: int = 20):
    """Return the audit trail for a utility service (most recent first)."""
    rows = await db.get_utility_service_audit(service_id, limit=min(limit, 100))
    return {"ok": True, "data": rows}


# ── Ph-2e: Tariff Profile Sync (store validated data back to utility service) ──

@router.post("/utility-services/{service_id}/tariff-profile")
async def sync_tariff_profile(service_id: str, req: dict):
    """Store a validated tariff profile snapshot back to the utility service record.

    Called by the UI after a successful 'Refresh from gateway' action.
    Stores tariff_type, nem_type, tariff_company_id, tariff_company_name,
    fwh_site_id, fwh_site_name, and sets tariff_validated_at to now.

    Body: the response from GET /api/gateways/{id}/tariff-profile
    """
    try:
        from datetime import datetime, timezone
        update = {
            "id":                   service_id,
            "tariff_type":          req.get("tariff_type"),
            "nem_type":             req.get("nem_type"),
            "tariff_company_id":    req.get("tariff_company_id"),
            "tariff_company_name":  req.get("tariff_company"),
            "fwh_site_id":          req.get("fwh_site_id"),
            "fwh_site_name":        req.get("fwh_site_name"),
            "tariff_validated_at":  datetime.now(timezone.utc).isoformat(),
        }
        # Strip None values — don't overwrite existing data with nulls
        update = {k: v for k, v in update.items() if v is not None}
        await db.upsert_utility_service(update)
        await db.write_utility_service_audit(
            service_id, "validated", actor="ui",
            detail=(
                f"Tariff profile synced from gateway: "
                f"{req.get('tariff_type_label', '?')} / "
                f"{req.get('tariff_company', 'unknown company')} / "
                f"NEM: {req.get('nem_type_label', '?')}"
            )
        )
        return {"ok": True, "message": "Tariff profile synced"}
    except Exception as exc:
        logger.exception(f"Failed to sync tariff profile for service {service_id}")
        return {"ok": False, "error": str(exc)}
