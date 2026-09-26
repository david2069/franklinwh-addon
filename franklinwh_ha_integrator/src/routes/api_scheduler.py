import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field, model_validator

from src.app_state import get_app_state
from src.services.db import log_admin_audit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["scheduler"])

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class ConditionDef(BaseModel):
    metric: str
    operator: str
    value: Any
    value_ref: Optional[str] = ""
    value_mode: Optional[str] = "literal"
    enabled: Optional[bool] = True

class ActionDef(BaseModel):
    type: str
    payload: dict
    enabled: Optional[bool] = True

class RuleCreate(BaseModel):
    name: str = Field(..., description="Human readable rule name")
    gateway_serial: str
    condition_logic: str = Field("AND", description="AND or OR for evaluating conditions")
    conditions: List[ConditionDef]
    actions: List[ActionDef]
    cron: str = Field("*/1 * * * *", description="Cron schedule for rule evaluation")
    run_at: Optional[str] = Field(None, description="Optional ISO8601 exact one-off run time")
    enabled: bool = True
    owner: str = Field("user", description="Owner tag: 'system' or 'user'")
    tags: List[str] = Field(default_factory=list, description="Optional classification tags")
    duration_secs: int = Field(0, description="Condition must be true continuously for this many seconds before executing")
    priority: int = Field(25, description="Rule evaluation priority (lower is higher priority)")

    @model_validator(mode='after')
    def check_exclusivity(self) -> 'RuleCreate':
        if self.run_at and self.cron and self.cron.strip() not in ("*/1 * * * *", ""):
            raise ValueError("Strict Validation Error: A rule cannot define both an explicit one-off 'run_at' target and a repeating 'cron' schedule natively. Choose exactly one mutually-exclusive trigger format.")
        return self

class RuleResponse(RuleCreate):
    id: str
    next_run_time: Optional[str]
    retention_days: int = 30  # Requested default UI configuration

class EvaluationRequest(BaseModel):
    gateway_serial: str
    condition_logic: str = "AND"
    conditions: List[ConditionDef]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_user_rule(kwargs: dict) -> bool:
    """True iff the job's kwargs match the RuleResponse schema (user-created
    automation rule). Used by /scheduler/jobs to filter out system-owned jobs
    without a maintenance-heavy prefix block-list. See GH #33."""
    if not isinstance(kwargs, dict):
        return False
    gateway_serial = kwargs.get("gateway_serial")
    if not isinstance(gateway_serial, str) or not gateway_serial:
        return False
    if not isinstance(kwargs.get("conditions"), list):
        return False
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/scheduler/jobs", response_model=List[RuleResponse])
async def list_jobs():
    """Retrieve all active condition rules."""
    state = get_app_state()
    engine = state.get("scheduler")
    if not engine:
        raise HTTPException(status_code=503, detail="Scheduler engine offline")

    jobs = engine.scheduler.get_jobs()
    results = []

    for job in jobs:
        # Structural allow-list — return only jobs whose kwargs shape
        # matches RuleResponse (user-created automation rules). System-
        # owned scheduler jobs (SD Macro/Meso/Micro, persona:weekly, and
        # anything added in future) lack these kwargs, so they're
        # naturally excluded — no per-prefix maintenance required.
        #
        # Previously this was a block-list of known system prefixes
        # (sd:*, persona:*). Every new system-owned job had to remember
        # to update the filter, or /api/scheduler/jobs 500'd and the
        # Automations tab silently blanked — see GH #33.
        kwargs = job.kwargs or {}
        if not _is_user_rule(kwargs):
            continue
        results.append({
            "id": job.id,
            "name": job.name,
            "gateway_serial": kwargs.get("gateway_serial"),
            "condition_logic": kwargs.get("condition_logic", "AND"),
            "conditions": kwargs.get("conditions", []),
            "actions": kwargs.get("actions", [kwargs.get("action")] if "action" in kwargs else []),
            "owner": kwargs.get("owner", "user"),
            "tags": kwargs.get("tags", []),
            "cron": kwargs.get("cron", "*/1 * * * *"),
            "run_at": kwargs.get("run_at"),
            "enabled": job.next_run_time is not None,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            "duration_secs": kwargs.get("duration_secs", 0),
            "retention_days": 30,
            "created_at": kwargs.get("created_at"),
            "updated_at": kwargs.get("updated_at")
        })

    return results

