"""What is already in Home Assistant under a FranklinWH name, and who owns it.

Two things leave entities behind that collide with this integration:

1. **Another FranklinWH integration** — richo's HACS `franklin_wh` is the common
   one. While its config entry exists, Home Assistant will not let anything else
   own `sensor.franklinwh_*`; ours arrive as `…_2`. Disabling the integration is
   NOT enough, because a disabled config entry still holds its entity ids. Only
   deleting the entry frees them.

2. **A previous install of this add-on** — its registry rows survive, and if the
   entity-id prefix has since changed they are orphaned: no config entry owns
   them, nothing will ever update them, and they sit in Home Assistant as
   permanently-unavailable entities that automations still reference.

This module only reports. Adopting an id means renaming a registry row, and
that is a bulk rewrite of the user's registry — a separate, previewed,
explicitly confirmed action.

Why it is worth doing at all: long-term statistics are keyed on `entity_id`, so
an entity that takes over an old id inherits its recorder history. Automations
and dashboards reference entity ids too. Adoption is the difference between
keeping years of Energy dashboard data and starting again.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Entity ids that look like they belong to a FranklinWH system, whoever owns
#: them. Deliberately broad — this only decides what to *report*.
FRANKLIN_PATTERNS = (
    re.compile(r"franklin", re.I),
    re.compile(r"\bapower\b", re.I),
    re.compile(r"\bagate\b", re.I),
)

#: The platform this integration's entities are published through.
OUR_PLATFORM = "mqtt"


def _looks_franklin(entity_id: str, name: str = "") -> bool:
    haystack = f"{entity_id} {name or ''}"
    return any(p.search(haystack) for p in FRANKLIN_PATTERNS)


async def _registry() -> list[dict]:
    from src.services.integration_conflicts import _ws_command

    replies = await _ws_command([{"type": "config/entity_registry/list"}])
    result = (replies[0] or {}).get("result") or []
    return [e for e in result if isinstance(e, dict)]


async def _config_entries() -> dict:
    from src.services.integration_conflicts import _ws_command

    replies = await _ws_command([{"type": "config_entries/get"}])
    result = (replies[0] or {}).get("result") or []
    return {e.get("entry_id"): e for e in result if isinstance(e, dict)}


async def survey(our_unique_prefix: str = "") -> dict:
    """Report FranklinWH entities in Home Assistant and who owns each.

    Returns three buckets:

    * `ours`      — published by this integration, nothing to do
    * `foreign`   — owned by a live config entry belonging to something else;
                    their ids cannot be taken while that entry exists
    * `orphaned`  — no config entry owns them; the ids are free, and the
                    entities are dead weight in Home Assistant until reused

    Read-only. `checked` is False when Home Assistant could not be reached,
    which must not be reported as "nothing found".
    """
    try:
        entities = await _registry()
        entries = await _config_entries()
    except Exception as exc:
        logger.debug(f"entity adoption: registry unavailable: {exc!r}")
        return {"ok": True, "checked": False, "ours": [], "foreign": [], "orphaned": []}

    ours, foreign, orphaned = [], [], []

    for ent in entities:
        entity_id = ent.get("entity_id") or ""
        if not _looks_franklin(entity_id, ent.get("original_name") or ent.get("name") or ""):
            continue

        record = {
            "entity_id": entity_id,
            "unique_id": ent.get("unique_id") or "",
            "platform": ent.get("platform") or "",
            "name": ent.get("name") or ent.get("original_name") or "",
            "disabled": bool(ent.get("disabled_by")),
        }

        entry_id = ent.get("config_entry_id")
        owner = entries.get(entry_id) if entry_id else None

        is_ours = (
            record["platform"] == OUR_PLATFORM
            and our_unique_prefix
            and record["unique_id"].startswith(our_unique_prefix)
        )

        if is_ours:
            ours.append(record)
        elif owner is None:
            # No config entry owns it. Either the integration was removed, or
            # this add-on published it under a prefix that has since changed.
            record["reason"] = "no integration owns this entity"
            orphaned.append(record)
        else:
            record["owner_domain"] = owner.get("domain") or ""
            record["owner_title"] = owner.get("title") or ""
            record["owner_disabled"] = bool(owner.get("disabled_by"))
            foreign.append(record)

    if foreign or orphaned:
        logger.info(
            f"Entity survey: {len(ours)} ours, {len(foreign)} owned elsewhere, "
            f"{len(orphaned)} orphaned"
        )

    return {
        "ok": True,
        "checked": True,
        "ours": ours,
        "foreign": foreign,
        "orphaned": orphaned,
        # Spelled out because the distinction is not obvious and decides what
        # the user has to do before anything can be adopted.
        "note": (
            "Entity ids owned by another integration cannot be taken while its "
            "config entry exists. Disabling that integration is not enough — a "
            "disabled entry still holds its ids. It has to be deleted."
        ),
    }


async def statistics_ages(entity_ids: list[str]) -> dict:
    """How far back Home Assistant's long-term statistics go for each entity.

    This is what decides whether adopting an old entity id is worth anything.
    Statistics are keyed on `entity_id`, so an entity that takes over an old id
    inherits that history — the Energy dashboard keeps its years of data. An
    orphan with no statistics is only worth adopting to save editing
    automations; one with three years of history is worth real effort.

    Returns `{entity_id: {"has_statistics": bool, "oldest": iso|None,
    "days": int|None, "unit": str}}`.
    """
    from src.services.integration_conflicts import _ws_command

    if not entity_ids:
        return {}

    try:
        replies = await _ws_command([{"type": "recorder/list_statistic_ids"}])
    except Exception as exc:
        logger.debug(f"entity adoption: statistics unavailable: {exc!r}")
        return {}

    known = {}
    for row in (replies[0] or {}).get("result") or []:
        if isinstance(row, dict) and row.get("statistic_id") in entity_ids:
            known[row["statistic_id"]] = row

    if not known:
        return {eid: {"has_statistics": False, "oldest": None, "days": None, "unit": ""}
                for eid in entity_ids}

    # One query for the earliest bucket. start_time omitted means "everything",
    # and `period: month` keeps the response small — we only need the first row.
    try:
        stats = await _ws_command([{
            "type": "recorder/statistics_during_period",
            "statistic_ids": list(known),
            "period": "month",
            "start_time": "1970-01-01T00:00:00+00:00",
        }])
        buckets = (stats[0] or {}).get("result") or {}
    except Exception as exc:
        logger.debug(f"entity adoption: statistics_during_period failed: {exc!r}")
        buckets = {}

    from datetime import datetime, timezone

    out = {}
    for eid in entity_ids:
        row = known.get(eid)
        if not row:
            out[eid] = {"has_statistics": False, "oldest": None, "days": None, "unit": ""}
            continue

        oldest, days = None, None
        series = buckets.get(eid) or []
        if series:
            first = series[0] or {}
            raw = first.get("start")
            try:
                # `start` is epoch ms in recent cores, ISO in older ones.
                if isinstance(raw, (int, float)):
                    dt = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                oldest = dt.isoformat()
                days = max(0, (datetime.now(timezone.utc) - dt).days)
            except Exception:
                pass

        out[eid] = {
            "has_statistics": True,
            "oldest": oldest,
            "days": days,
            "unit": row.get("unit_of_measurement") or "",
        }
    return out


#: Entity ids separate words with "_", "." and "-". \b is useless here:
#: underscore is a word character, so r"\bsoc\b" never matches
#: "sensor.fw_battery_soc". These bracket a token explicitly instead.
_SEP = r"(?:^|[^a-z0-9])"
_END = r"(?:$|[^a-z0-9])"

#: Fragments that identify what an entity measures, whoever named it. Ordered
#: most specific first — "smart_circuit_2" must not match the bare "circuit"
#: rule before its numbered one.
_ROLE_HINTS = (
    ("smart_circuit", re.compile(r"smart[_.\-]?circuit[_.\-]?(\d+)", re.I)),
    ("battery_soc", re.compile(_SEP + r"(?:soc|state[_.\-]?of[_.\-]?charge|battery[_.\-]?level)" + _END, re.I)),
    ("battery_power", re.compile(r"battery[_.\-]?(?:power|kw)" + _END, re.I)),
    ("solar_power", re.compile(_SEP + r"(?:solar|pv)[_.\-]?(?:power|kw|production)?" + _END, re.I)),
    ("generator", re.compile(_SEP + r"generator", re.I)),
    ("grid_status", re.compile(r"grid[_.\-]?(?:status|state|connection)", re.I)),
    ("grid_power", re.compile(_SEP + r"grid[_.\-]?(?:power|kw)?" + _END, re.I)),
    ("home_load", re.compile(_SEP + r"(?:home|house|load)[_.\-]?(?:power|kw|load)?" + _END, re.I)),
    ("operating_mode", re.compile(r"(?:work|operating|runtime)[_.\-]?mode", re.I)),
)


def _role_of(entity_id: str, name: str = "") -> tuple[str, str]:
    """(role, qualifier) for an entity, or ("", "") if nothing recognisable."""
    haystack = f"{entity_id} {name or ''}"
    for role, pattern in _ROLE_HINTS:
        m = pattern.search(haystack)
        if m:
            qualifier = m.group(1) if m.groups() and (m.group(1) or "").isdigit() else ""
            return role, qualifier
    return "", ""


def map_to_ours(foreign: list[dict], ours: list[dict]) -> list[dict]:
    """Pair each foreign/orphaned entity with the equivalent published here.

    The point is not adoption — often that is impossible — but telling someone
    whose automation drives `switch.franklinwh_smart_circuit_2` which entity now
    does that job, so they can fix the automation by hand.

    Matching is on what the entity MEASURES, not on its name: the two
    integrations name things differently, which is the whole problem. Domain
    must agree, so a sensor is never offered as the replacement for a switch.
    """
    index: dict[tuple[str, str, str], dict] = {}
    for ent in ours:
        role, qualifier = _role_of(ent["entity_id"], ent.get("name", ""))
        if role:
            domain = ent["entity_id"].split(".")[0]
            index.setdefault((role, qualifier, domain), ent)

    paired = []
    for ent in foreign:
        role, qualifier = _role_of(ent["entity_id"], ent.get("name", ""))
        domain = ent["entity_id"].split(".")[0]
        match = index.get((role, qualifier, domain)) if role else None
        paired.append({
            **ent,
            "role": role,
            "equivalent": match["entity_id"] if match else None,
            "equivalent_note": (
                None if match else
                "No equivalent published here — this may be hardware we do not "
                "expose, or named too differently to match automatically."
            ),
        })
    return paired
