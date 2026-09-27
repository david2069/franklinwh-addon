"""
Flatten a TOU snapshot into the flat block list a preset holds.

The two stores describe the same thing in different shapes, which is why
"restore this snapshot as a preset" was not a one-line call:

  snapshot  strategyList -> [season] -> dayTypeVoList -> [dayType] -> detailVoList -> [block]
  preset    a single flat list of blocks — one day, no seasons, no day types

So the conversion is a *projection*, and it is lossy whenever the snapshot has
more than one season or day type. Real gateways do: every snapshot in this
deployment carries Weekdays (dayType 1) and Weekends,Holidays (dayType 2), and
multi-season plans are normal for a tariff with seasonal rates. The caller is
told exactly what was dropped rather than being handed a silently narrowed
schedule.

Two things this must get right, because both fail silently:

  * **Cloud ids must not survive.** Blocks carry ``id`` and ``strategyId``, and
    seasons carry ``id``/``templateId`` — primary keys belonging to the gateway
    and strategy the snapshot came from. A preset is cross-gateway and gets
    pushed elsewhere, so carrying them risks mis-targeting a write. They are
    stripped.

  * **Operational fields must survive.** The push path forwards maxChargeSoc,
    minDischargeSoc, gridChargeMax and gridDischargeMax per block, defaulting
    them to 100 / 0 / 5000 / 5000 when absent. Dropping a snapshot's real
    values would quietly reset SOC limits and grid-charge ceilings on the next
    push — the schedule would look identical and behave differently.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# dayType is stable across every snapshot observed: 1 = Weekdays,
# 2 = Weekends,Holidays. Weekdays is the sensible default — it is the day a
# preset is most likely meant to cover.
DAY_TYPE_WEEKDAY = 1
DAY_TYPE_WEEKEND = 2

# Identity fields belonging to the source gateway/strategy. Never copied.
CLOUD_ID_FIELDS = ("id", "strategyId", "templateId")

# Carried through verbatim when present. The first five define the block; the
# rest change how the battery behaves and are defaulted by the push path, so
# losing them is a silent behaviour change rather than a visible gap.
BLOCK_FIELDS = (
    "startHourTime",
    "endHourTime",
    "name",
    "waveType",
    "dispatchId",
    "maxChargeSoc",
    "minDischargeSoc",
    "gridChargeMax",
    "gridDischargeMax",
    "solarPriority",
    "loadPriority",
    "solarCutoff",
)


def _season_label(season: dict) -> str:
    return str(season.get("seasonName") or season.get("name") or "Season")


def _day_label(day: dict) -> str:
    return str(day.get("dayName") or f"Day type {day.get('dayType')}")


def _pick_season(seasons: list[dict], wanted: str | None) -> tuple[dict | None, list[str]]:
    """Choose a season by name, else the first. Returns (chosen, others)."""
    if not seasons:
        return None, []
    chosen = None
    if wanted:
        chosen = next((s for s in seasons if _season_label(s) == wanted), None)
    if chosen is None:
        chosen = seasons[0]
    others = [_season_label(s) for s in seasons if s is not chosen]
    return chosen, others


def _pick_day_type(days: list[dict], wanted: str | int | None) -> tuple[dict | None, list[str]]:
    """Choose a day type by name or dayType code, else Weekdays, else first."""
    if not days:
        return None, []
    chosen = None
    if wanted is not None:
        chosen = next(
            (d for d in days if _day_label(d) == wanted or d.get("dayType") == wanted),
            None,
        )
    if chosen is None:
        chosen = next((d for d in days if d.get("dayType") == DAY_TYPE_WEEKDAY), None)
    if chosen is None:
        chosen = days[0]
    others = [_day_label(d) for d in days if d is not chosen]
    return chosen, others


def project_block(block: dict) -> dict:
    """Reduce one snapshot block to preset shape, minus the cloud identity."""
    out: dict[str, Any] = {}
    for field in BLOCK_FIELDS:
        if field in block and block[field] is not None:
            out[field] = block[field]
    # A block with no dispatchId would push as dispatchId 1 (Home Loads) — a
    # real behaviour change dressed as a default. Keep it explicit.
    out.setdefault("dispatchId", block.get("dispatchId", 1))
    out.setdefault("waveType", block.get("waveType", 0))
    out.setdefault("name", block.get("name") or "Time Block")
    return out


def flatten_snapshot(
    strategy_list: list[dict],
    *,
    season: str | None = None,
    day_type: str | int | None = None,
) -> dict[str, Any]:
    """Project a snapshot's strategyList onto one season + one day type.

    Returns the blocks alongside what was chosen and what was left behind, so
    the caller can say so rather than presenting a narrowed schedule as if it
    were the whole thing.
    """
    if not isinstance(strategy_list, list) or not strategy_list:
        return {
            "ok": False,
            "error": "Snapshot has no seasons to convert.",
            "blocks": [],
        }

    chosen_season, other_seasons = _pick_season(strategy_list, season)
    days = chosen_season.get("dayTypeVoList") or []
    chosen_day, other_days = _pick_day_type(days, day_type)

    if chosen_day is None:
        return {
            "ok": False,
            "error": f"Season {_season_label(chosen_season)!r} has no day types.",
            "blocks": [],
        }

    raw = chosen_day.get("detailVoList") or []
    blocks = [project_block(b) for b in raw if isinstance(b, dict)]

    if not blocks:
        return {
            "ok": False,
            "error": (
                f"{_season_label(chosen_season)} / {_day_label(chosen_day)} "
                "has no time blocks."
            ),
            "blocks": [],
        }

    return {
        "ok": True,
        "blocks": blocks,
        "season": _season_label(chosen_season),
        "day_type": _day_label(chosen_day),
        "dropped_seasons": other_seasons,
        "dropped_day_types": other_days,
        "lossy": bool(other_seasons or other_days),
    }


def describe_projection(result: dict) -> str:
    """One line for the preset description and the audit trail."""
    if not result.get("ok"):
        return result.get("error", "conversion failed")
    parts = [
        f"{len(result['blocks'])} blocks from {result['season']} / {result['day_type']}"
    ]
    if result.get("dropped_seasons"):
        parts.append("other seasons not included: " + ", ".join(result["dropped_seasons"]))
    if result.get("dropped_day_types"):
        parts.append("other day types not included: " + ", ".join(result["dropped_day_types"]))
    return "; ".join(parts)
