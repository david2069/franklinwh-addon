"""
earnings_tracker.py — Amber hourly earnings cache.

Polls AmberAdapter.get_usage() once per hour (AP-4 compliant — NOT in the 30s telemetry loop).
Writes results to amber_usage_cache.  Earnings are read back by the SmartDispatchEngine
to evaluate the earnings_target rule category.

Billing cycle integration:
  Monthly reset date is read from utility_config.bill_start_day (Pricing & Billing tab).
  Falls back to day 1 of the current month if no utility_config row exists.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from src.services import db
from src.services import bill_period, tariff_costing

logger = logging.getLogger(__name__)

_estimation_cache = {}  # { gateway_id: (timestamp, est_earnings, est_cost) }
_ESTIMATION_TTL_SEC = 300  # 5 minutes

def _billing_cycle_start(bill_start_day: int) -> date:
    """
    Return the start date of the current billing cycle.
    If today's day >= bill_start_day, cycle started this month.
    Otherwise it started last month.
    """
    today = date.today()
    try:
        cycle_start = today.replace(day=bill_start_day)
    except ValueError:
        # bill_start_day > days in current month (e.g. 31 in Feb) — use last valid day
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        cycle_start = today.replace(day=min(bill_start_day, last_day))

    if today < cycle_start:
        # We haven't reached the billing start day yet — cycle started last month
        first_of_last = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        try:
            cycle_start = first_of_last.replace(day=bill_start_day)
        except ValueError:
            import calendar
            last_day = calendar.monthrange(first_of_last.year, first_of_last.month)[1]
            cycle_start = first_of_last.replace(day=min(bill_start_day, last_day))

    return cycle_start


async def _get_bill_start_day() -> tuple[int, bool]:
    """
    Read bill_start_day from utility_config.
    Returns (day: int, is_configured: bool).
    is_configured=False means no utility_config row exists — earnings rule should be skipped.
    """
    try:
        async with db.get_db() as conn:
            async with conn.execute(
                "SELECT bill_start_day FROM utility_config ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        if row and row["bill_start_day"] is not None:
            return int(row["bill_start_day"]), True
        return 1, False   # row exists but day is NULL — treat as unconfigured
    except Exception as exc:
        logger.debug(f"earnings_tracker: could not read bill_start_day — {exc}")
        return 1, False   # no utility_config table row — not configured


async def poll_usage_and_cache(gateway_id: str, adapter) -> dict:
    """
    Fetch the last 7 days of Amber usage for a gateway and populate amber_usage_cache.
    Designed to be called once per hour, not on every telemetry cycle.

    Args:
        gateway_id: aGate short_id
        adapter:    AmberAdapter instance (already authenticated)

    Returns:
        {"general_inserted": int, "feedin_inserted": int, "error": str|None}
    """
    today_str = date.today().isoformat()
    week_ago_str = (date.today() - timedelta(days=7)).isoformat()

    try:
        usage = await adapter.get_usage(week_ago_str, today_str)
    except Exception as exc:
        logger.warning(f"earnings_tracker [{gateway_id}]: get_usage failed — {exc}")
        return {"general_inserted": 0, "feedin_inserted": 0, "error": str(exc)}

    general_n = await db.cache_amber_usage(gateway_id, usage.get("general", []), "general")
    feedin_n  = await db.cache_amber_usage(gateway_id, usage.get("feed_in", []), "feed_in")

    logger.info(
        f"earnings_tracker [{gateway_id}]: cached {general_n} general + {feedin_n} feed_in rows"
    )
    return {"general_inserted": general_n, "feedin_inserted": feedin_n, "error": None}


async def get_daily_earnings(gateway_id: str) -> float:
    """Return today's feed-in earnings in AUD (positive)."""
    since_iso = date.today().isoformat() + "T00:00:00"
    return await db.get_amber_earnings(gateway_id, since_iso, channel="feed_in")


async def get_monthly_earnings(gateway_id: str) -> tuple[float, bool]:
    """
    Return (earnings_aud: float, is_configured: bool) for the current billing cycle.
    is_configured=False when Pricing & Billing has no bill_start_day set;
    the engine should skip the earnings_target rule in that case.
    """
    bill_start_day, is_configured = await _get_bill_start_day()
    if not is_configured:
        return 0.0, False
    cycle_start = _billing_cycle_start(bill_start_day)
    since_iso = cycle_start.isoformat() + "T00:00:00"
    earnings = await db.get_amber_earnings(gateway_id, since_iso, channel="feed_in")
    return earnings, True

async def _service_for(gateway_id: str) -> Optional[dict]:
    try:
        return await db.get_utility_service_for_gateway(gateway_id)
    except Exception:
        logger.debug("earnings: no utility service for %s", gateway_id, exc_info=True)
        return None


