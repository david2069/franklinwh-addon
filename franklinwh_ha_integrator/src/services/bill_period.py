"""Turn metric history into the quantities a bill is actually built from.

tariff_costing knows how to price a demand peak or an in-window export. It does
not know what the peak *was*, and nothing computed it — so the demand window,
the export window and the free allowance were all stored, editable, and absent
from every total.

Two quantities, both read from gateway_metrics:

  **The demand peak.** Not the highest instantaneous reading — the highest
  *average* over the plan's measurement interval, because that is what the
  meter bills. A 9 kW kettle for ninety seconds does not set a 9 kW demand
  charge against a 60-minute interval, and treating it as though it does is
  the difference between a plausible bill and an alarming one.

  **Energy inside a window.** Integrated from instantaneous kW rather than
  differenced from the cumulative counters, because those reset daily and a
  window that straddles midnight would read as a large negative.

`grid_kw` is signed: positive is import, negative is export. Timestamps in
gateway_metrics are naive UTC — the fifth place in this project where that has
had to be said — and windows are local wall-clock, so every row is converted
before it is compared to a window.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from src.services import tariff_costing

logger = logging.getLogger(__name__)

# A gap longer than this means the poller stopped; treating it as a continuous
# sample would attribute hours of energy to a single reading.
MAX_SAMPLE_GAP_SEC = 15 * 60


def _local(timestamp: str) -> Optional[datetime]:
    """gateway_metrics stores naive UTC. Windows are local wall-clock."""
    try:
        dt = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _samples(rows: Iterable[tuple]) -> list[tuple[datetime, float]]:
    """(local time, signed grid kW), oldest first."""
    out = []
    for timestamp, payload in rows or []:
        when = _local(timestamp)
        if when is None:
            continue
        try:
            data = json.loads(payload) if isinstance(payload, str) else (payload or {})
            grid_kw = data.get("grid_kw")
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if grid_kw is None:
            continue
        try:
            out.append((when, float(grid_kw)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda s: s[0])
    return out


def demand_peak_kw(rows, window: dict | None, service: dict | None) -> float:
    """Highest average import over the plan's measurement interval, in-window.

    Returns 0.0 when the window never matched, which is not the same as a peak
    of zero and is why the caller checks the window separately.
    """
    if not window:
        return 0.0
    interval_sec = tariff_costing.demand_interval_minutes(service) * 60
    samples = [(t, kw) for t, kw in _samples(rows)
               if tariff_costing.window_matches(window, t)]
    if not samples:
        return 0.0

    # Import only. Export does not contribute to a demand charge, and letting a
    # negative reading into the average would understate the peak.
    imports = [(t, max(0.0, kw)) for t, kw in samples]

    peak = 0.0
    start = 0
    for end in range(len(imports)):
        # Slide the window start until the span fits the interval.
        while (imports[end][0] - imports[start][0]).total_seconds() > interval_sec:
            start += 1
        span = imports[start:end + 1]
        if not span:
            continue
        peak = max(peak, sum(kw for _, kw in span) / len(span))
    return round(peak, 4)


def energy_in_window_kwh(rows, window: dict | None, *, exporting: bool) -> float:
    """Energy through the meter inside a window, in kWh.

    Integrated over the gap between consecutive samples. A gap longer than
    MAX_SAMPLE_GAP_SEC is skipped rather than extrapolated — a poller that
    stopped for six hours would otherwise book six hours of whatever it last
    saw.
    """
    if not window:
        return 0.0
    samples = _samples(rows)
    if len(samples) < 2:
        return 0.0

    total_kwh = 0.0
    for (t0, kw0), (t1, _) in zip(samples, samples[1:]):
        gap = (t1 - t0).total_seconds()
        if gap <= 0 or gap > MAX_SAMPLE_GAP_SEC:
            continue
        if not tariff_costing.window_matches(window, t0):
            continue
        signed = -kw0 if exporting else kw0
        if signed <= 0:
            continue
        total_kwh += signed * gap / 3600.0
    return round(total_kwh, 4)


async def period_extras_aud(gateway_id: str, service: dict | None,
                            days: float, rows=None) -> dict:
    """Demand, export-window and listed standing charges for a period.

    Returned itemised rather than summed: a demand charge that appears from
    nowhere in a total is the kind of thing people assume is a bug, and the
    breakdown is what makes it checkable.
    """
    from src.services import db

    result = {"demand_aud": 0.0, "export_window_aud": 0.0,
              "standing_list_aud": 0.0, "items": []}
    if not service:
        return result

    service_id = service.get("id")
    try:
        windows = await db.get_utility_service_windows(service_id) if service_id else []
    except Exception:
        logger.debug("bill: windows unavailable for %s", service_id, exc_info=True)
        windows = []

    try:
        charges = await db.list_standing_charges(service_id) if service_id else []
    except Exception:
        charges = []
    if charges:
        result["standing_list_aud"] = tariff_costing.list_standing_charges_aud(
            charges, days, month=datetime.now().astimezone().month)
        for charge in charges:
            result["items"].append(
                {"kind": "standing", "label": charge.get("label"),
                 "aud": round(tariff_costing.list_standing_charges_aud(
                     [charge], days, month=datetime.now().astimezone().month), 4)})

    demand_windows = tariff_costing.windows_of_type(windows, "demand")
    export_windows = tariff_costing.windows_of_type(windows, "export")
    if not demand_windows and not export_windows:
        return result

    if rows is None:
        try:
            rows = await db.get_gateway_metric_rows(gateway_id)
        except Exception:
            logger.debug("bill: metrics unavailable for %s", gateway_id, exc_info=True)
            rows = []

    for window in demand_windows:
        peak = demand_peak_kw(rows, window, service)
        amount = tariff_costing.demand_charge_aud(service, window, peak, days)
        if amount:
            result["demand_aud"] += amount
            result["items"].append(
                {"kind": "demand", "label": window.get("label") or "Demand",
                 "peak_kw": peak, "aud": round(amount, 4)})

    free_kwh = tariff_costing.export_free_allowance_kwh(service, days)
    for window in export_windows:
        kwh = energy_in_window_kwh(rows, window, exporting=True)
        amount = tariff_costing.export_window_aud(window, kwh, free_kwh)
        if amount:
            result["export_window_aud"] += amount
            result["items"].append(
                {"kind": "export_window", "label": window.get("label") or "Export",
                 "kwh": kwh, "aud": round(amount, 4)})

    result["demand_aud"] = round(result["demand_aud"], 4)
    result["export_window_aud"] = round(result["export_window_aud"], 4)
    result["standing_list_aud"] = round(result["standing_list_aud"], 4)
    return result