@router.post("/scheduler/jobs")
async def create_job(rule: RuleCreate):
    """Add a new automation rule checking telemetry periodically."""
    state = get_app_state()
    engine = state.get("scheduler")
    if not engine:
        raise HTTPException(status_code=503, detail="Scheduler engine offline")

    # The actual execution payload triggers `run_rule` continuously based on the cron
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    
    try:
        from src.services.scheduler_core import run_automation_job
        if rule.run_at:
            trigger = DateTrigger(run_date=datetime.fromisoformat(rule.run_at.replace("Z", "+00:00")))
        else:
            trigger = CronTrigger.from_crontab(rule.cron)
            
        job = engine.scheduler.add_job(
            run_automation_job,
            trigger=trigger,
            name=rule.name,
            max_instances=1,
            kwargs={
                "rule_id": str(int(len(engine.scheduler.get_jobs()) + 1)),
                "rule_name": rule.name,
                "gateway_serial": rule.gateway_serial,
                "condition_logic": rule.condition_logic,
                "conditions": [c.model_dump() for c in rule.conditions],
                "actions": [a.model_dump() for a in rule.actions],
                "owner": rule.owner,
                "tags": rule.tags,
                "cron": rule.cron,
                "run_at": rule.run_at,
                "duration_secs": rule.duration_secs,
                "priority": rule.priority,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            },
            replace_existing=True
        )
        
        if not rule.enabled:
            job.pause()

        await log_admin_audit("automation", "create", "david_admin", f"Created new rule '{rule.name}' targeting {rule.gateway_serial}")
        return {"status": "ok", "job_id": job.id}
    except Exception as e:
        logger.error(f"Failed to create job: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/scheduler/jobs/{job_id}")
async def update_job(job_id: str, rule: RuleCreate):
    """Update an existing automation rule."""
    state = get_app_state()
    engine = state.get("scheduler")
    if not engine:
        raise HTTPException(status_code=503, detail="Scheduler engine offline")

    existing_job = engine.scheduler.get_job(job_id)
    if not existing_job:
        raise HTTPException(status_code=404, detail="Job not found")

    if existing_job.kwargs.get("owner") == "system":
        raise HTTPException(status_code=403, detail="System rules cannot be modified")

    rule_id = existing_job.kwargs.get("rule_id", str(int(len(engine.scheduler.get_jobs()) + 1)))

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from src.services.scheduler_core import run_automation_job
    
    try:
        if rule.run_at:
            trigger = DateTrigger(run_date=datetime.fromisoformat(rule.run_at.replace("Z", "+00:00")))
        else:
            trigger = CronTrigger.from_crontab(rule.cron)
            
        job = engine.scheduler.add_job(
            run_automation_job,
            trigger=trigger,
            name=rule.name,
            id=job_id,
            max_instances=1,
            kwargs={
                "rule_id": rule_id,
                "rule_name": rule.name,
                "gateway_serial": rule.gateway_serial,
                "condition_logic": rule.condition_logic,
                "conditions": [c.model_dump() for c in rule.conditions],
                "actions": [a.model_dump() for a in rule.actions],
                "owner": rule.owner,
                "tags": rule.tags,
                "cron": rule.cron,
                "run_at": rule.run_at,
                "duration_secs": rule.duration_secs,
                "priority": rule.priority,
                "created_at": existing_job.kwargs.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            },
            replace_existing=True
        )
        
        if not rule.enabled:
            job.pause()
        else:
            job.resume()

        await log_admin_audit("automation", "update", "david_admin", f"Updated configuration for rule '{rule.name}' ({job_id})")
        return {"status": "ok", "job_id": job.id}
    except Exception as e:
        logger.error(f"Failed to update job: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/scheduler/jobs/{job_id}")
async def delete_job(job_id: str):
    """"Remove an automation rule."""
    state = get_app_state()
    engine = state.get("scheduler")
    if not engine:
        raise HTTPException(status_code=503, detail="Scheduler engine offline")

    try:
        job = engine.scheduler.get_job(job_id)
        if job and job.kwargs.get("owner") == "system":
            raise HTTPException(status_code=403, detail="System rules cannot be deleted")
            
        engine.scheduler.remove_job(job_id)
        await log_admin_audit("automation", "delete", "david_admin", f"Deleted automation rule '{job_id}'")
        return {"status": "ok", "job_id": job_id}
    except Exception as e:
        logger.error(f"Failed to delete job: {e}")
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found: {e}")