async def get_daily_cost(gateway_id: str) -> float:
    """Today's grid cost in AUD — energy plus the day's fixed charges.

    The standing charges used to be missing entirely. For a plan with a
    158.631 c/day supply charge that understated every day by about $1.59,
    which on a small bill is frequently the largest single line.
    """
    breakdown = await cost_breakdown(gateway_id, days=1,
                                     since_iso=date.today().isoformat() + "T00:00:00")
    return breakdown["net_aud"]


async def get_monthly_cost(gateway_id: str, is_configured: bool,
                           cycle_start: Optional[date]) -> float:
    if not is_configured or not cycle_start:
        return 0.0
    # Days elapsed in the cycle, today included — the charge applies from the
    # first day, so a cycle that started today is one day, not zero.
    days = (date.today() - cycle_start).days + 1
    breakdown = await cost_breakdown(
        gateway_id, days=days, since_iso=cycle_start.isoformat() + "T00:00:00",
        apply_minimum=True)
    return breakdown["net_aud"]


async def cost_breakdown(gateway_id: str, *, days: float, since_iso: str,
                         apply_minimum: bool = False) -> dict:
    """A period's cost, itemised.

    Everything below the energy line was stored, editable and absent from every
    total: the demand window, the export window, the free allowance and any
    standing charge beyond the three fixed columns. A plan with a demand charge
    reported a bill that could not be reconciled with the one that arrived.

    Itemised rather than summed because a demand charge appearing from nowhere
    in a total reads as a bug; the breakdown is what makes it checkable.
    """
    service = await _service_for(gateway_id)
    energy_aud = await db.get_amber_earnings(gateway_id, since_iso, channel="general")
    fixed_aud = tariff_costing.standing_charges_aud(service, days)

    try:
        extras = await bill_period.period_extras_aud(gateway_id, service, days)
    except Exception:
        logger.debug("cost: period extras unavailable for %s", gateway_id, exc_info=True)
        extras = {"demand_aud": 0.0, "export_window_aud": 0.0,
                  "standing_list_aud": 0.0, "items": []}

    # export_window_aud is signed: a feed-in credit is positive and a two-way
    # export charge is negative, so it is subtracted either way and one sign
    # convention covers both.
    net = (energy_aud + fixed_aud + extras["demand_aud"]
           + extras["standing_list_aud"] - extras["export_window_aud"])

    minimum_applied = False
    if apply_minimum:
        floored = tariff_costing.apply_minimum_bill(net, service)
        minimum_applied = floored != net
        net = floored

    return {
        "energy_aud": round(energy_aud, 4),
        "fixed_aud": round(fixed_aud, 4),
        "demand_aud": extras["demand_aud"],
        "export_window_aud": extras["export_window_aud"],
        "standing_list_aud": extras["standing_list_aud"],
        "items": extras["items"],
        "days": round(float(days), 4),
        "minimum_applied": minimum_applied,
        "net_aud": round(net, 4),
    }

