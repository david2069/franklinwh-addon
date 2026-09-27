"""Repair gateway model names that were built from a hardware version.

Discovery used to paste the hardware version after "aGate" whenever the cloud
sent no model name, producing "aGate 102" — 102 being `sysHdVersion`, an
integer, presented to the user as a product name. That derivation was fixed,
but `gateways.model` is only ever written at registration: nothing re-resolves
it, so every install that registered before the fix kept the fabricated name
indefinitely. Fixing the derivation quietly did nothing for the people who
already had the wrong value on screen.

The fabricated name carries the number that produced it, so it is exactly
recoverable: `aGate 102` → hardware version 102 → the catalog's `aGate X`.

Deliberately narrow. Only the exact fabricated shape is touched, and only when
the catalog has an answer for that version — a model name that is not
`aGate <digits>` came from the cloud or the catalog and is left alone.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: "aGate 102" and nothing else. A real model carries letters after the space
#: ("aGate X", "aGate X-01-AU"); only an all-digit tail is the hardware
#: version wearing a product name.
FABRICATED = re.compile(r"^\s*aGate\s+(\d+)\s*$", re.IGNORECASE)


def fabricated_hw_version(model: str | None) -> int | None:
    """The hardware version a fabricated model name was built from, or None."""
    match = FABRICATED.match(model or "")
    return int(match.group(1)) if match else None


async def repair_gateway_models() -> int:
    """Re-resolve fabricated model names from the device catalog.

    Returns how many rows were corrected. Best-effort: a version the catalog
    does not know is left as it is rather than replaced with a guess.
    """
    from src.services import db

    try:
        gateways = await db.get_all_gateways() or []
    except Exception:
        logger.debug("model repair: could not read gateways", exc_info=True)
        return 0

    repaired = 0
    for gw in gateways:
        hw_version = fabricated_hw_version(gw.get("model"))
        if hw_version is None:
            continue

        try:
            row = await db.get_device_model(hw_version)
        except Exception:
            logger.debug("model repair: catalog lookup failed for %s", hw_version, exc_info=True)
            continue

        name = ((row or {}).get("name") or "").strip()
        if not name:
            logger.info(
                "model repair: %r is a hardware version, but the catalog has no "
                "entry for %s — leaving it alone", gw.get("model"), hw_version,
            )
            continue

        try:
            await db.update_gateway_model(gw.get("short_id"), name)
        except Exception:
            logger.warning("model repair: could not update %s", gw.get("short_id"), exc_info=True)
            continue

        repaired += 1
        logger.info(
            "model repair: %s model %r was the hardware version — now %r",
            gw.get("short_id"), gw.get("model"), name,
        )

    return repaired
