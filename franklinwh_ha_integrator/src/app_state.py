"""Application state singleton — populated during lifespan startup by
`src.main`, read by routes. Extracted from `src.main` in v0.4.1
(2026-08-05) to eliminate a circular import that had been silently
breaking the Release CI on Python 3.12 since 2026-04.

Root cause of the CI failure:
- Modules like `src/routes/api_mqtt.py` did `from src.main import
  get_app_state` at module scope.
- `src.main` batch-imports every route module at line 465 — during
  that batch, if any route module re-enters `src.main` to grab
  `get_app_state`, Python 3.12's partial-module machinery raises
  `AttributeError: partially initialized module 'src.routes.api_mqtt'
  has no attribute 'router'`. Only /api/health ends up registered.
- Python 3.14 (my local) was more permissive and hid the bug.

Fix: this file has no dependencies on anything, so no cycle can form.
`src.main` still owns the actual lifecycle write to `app_state`; every
route just reads via `get_app_state()`."""
from __future__ import annotations


# Single mutable dict shared across the whole process. Populated by
# `src/main.py` lifespan startup; read-only for everyone else.
app_state: dict = {}


def get_app_state() -> dict:
    return app_state