@router.post("/scheduler/jobs/{job_id}/execute")
async def execute_job_now(job_id: str):
    """Manually trigger an automation rule asynchronously and return a trace ID for polling."""
    state = get_app_state()
    engine = state.get("scheduler")
    if not engine:
        raise HTTPException(status_code=503, detail="Scheduler engine offline")
        
    job = engine.scheduler.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Automation job not found")
        
    from src.services.scheduler_core import run_automation_job
    import asyncio
    import time
    
    # Generate a unique trace ID for this specific manual run
    trace_id = f"manual_{int(time.time() * 1000)}"
    
    try:
        # Run in background to avoid blocking the UI response
        asyncio.create_task(run_automation_job(**job.kwargs, trace_id=trace_id))
        
        return {
            "status": "ok", 
            "message": "Manual execution started", 
            "trace_id": trace_id,
            "rule_id": job.kwargs.get("rule_id", job_id)
        }
    except Exception as e:
        logger.error(f"Execution failure: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/scheduler/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, enabled: bool):
    """Pause or resume execution."""
    state = get_app_state()
    engine = state.get("scheduler")
    if engine:
        try:
            if enabled:
                engine.scheduler.resume_job(job_id)
            else:
                engine.scheduler.pause_job(job_id)
            
            action_str = "Resumed" if enabled else "Paused"
            await log_admin_audit("automation", "toggle", "david_admin", f"{action_str} execution lifecycle for rule '{job_id}'")
            return {"status": "ok", "message": f"Job {job_id} {'enabled' if enabled else 'disabled'}"}
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e))
    raise HTTPException(status_code=503, detail="Scheduler engine offline")

@router.get("/scheduler/audit")
async def get_scheduler_audit():
    """Retrieve isolated structural telemetry edits for Automations."""
    try:
        from src.services.db import get_admin_audit_logs
        logs = await get_admin_audit_logs(limit=200, category="automation")
        return {"status": "ok", "logs": logs}
    except Exception as e:
        logger.error(f"Failed to fetch scheduler audit logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/scheduler/history")
async def get_historical_jobs(limit: int = 100, request_id: str = None):
    """Retrieve the ledger of historically fired rules and their outcomes. Filterable by trace ID."""
    from src.services.db import get_automation_history
    try:
        return await get_automation_history(limit=limit, request_id=request_id)
    except Exception as e:
        logger.error(f"Failed to read automation history: {e}")
        raise HTTPException(status_code=500, detail="Database read failed")

