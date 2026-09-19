"""Typical home load and solar for a site, by season, day type and local hour.

Built for the schedule optimiser, and the distinction matters.

A TOU schedule is a **repeating daily pattern**. It is written to the gateway
and stays there for months. Optimising it against today's or tomorrow's weather
bakes one day's cloud cover into a season-long rule — a fortnight of rain in
the forecast would produce a schedule that keeps grid-charging in December.

So this returns a *representative* day for the season, not a forecast of any
particular one. The live solar forecast has the opposite job: Smart Dispatch
re-decides every few minutes against what is actually about to happen, and
"today" versus "remaining today" is the right question there. It is the wrong
question for a pattern that repeats.

Medians, not means. One night the aircon ran until 4am should not raise the
expected overnight load for the whole season, and a single cloudless Sunday
should not raise expected solar. Medians ignore that; means do not.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Below this many samples an hour's median is noise dressed as a number.
MIN_SAMPLES_PER_HOUR = 12

WEEKDAY, WEEKEND = "weekday", "weekend"


def _local_hour(timestamp: str) -> Optional[tuple[int, str, int]]:
    """(local hour, day type, month) for a stored timestamp.

    Local, because schedule blocks are local-time boundaries. Metrics are
    stored in UTC, and reading them as local shifts the whole day — a Sydney
    site's solar peak lands at "00:00" if you do not convert, which is exactly
    the mistake that put 4 kW of expected sun at 2am in the optimiser.
    """
    try:
        dt = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None

    # gateway_metrics stores naive UTC — "2026-09-12 13:16:22" while the site's
    # clock reads 23:16. A naive value must therefore be stamped UTC before
    # converting, not taken at face value; treating it as local put the solar
    # peak at 01:00 and would have had the optimiser plan around sunshine in
    # the small hours. Third time this session that a UTC/local mix-up has
    # produced a confidently wrong answer, hence the explicit stamp.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()

    return dt.hour, (WEEKDAY if dt.weekday() < 5 else WEEKEND), dt.month


def build_profile(rows: list[tuple[str, str]], months: Optional[set[int]] = None) -> dict[str, Any]:
    """Median home load and solar per local hour, split by day type.

    `rows` is (timestamp, data_json) as stored in gateway_metrics. `months`
    restricts to one season — the point of a seasonal profile is that August
    does not inform January.
    """
    load: dict[str, dict[int, list[float]]] = {WEEKDAY: defaultdict(list), WEEKEND: defaultdict(list)}
    solar: dict[str, dict[int, list[float]]] = {WEEKDAY: defaultdict(list), WEEKEND: defaultdict(list)}
    used = 0

    for timestamp, payload in rows:
        parsed = _local_hour(timestamp)
        if not parsed:
            continue
        hour, day_type, month = parsed
        if months and month not in months:
            continue
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue

        home_kw = data.get("home_kw")
        solar_kw = data.get("solar_kw")
        if home_kw is None and solar_kw is None:
            continue
        used += 1
        if home_kw is not None:
            load[day_type][hour].append(float(home_kw))
        if solar_kw is not None:
            solar[day_type][hour].append(float(solar_kw))

    def _medians(buckets: dict[int, list[float]]) -> dict[int, float]:
        return {
            hour: round(statistics.median(values), 4)
            for hour, values in buckets.items()
            if len(values) >= MIN_SAMPLES_PER_HOUR
        }

    return {
        "samples": used,
        "months": sorted(months) if months else None,
        "home_kw": {WEEKDAY: _medians(load[WEEKDAY]), WEEKEND: _medians(load[WEEKEND])},
        "solar_kw": {WEEKDAY: _medians(solar[WEEKDAY]), WEEKEND: _medians(solar[WEEKEND])},
    }


def merge_day_types(profile: dict[str, Any], key: str) -> dict[int, float]:
    """One hour→value map, for a season whose schedule has no weekday split.

    Averaged across the two day types rather than picking one, so a schedule
    that applies to every day is not planned against Tuesdays alone.
    """
    weekday = (profile.get(key) or {}).get(WEEKDAY) or {}
    weekend = (profile.get(key) or {}).get(WEEKEND) or {}
    hours = set(weekday) | set(weekend)
    merged: dict[int, float] = {}
    for hour in hours:
        values = [v[hour] for v in (weekday, weekend) if hour in v]
        merged[hour] = round(sum(values) / len(values), 4)
    return merged


def is_usable(profile: dict[str, Any]) -> bool:
    """Whether there is enough history to plan against.

    A fresh install has none, which is a normal state — the optimiser falls
    back to the live solar forecast rather than refusing to run. Said out loud
    because a profile built from six hours of data would look authoritative and
    be worthless.
    """
    solar = profile.get("solar_kw") or {}
    covered = set(solar.get(WEEKDAY) or {}) | set(solar.get(WEEKEND) or {})
    return len(covered) >= 12
