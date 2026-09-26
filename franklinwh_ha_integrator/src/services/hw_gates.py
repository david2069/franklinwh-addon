"""What the hardware gates are doing, and what the user has overridden.

A gate that silently removes entities is indistinguishable from a bug. A site
generating 4.35 kW of solar published no Solar Power sensor, no Daily Solar
Energy sensor, and nothing anywhere said why — the profile simply never carried
`has_solar`, and the filter read missing as absent.

So the gates are no longer only applied; they are reportable. Each one says
what the gateway reported, what the user chose, what was decided, and which
entities it controls. FEM's `cli_discovery.py` prints the same facts as a
HARDWARE FLAGS block; this is that, live and overridable.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: Where the overrides live. A single JSON object keyed by gate name.
CONFIG_KEY = "entity_gate_overrides"

#: Human labels, so the panel does not show profile key names.
LABELS = {
    "solar": "Solar",
    "generator": "Generator",
    "smart_circuits": "Smart circuits",
    "apbox": "aPBox",
}


async def load_overrides() -> dict:
    """User overrides, or an empty dict. Never raises."""
    from src.services import db

    try:
        raw = await db.get_config_value(CONFIG_KEY, "") or ""
        if not raw:
            return {}
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except Exception:
        logger.debug("hw_gates: could not read overrides", exc_info=True)
        return {}


async def save_override(gate: str, choice: str) -> None:
    """Set one gate to auto/show/hide."""
    from src.models.entities import GATE_AUTO, GATE_HIDE, GATE_SHOW, HW_GATES
    from src.services import db

    if gate not in HW_GATES:
        raise ValueError(f"unknown gate {gate!r}")
    if choice not in (GATE_AUTO, GATE_SHOW, GATE_HIDE):
        raise ValueError(f"unknown choice {choice!r}")

    overrides = await load_overrides()
    if choice == GATE_AUTO:
        overrides.pop(gate, None)
    else:
        overrides[gate] = choice

    await db.set_config_value(CONFIG_KEY, json.dumps(overrides))
    logger.info("hw_gates: %s set to %s", gate, choice)


async def report(profile: dict) -> list[dict]:
    """Every gate, what it decided, and the entities it controls."""
    from src.models.entities import AGATE_ENTITIES, HW_GATES, gate_decision

    overrides = await load_overrides()
    rows = []
    for gate in HW_GATES:
        decision = gate_decision(gate, profile or {}, overrides)
        controlled = [e for e in AGATE_ENTITIES if e.hw_requires == gate]
        decision["label"] = LABELS.get(gate, gate)
        decision["entity_count"] = len(controlled)
        decision["entities"] = sorted(e.name for e in controlled)
        rows.append(decision)
    return rows
