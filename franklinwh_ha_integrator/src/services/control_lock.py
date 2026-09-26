"""Control Lock — a master enforcement switch with per-category locks.

FHAI's own UI puts Go Off-Grid behind a red modal: the utility grid and main
panels remain live, the aGate is not a UPS, loads upstream lose power, and
"I acknowledge the physical risks of forced islanding" must be ticked first.

Home Assistant got the same command as a bare dropdown option that fired
immediately. On 2026-09-20 it was selected while exploring options that
appeared not to work — they were reverting for want of a state echo, fixed in
0.6.55 — and the grid relay opened, leaving the site in Outage for about three
minutes.

Structure, so one switch does not have to mean everything:

* **Control Lock** (master) — when ON, enforcement is active. Turning it OFF
  suspends every category for a few minutes and then re-arms itself, which is
  the "unlock, act, forget about it" path.
* **Per-category locks** — Grid, Operating Mode, Battery Dispatch. Each says
  whether that category is guarded while the master is on.

Defaults guard the grid and nothing else. Locking mode or dispatch by default
would break existing automations on upgrade, and neither physically endangers
anyone; the grid relay does. They are offered because a site where automations
must never touch the battery is a real thing, not because they are dangerous.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: How long turning the master off suspends enforcement before it re-arms.
#: Mirrors the auto-relock the Smart Dispatch safeguards already use.
UNLOCK_TTL_SECONDS = 300

#: category -> {slug: matching values, or None for "any value"}
CATEGORIES: dict[str, dict[str, set[str] | None]] = {
    "grid": {"off_grid_mode": {"off-grid"}},
    "operating_mode": {"operating_mode": None},
    "battery_dispatch": {"dispatch_action": {"charge", "discharge", "stop"}},
}

#: Which categories are guarded out of the box.
DEFAULT_LOCKED = {"grid": True, "operating_mode": False, "battery_dispatch": False}

#: The switch entity slug for each category.
SLUGS = {
    "grid": "lock_grid",
    "operating_mode": "lock_operating_mode",
    "battery_dispatch": "lock_battery_dispatch",
}
MASTER_SLUG = "control_lock"

_state: dict[str, bool] = dict(DEFAULT_LOCKED)
_master_enforcing: bool = True
_suspended_until: float = 0.0


def category_for(slug: str, value) -> str | None:
    """The category this command belongs to, or None if it is not guarded."""
    for category, commands in CATEGORIES.items():
        if slug not in commands:
            continue
        allowed = commands[slug]
        if allowed is None or str(value).strip().lower() in allowed:
            return category
    return None


def set_master(enforcing: bool, now: float | None = None) -> None:
    """Master on re-arms immediately; master off suspends with a timeout."""
    global _master_enforcing, _suspended_until
    _master_enforcing = enforcing
    if enforcing:
        _suspended_until = 0.0
        logger.info("control lock: master ON — critical commands guarded again")
    else:
        _suspended_until = (now if now is not None else time.time()) + UNLOCK_TTL_SECONDS
        logger.warning(
            "control lock: master OFF — critical commands permitted for %ds, "
            "then it re-arms itself", UNLOCK_TTL_SECONDS,
        )


def set_category(category: str, locked: bool) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    _state[category] = locked
    logger.info("control lock: %s %s", category, "locked" if locked else "unlocked")


def is_enforcing(now: float | None = None) -> bool:
    """Whether the master is currently enforcing."""
    if _master_enforcing:
        return True
    return (now if now is not None else time.time()) >= _suspended_until


def is_locked(category: str) -> bool:
    return bool(_state.get(category, DEFAULT_LOCKED.get(category, False)))


def seconds_remaining(now: float | None = None) -> int:
    remaining = _suspended_until - (now if now is not None else time.time())
    return int(remaining) if remaining > 0 and not _master_enforcing else 0


def reset() -> None:
    """Back to defaults. For tests and for a clean start."""
    global _state, _master_enforcing, _suspended_until
    _state = dict(DEFAULT_LOCKED)
    _master_enforcing = True
    _suspended_until = 0.0


def check(slug: str, value, now: float | None = None) -> str | None:
    """None if the command may proceed, else why it was refused."""
    category = category_for(slug, value)
    if category is None:
        return None
    if not is_enforcing(now):
        return None
    if not is_locked(category):
        return None

    label = SLUGS[category].replace("lock_", "").replace("_", " ")
    extra = (
        " It physically disconnects the site from the grid."
        if category == "grid" else ""
    )
    return (
        f"{slug}={value!r} is refused: the Control Lock is on and '{label}' is "
        f"locked.{extra} Turn the Control Lock off — it re-arms after "
        f"{UNLOCK_TTL_SECONDS}s — or unlock that category. The FranklinWH "
        "Integrator UI asks for the same acknowledgement."
    )
