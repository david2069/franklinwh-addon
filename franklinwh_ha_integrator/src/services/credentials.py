"""Where a gateway's cloud credentials come from. One answer, one place.

There are two stores. `gateway_credentials` is the real one — encrypted at
rest, audited, and what the poller reads. `gateways.credentials_json` is a
legacy column the registry has been migrating away from for as long as it has
existed, and `refresh_gateway_profile` blanks it on every run by passing
`credentials={}` to `upsert_gateway`.

The Gateways tab's "Validate" button read only the legacy column. So on an
install whose credentials live in the modern store — which is every install
after a profile refresh — it reported "No credentials stored. Save credentials
first." while the Edit Gateway dialog beside it displayed "✅ Credentials
stored", because that dialog asked the other store. Both were reporting
honestly about different tables.

Resolution order is the registry's, because the registry is the one that has
to be right for polling to work at all:

  1. `gateway_credentials`, keyed by full serial
  2. the legacy `credentials_json` column, which is migrated forward on sight
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def resolve(gw: dict | None) -> dict:
    """Credentials for one gateway row: {"email", "password", "source"}.

    `source` is "stored", "legacy" (just migrated) or "none" — the caller can
    say which, rather than guessing why an empty result is empty.
    """
    from src.services import db

    gw = gw or {}
    full_serial = (gw.get("full_serial") or gw.get("short_id") or "").strip()
    if not full_serial:
        return {"email": "", "password": "", "source": "none"}

    try:
        creds = await db.get_credentials(full_serial)
    except Exception:
        logger.debug("credentials: lookup failed for %s", full_serial[:8], exc_info=True)
        creds = None

    if creds and creds.get("email"):
        return {"email": creds.get("email", ""),
                "password": creds.get("password", ""),
                "source": "stored"}

    raw = gw.get("credentials_json") or "{}"
    try:
        legacy = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        legacy = {}

    email = (legacy or {}).get("email", "")
    password = (legacy or {}).get("password", "")

    if email and password:
        # Migrated on sight, as the registry has always done — so the next
        # reader finds it in the store everything else uses.
        try:
            await db.upsert_credentials(full_serial, email, password, source="migration")
            logger.info("[%s] migrated credentials out of the legacy column", full_serial)
        except Exception:
            logger.debug("credentials: could not migrate %s", full_serial, exc_info=True)
        return {"email": email, "password": password, "source": "legacy"}

    return {"email": "", "password": "", "source": "none"}


async def present(gw: dict | None) -> bool:
    """Whether this gateway has usable credentials in either store."""
    resolved = await resolve(gw)
    return bool(resolved["email"] and resolved["password"])
