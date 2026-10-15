"""Logging filters that keep a steady-state log readable.

A cloud rate limit or outage repeats the same warning on every poll — 30 s
apart, for hours (2026-10-15: 13,000+ dropped polls, each logged twice).
"""
from __future__ import annotations

import logging
import time


class RepeatCollapseFilter(logging.Filter):
    """Pass the first of each identical message, then hold repeats for
    `window_s`; the next one after the window carries the count it stood for.

    Applies only to loggers under `prefixes`, at `min_level` and above.
    """

    def __init__(self, prefixes=("franklinwh_cloud",), window_s: float = 600,
                 min_level: int = logging.WARNING):
        super().__init__()
        self.prefixes = tuple(prefixes)
        self.window_s = window_s
        self.min_level = min_level
        self._seen: dict[tuple, list] = {}   # key -> [first_shown_at, suppressed]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < self.min_level or not record.name.startswith(self.prefixes):
            return True
        key = (record.name, record.levelno, record.getMessage())
        now = time.monotonic()
        entry = self._seen.get(key)
        if entry is None or now - entry[0] >= self.window_s:
            held = entry[1] if entry else 0
            self._seen[key] = [now, 0]
            if held:
                record.msg = f"{record.getMessage()} (repeated {held}× in the last {int(self.window_s // 60)} min)"
                record.args = None
            return True
        entry[1] += 1
        return False


def install() -> None:
    """Quiet the routine scheduler lines; collapse repeated library warnings."""
    # "Running job … / executed successfully" every 30 s for the SD ticker.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    flt = RepeatCollapseFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(flt)
