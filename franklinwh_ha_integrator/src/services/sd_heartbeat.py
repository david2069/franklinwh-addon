"""Smart Dispatch tick heartbeat (GH #36).

A liveness signal that means "the engine ran", kept strictly separate from
"the engine decided something new".

Why this exists
---------------
`scheduler_liveness` originally used `MAX(ts) FROM pricing_eval_log` as its
proof-of-life. That table is a CHANGE log — `evaluate_and_log` only writes a
row when `prev_action != action_key`. A healthy engine on a stable system
returns the same decision every tick and therefore writes nothing, so the
signal aged out and the watchdog rebuilt the scheduler every two minutes.
Observed live at restart #733, with `last_eval` frozen ~24 h in the past
while the engine was ticking perfectly and logging
"(same as prev — dedup)" each time.

Each rebuild called `shutdown(wait=False)`, cancelling in-flight jobs, so the
"cure" destroyed roughly a quarter of the ticks it was meant to protect.

The fix is to stop overloading a change log as a heartbeat. This module is
stamped on EVERY completed tick regardless of dedup.

Design notes
------------
* Deliberately tiny and dependency-free so both `smart_dispatch` (writer) and
  `scheduler_liveness` (reader) can import it without a cycle.
* In-process memory is sufficient and correct: the monitor task runs on the
  same uvicorn event loop as the engine. It is also the right scope — after a
  process restart there genuinely is no recent tick, and the startup grace
  period covers that window.
* Never raises. A heartbeat that can fail is worse than no heartbeat, since
  the failure would masquerade as a stall and trigger the exact destructive
  restart this exists to prevent.
"""
from __future__ import annotations

import time
from typing import Optional

# Monotonic so wall-clock adjustments (NTP step, DST, container clock skew)
# cannot fabricate a stall. The pricing_eval_log path had exactly that class of
# bug — a naive-UTC parse that was off by the container's offset.
_last_tick_monotonic: Optional[float] = None
_last_tick_wall: Optional[float] = None
_tick_count: int = 0


def mark_tick() -> None:
    """Record that a Smart Dispatch evaluation completed. Call unconditionally."""
    global _last_tick_monotonic, _last_tick_wall, _tick_count
    try:
        _last_tick_monotonic = time.monotonic()
        _last_tick_wall = time.time()
        _tick_count += 1
    except Exception:
        pass


def age_s() -> Optional[float]:
    """Seconds since the last completed tick, or None if none recorded yet.

    None means "no evidence either way" — callers must treat it as unknown,
    NOT as stale, or every fresh process would look dead on boot.
    """
    if _last_tick_monotonic is None:
        return None
    try:
        return max(0.0, time.monotonic() - _last_tick_monotonic)
    except Exception:
        return None


def last_tick_wall() -> Optional[float]:
    """Unix timestamp of the last tick, for display only. None if never ticked."""
    return _last_tick_wall


def tick_count() -> int:
    return _tick_count


def reset() -> None:
    """Clear all state. Tests only."""
    global _last_tick_monotonic, _last_tick_wall, _tick_count
    _last_tick_monotonic = None
    _last_tick_wall = None
    _tick_count = 0