@router.post("/scheduler/evaluate")
async def evaluate_logic(req: EvaluationRequest):
    """Dynamically test a condition constraint against live hardware data without executing actions."""
    from src.services.scheduler_core import evaluate_conditions, automation_engine
    
    state = get_app_state()
    engine = state.get("scheduler", automation_engine)
    if not engine or not engine.registry:
        raise HTTPException(status_code=503, detail="Gateway registry offline")

    target_gws = []
    if req.gateway_serial == "ALL":
        target_gws = list(engine.registry._services.values())
    else:
        serials = [s.strip() for s in req.gateway_serial.split(",") if s.strip()]
        for s in serials:
            gw = engine.registry.get_gateway(s)
            if not gw:
                for g in engine.registry._services.values():
                    if getattr(g, "full_serial", None) == s or getattr(g, "short_id", None) == s:
                        gw = g
                        break
            if gw and gw not in target_gws:
                target_gws.append(gw)
                
    if not target_gws:
        return {"error": "No matching gateways found online to verify against"}

    evaluations = []
    for gateway in target_gws:
        try:
            live_data = gateway.status.last_data
            
            # Fetch pricing context for 'pricing.*' metrics
            extra_ctx = {}
            from src.services import db as _db

            # Initialize default pricing context
            extra_ctx["pricing"] = {
                "import_price_c_kwh": 0.0,
                "export_price_c_kwh": 0.0,
                "demand_active": 0,
                "demand_window_active": False,
                "spike_status": "neutral",
                "tariff_type": "Flat",
                "renewables_pct": 0.0,
                "daily_earnings": 0.0,
                "monthly_earnings": 0.0,
                "descriptor": "",
                "tariff_period": "",
                "solar_sponge": False,
                "sd_signal": "",
                "sd_signal_power_kw": None,
                "sd_signal_duration_mins": None,
                "sd_signal_target_soc": None,
            }

            pricing_reg = get_app_state().get("pricing_registry")
            if pricing_reg:
                pricing_svc = pricing_reg.get_primary_service()
                if pricing_svc:
                    snap = pricing_svc.get_snapshot()
                    if snap:
                        # Fetch earnings tracking
                        from src.services.earnings_tracker import get_daily_earnings, get_monthly_earnings
                        try:
                            daily_earnings = await get_daily_earnings(gateway.short_id)
                            monthly_earnings, _ = await get_monthly_earnings(gateway.short_id)
                        except Exception:
                            daily_earnings = 0.0
                            monthly_earnings = 0.0
                            
                        extra_ctx["pricing"] = {
                            "import_price_c_kwh": snap.import_c_kwh,
                            "export_price_c_kwh": snap.export_c_kwh,
                            "demand_active": 1 if snap.demand_window else 0,
                            # Phase 0: fix key name mismatch — UI condition uses demand_window_active
                            "demand_window_active": snap.demand_window,
                            "spike_status": snap.spike_status,
                            "tariff_type": snap.tariff_type,
                            "renewables_pct": snap.renewables_pct,
                            "daily_earnings": daily_earnings,
                            "monthly_earnings": monthly_earnings,
                            # Phase 0: price descriptor (extremelyLow/negative/low/neutral/high/spike)
                            "descriptor": getattr(snap, "descriptor", "") or "",
                            # Phase 0: tariff period (solarSponge/offPeak/shoulder/peak)
                            "tariff_period": getattr(snap, "tariff_period", "") or "",
                            # Phase 0: solar sponge boolean alias
                            "solar_sponge": (getattr(snap, "tariff_period", "") or "").lower() == "solarsponge",
                            # Phase 1+2: SD engine decision signal + enriched payload
                            **{"sd_signal": "", "sd_signal_power_kw": None, "sd_signal_duration_mins": None, "sd_signal_target_soc": None},
                        }

            # Enriched pricing with sd_signal
            try:
                if (_sd_sig_payload := await _db.get_sd_signal_payload(gateway.short_id)):
                    extra_ctx["pricing"].update({
                        "sd_signal": _sd_sig_payload.get("signal", ""),
                        "sd_signal_power_kw": _sd_sig_payload.get("power_kw"),
                        "sd_signal_duration_mins": _sd_sig_payload.get("duration_mins"),
                        "sd_signal_target_soc": _sd_sig_payload.get("target_soc"),
                        "sd_signal_calc_basis": _sd_sig_payload.get("calc_basis", ""),
                    })
            except Exception:
                pass

            # Phase 0: SD Engine Parameters for dispatch.* conditions
            try:
                sd_cfg = await _db.get_smart_dispatch_config(gateway.short_id)
            except Exception:
                sd_cfg = {}

            # Site Season & Forecast Loads
            home_loads_ctx = {"home_load": {}, "home_loads": {}}
            try:
                lat_val = await _db.get_config_value(f"lat_{gateway.short_id}")
                from datetime import datetime
                from src.services.smart_dispatch import (
                    get_site_season,
                    get_active_forecast_load_kw,
                    build_home_loads_context,
                )
                site_season = ""
                if lat_val:
                    site_season = get_site_season(float(lat_val), datetime.now().month)

                fl_loads = await _db.get_all_forecast_loads()

                ha_states = {}
                from src.routes.api_ha import get_ha_state
                for fl in fl_loads:
                    if fl.get("enabled", 1) and fl.get("measurement_type") == "now":
                        for key in ["ha_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                            ent_id = fl.get(key)
                            if ent_id:
                                st = await get_ha_state(ent_id)
                                if st and "state" in st:
                                    ha_states[ent_id] = st["state"]

                fl_kw = get_active_forecast_load_kw(
                    fl_loads,
                    datetime.now(),
                    site_season,
                    is_current_slot=True,
                    ha_states=ha_states
                )

                # Home Loads FHAI-native context (AB exposure).
                # See docs/automation_builder_home_loads_exposure_plan.md.
                home_loads_ctx = build_home_loads_context(
                    loads=fl_loads,
                    target_time=datetime.now(),
                    seasons_by_gateway={
                        gateway.short_id: site_season,
                        "global":         site_season,
                    },
                    rule_gateway_scope=gateway.short_id,
                )
            except Exception:
                site_season = ""
                fl_kw = 0.0

            extra_ctx["dispatch"] = {
                "min_soc_limit": float(sd_cfg.get("min_soc", 20)),
                "max_soc_limit": float(sd_cfg.get("max_soc", 90)),
                # Peak window SOC guard rails
                "min_peak_window_soc": float(sd_cfg.get("min_peak_window_soc", 60)),
                "max_peak_window_soc": float(sd_cfg.get("max_peak_window_soc", 90)),
                # Phase 2: expose min_export_price + max_charge_price for RHS value_ref resolution
                "min_export_price": float(sd_cfg.get("min_export_price", 0.0)),
                "max_charge_price": float(sd_cfg.get("max_charge_price", 15.0)),
                # solar_pv_active: true if solar is currently generating (> 100W threshold)
                "solar_pv_active": float((live_data or {}).get("solar_kw", 0) or 0) > 0.1,
                "site_season": site_season,
                "forecast_load_kw": round(fl_kw, 2),
                "forecast_load_active": fl_kw > 0.0,
            }

            # Home Loads namespaces (FHAI-native, scope-aware)
            extra_ctx["home_load"]  = home_loads_ctx["home_load"]
            extra_ctx["home_loads"] = home_loads_ctx["home_loads"]

            # Phase 0: gateway context booleans
            extra_ctx["gateway"] = {
                "has_solar": bool((live_data or {}).get("has_solar", False)),
            }

            # Fetch and inject Solar Forecast context
            tz_name = live_data.get("device", {}).get("timezone") if live_data else None
            try:
                from src.services.scheduler_core import get_solar_forecast_metrics
                sf_metrics = await get_solar_forecast_metrics(gateway.short_id, tz_name)
            except Exception as e:
                logger.error(f"Failed to get solar forecast metrics for evaluation: {e}")
                sf_metrics = {"today_kwh": 0.0, "remaining_kwh": 0.0, "tomorrow_kwh": 0.0}
            extra_ctx["solar_forecast"] = sf_metrics

            # Fetch and inject Weather context
            extra_ctx["weather"] = {
                "temp": float(live_data.get("weather_temp", 0.0) or 0.0) if live_data else 0.0,
                "humidity": int(live_data.get("weather_humidity", 0) or 0) if live_data else 0,
                "condition": str(live_data.get("weather_condition", "Unknown")) if live_data else "Unknown",
                "icon": str(live_data.get("weather_icon", "")) if live_data else "",
                "pressure": int(live_data.get("weather_pressure", 1013) or 1013) if live_data else 1013,
                "active_storm_count": int(live_data.get("active_storm_count", 0) or 0) if live_data else 0,
                "active_storm_warning": int(live_data.get("active_storm_warning", 0) or 0) if live_data else 0,
            }

            # Phase 8: Reusable Macros / Base Conditions evaluation stub
            extra_ctx["base"] = {}

            # Phase 3: Site Orchestrator — Virtual Site Meter
            extra_ctx["site"] = {
                "p_fhp": 0.0,
                "p_uti": 0.0,
                "p_sun": 0.0,
                "p_ld": 0.0,
                "soc_avg": 0.0,
                "soc_min": 0.0,
            }
            if engine and getattr(engine, "registry", None):
                try:
                    site_snap = engine.registry.get_site_snapshot()
                    extra_ctx["site"] = {
                        "p_fhp":         site_snap.get("p_fhp", 0.0),
                        "p_uti":         site_snap.get("p_uti", 0.0),
                        "p_sun":         site_snap.get("p_sun", 0.0),
                        "p_ld":          site_snap.get("p_ld", 0.0),
                        "soc_avg":       site_snap.get("soc_avg", 0.0),
                        "soc_min":       site_snap.get("soc_min", 0.0),
                    }
                except Exception:
                    pass
            
            matched, failed_reasons, cond_results = evaluate_conditions(live_data, req.condition_logic, [c.model_dump() for c in req.conditions], extra_context=extra_ctx)
            evaluations.append({
                "gateway": gateway.short_id,
                "name": gateway.context.get("name") or live_data.get("equipment", {}).get("agate", {}).get("name") or f"Gateway {gateway.short_id}",
                "matched": matched,
                "reasons": failed_reasons,
                "states": cond_results
            })
        except Exception as e:
            evaluations.append({
                "gateway": gateway.short_id,
                "matched": False,
                "reasons": [f"Evaluation crashed: {e}"],
                "states": [False for _ in req.conditions]
            })

    return {"status": "ok", "matched": any(e["matched"] for e in evaluations), "evaluations": evaluations}
