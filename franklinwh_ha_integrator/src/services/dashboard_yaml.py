"""
Home Assistant dashboard card YAML for this integration's entities.

Entity ids come from Home Assistant's own registry (the entity survey), never
from fwhai's prediction: HA can rename an entity or suffix it `_2`, and a card
built from guessed ids shows "Entity not available" rows. Each registry row is
matched to its EntityDef by unique_id — the longest slug the unique_id ends
with, so `battery_power` never matches `power`.

Output is plain `entities` cards, which every HA install renders without a
custom card. Themes are left out: they are personal.
"""
from __future__ import annotations

from typing import Iterable

from src.models.entities import AGATE_ENTITIES, BATTERY_ACCESSORY_ENTITIES

#: Card order and which entity definitions land in each.
GROUPS = [
    ("Power & Energy", lambda e: e.state_group in ("power", "energy", "solar")),
    ("Battery",        lambda e: e.state_group == "battery"),
    ("Controls",       lambda e: e.is_control or e.state_group == "control"),
    ("Status",         lambda e: e.state_group in ("status", "smart_circuits", "device")),
]
DIAGNOSTICS = "Diagnostics"


def _defs():
    return [(e, False) for e in AGATE_ENTITIES] + [(e, True) for e in BATTERY_ACCESSORY_ENTITIES]


def match_slug(unique_id: str, slugs: Iterable[str]) -> str | None:
    """The longest slug that `unique_id` ends with, as `_<slug>`."""
    best = None
    for s in slugs:
        if unique_id == s or unique_id.endswith("_" + s):
            if best is None or len(s) > len(best):
                best = s
    return best


def group_entities(ours: list[dict], include_diagnostics: bool = False) -> list[tuple[str, list[str]]]:
    """[(card title, [entity_id, ...]), ...] in card order, empty cards dropped."""
    defs = {e.slug: (e, is_bat) for e, is_bat in _defs()}
    buckets: dict[str, list[str]] = {t: [] for t, _ in GROUPS}
    buckets[DIAGNOSTICS] = []
    seen = set()
    for row in sorted(ours, key=lambda r: r.get("entity_id", "")):
        eid = row.get("entity_id") or ""
        if not eid or row.get("disabled") or eid in seen:
            continue
        slug = match_slug(row.get("unique_id") or "", defs)
        if not slug:
            continue
        seen.add(eid)
        e, is_bat = defs[slug]
        if e.entity_category in ("diagnostic", "config"):
            buckets[DIAGNOSTICS].append(eid)
            continue
        if is_bat:
            buckets["Battery"].append(eid)
            continue
        for title, test in GROUPS:
            if test(e):
                buckets[title].append(eid)
                break
        else:
            buckets[DIAGNOSTICS].append(eid)
    order = [t for t, _ in GROUPS] + ([DIAGNOSTICS] if include_diagnostics else [])
    return [(t, buckets[t]) for t in order if buckets[t]]


def _entities_card(title: str, ids: list[str], indent: str = "") -> list[str]:
    out = [f"{indent}type: entities", f"{indent}title: {title}",
           f"{indent}show_header_toggle: false", f"{indent}state_color: true",
           f"{indent}entities:"]
    out += [f"{indent}  - entity: {eid}" for eid in ids]
    return out


def render(groups: list[tuple[str, list[str]]], layout: str = "grouped",
           title: str = "FranklinWH") -> str:
    if not groups:
        return ""
    if layout == "single":
        ids = [eid for _, g in groups for eid in g]
        return "\n".join(_entities_card(title, ids)) + "\n"
    lines = ["type: vertical-stack", "cards:"]
    for t, ids in groups:
        card = _entities_card(f"{title} — {t}", ids, indent="    ")
        card[0] = "  - " + card[0].lstrip()
        lines += card
    return "\n".join(lines) + "\n"