async def _estimate_today(gateway_id: str) -> tuple[float, float]:
    """
    Estimate today's earnings/cost using FranklinWH cloud metrics + Amber pricing.
    Provides a real-time estimate bridging 5-min historical actuals with 30-min 
    future forecasts.
    """
    now = datetime.now().timestamp()
    cached = _estimation_cache.get(gateway_id)
    if cached and now - cached[0] < _ESTIMATION_TTL_SEC:
        return cached[1], cached[2]

    try:
        from src.services.smart_dispatch import smart_dispatch_engine
        gw = smart_dispatch_engine._gateway_registry.get_gateway(gateway_id)
        if not gw: return 0.0, 0.0

        client = await gw._get_or_create_client()
        today_date = date.today().strftime("%Y-%m-%d")
        stats = await client.get_power_by_day(dayTime=today_date)
        
        def _sum(idx, keys):
            total = 0.0
            for k in keys:
                arr = stats.get(k)
                if arr and len(arr) > idx:
                    val = arr[idx]
                    if val is not None:
                        try:
                            total += float(val)
                        except ValueError:
                            pass
            return total

        est_earnings = 0.0
        est_cost = 0.0
        
        utility_service = await db.get_utility_service_for_gateway(gateway_id)
        service_id = utility_service["id"] if utility_service else None
        prices = await db.get_price_history(hours=24, utility_service_id=service_id)
        
        today_iso = date.today().isoformat()
        today_prices = [p for p in prices if (p.get("start_time") or "").startswith(today_iso)]
        
        # Build price lookup by timestamp string (up to minute)
        price_map = {}
        for p in today_prices:
            st = p.get("start_time")
            if st and len(st) >= 16:
                price_map[st[:16]] = p
                
        # Phase A: Historical actuals (5-min intervals)
        device_times = stats.get("deviceTimeArray", [])
        for i, dt_str in enumerate(device_times):
            if not dt_str or len(dt_str) < 16:
                continue
            # "2026-04-26 00:05:00" -> "2026-04-26T00:05"
            iso_key = dt_str[:10] + "T" + dt_str[11:16]
            
            p = price_map.get(iso_key)
            if not p:
                continue
                
            import_c = float(p.get("import_c_kwh", 0.0))
            export_c = float(p.get("export_c_kwh", 0.0))
            
            g_in = _sum(i, ['powerGirdHomeArray', 'powerGirdFhpArray'])
            g_out = _sum(i, ['powerSolarGirdArray', 'powerFhpGirdArray'])
            
            # Through the costing helpers so the conversion is defined once.
            # Rates are used exactly as entered, in cents, with no adjustment
            # applied on either side — see tariff_costing.
            est_cost += tariff_costing.import_cost_aud(g_in / 12.0, import_c)
            est_earnings += tariff_costing.export_revenue_aud(g_out / 12.0, export_c)

        # Phase B: Future forecasted estimation (30-min slots)
        from src.routes.api_smart_dispatch import get_forecast_map
        forecast_data = await get_forecast_map(gateway_id)
        if forecast_data and forecast_data.get("ok"):
            slots = forecast_data.get("slots", [])
            for slot in slots:
                slot_start_str = slot.get("start", "")
                if not slot_start_str.startswith(today_iso):
                    continue
                
                try:
                    slot_dt = datetime.fromisoformat(slot_start_str)
                    if slot_dt.timestamp() < now:
                        continue
                except ValueError:
                    continue
                    
                import_c = float(slot.get("import_c", 0.0))
                export_c = float(slot.get("export_c", 0.0))
                
                charge_kw = slot.get("charge_kw", 0.0)
                discharge_kw = slot.get("discharge_kw", 0.0)
                home_load_kw = slot.get("home_load_kw", 0.0)
                pv_kw = slot.get("pv_kw", 0.0)
                
                net_kw = home_load_kw + charge_kw - pv_kw - discharge_kw
                if net_kw > 0:
                    est_cost += tariff_costing.import_cost_aud(net_kw * 0.5, import_c)
                elif net_kw < 0:
                    est_earnings += tariff_costing.export_revenue_aud(abs(net_kw) * 0.5, export_c)
        
        _estimation_cache[gateway_id] = (now, est_earnings, est_cost)
        return est_earnings, est_cost
    except Exception as e:
        logger.error(f"Earnings estimation failed: {e}", exc_info=True)
        return 0.0, 0.0

async def get_earnings_summary(gateway_id: str) -> dict:
    """Return a dict with daily and monthly earnings plus billing context."""
    bill_start_day, is_configured = await _get_bill_start_day()
    cycle_start = _billing_cycle_start(bill_start_day) if is_configured else None

    daily = await get_daily_earnings(gateway_id)
    monthly_earnings, _ = await get_monthly_earnings(gateway_id)
    
    daily_cost = await get_daily_cost(gateway_id)
    monthly_cost = await get_monthly_cost(gateway_id, is_configured, cycle_start)
    
    est_daily, est_cost = await _estimate_today(gateway_id)

    # The estimate is energy-only; add today's standing charges so it agrees
    # with daily_cost_aud rather than sitting quietly below it.
    service = await _service_for(gateway_id)
    est_cost += tariff_costing.standing_charges_aud(service, 1)

    # Itemise, so a cost figure can be explained rather than asserted. Without
    # this a user seeing $1.59 on a day they used nothing has no way to learn
    # that it is the supply charge and not a bug.
    daily_breakdown = tariff_costing.build_breakdown(
        import_aud=await db.get_amber_earnings(
            gateway_id, date.today().isoformat() + "T00:00:00", channel="general"),
        export_aud=daily,
        service=service,
        days=1,
    )

    return {
        "is_billing_configured": is_configured,
        "daily_breakdown":       daily_breakdown.to_dict(),
        "supply_charge_warning": tariff_costing.supply_charge_warning(service),
        "daily_earnings_aud":    round(daily, 4),
        "monthly_earnings_aud":  round(monthly_earnings, 4),
        "daily_cost_aud":        round(daily_cost, 4),
        "monthly_cost_aud":      round(monthly_cost, 4),
        "estimated_daily_earnings_aud": round(est_daily, 4),
        "estimated_daily_cost_aud":     round(est_cost, 4),
        "billing_cycle_start":  cycle_start.isoformat() if cycle_start else None,
        "bill_start_day":       bill_start_day if is_configured else None,
    }
