"""Rename existing Home Assistant entities onto the current id scheme.

Home Assistant assigns an entity id once, at creation, and keeps it. So the
0.6.59 fix — publishing `object_id` so ids carry the gateway rather than the
device label — governs entities created from then on and leaves existing ones
alone. A site that already had `sensor.fhp_state_of_charge` still has it.

Renaming is the missing half, and it needs no new machinery: the entity
registry is writable over the same WebSocket that `integration_conflicts`
already authenticates against, and the *correct* id is derivable rather than
guessed. Because discovery publishes `object_id` equal to `unique_id`, the id
Home Assistant would give an entity created today is exactly
``{domain}.{unique_id}`` — so any entity of ours whose id differs from that is,
by definition, one created before the scheme it should be on.

Dry run by default. Renaming breaks any dashboard, script or automation that
refers to the old id — Home Assistant does not rewrite YAML — so the plan is
shown before anything moves.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Only ever touch entities this integration published.
OURS = "franklinwh"

#: The MQTT integration is the only platform we publish through.
PLATFORM = "mqtt"


def desired_entity_id(entry: dict) -> str | None:
    """The id this entity would be given if it were created today.

    Discovery sets `object_id` to the `unique_id`, and Home Assistant builds
    the entity id from `object_id`, so the two are the same by construction.
    """
    unique_id = (entry.get("unique_id") or "").strip()
    entity_id = (entry.get("entity_id") or "").strip()
    if not unique_id or "." not in entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    return f"{domain}.{unique_id}"


def is_ours(entry: dict) -> bool:
    if (entry.get("platform") or "") != PLATFORM:
        return False
    return (entry.get("unique_id") or "").lower().startswith(OURS)


async def plan() -> list[dict]:
    """Entities of ours whose id predates the current scheme.

    Read-only. Returns one row per rename, each with the reason.
    """
    from src.services.integration_conflicts import _ws_command

    try:
        replies = await _ws_command([{"type": "config/entity_registry/list"}])
    except Exception as exc:
        logger.warning("entity rename: could not read the registry — %r", exc)
        return []

    entries = []
    for reply in replies:
        result = reply.get("result")
        if isinstance(result, list):
            entries = result
            break

    renames = []
    for entry in entries:
        if not is_ours(entry):
            continue
        wanted = desired_entity_id(entry)
        current = entry.get("entity_id")
        if not wanted or wanted == current:
            continue
        renames.append({
            "from": current,
            "to": wanted,
            "unique_id": entry.get("unique_id"),
            "name": entry.get("original_name") or entry.get("name") or "",
        })

    logger.info("entity rename: %d of %d entities would change id",
                len(renames), sum(1 for e in entries if is_ours(e)))
    return renames


async def apply(renames: list[dict]) -> dict:
    """Perform the renames. Returns what moved and what did not.

    Each rename is its own command: one refusal — a target id already in use,
    typically held by a stale duplicate device — must not abandon the rest.
    """
    from src.services.integration_conflicts import _ws_command

    if not renames:
        return {"renamed": 0, "failed": [], "details": []}

    done, failed, details = 0, [], []
    for row in renames:
        try:
            replies = await _ws_command([{
                "type": "config/entity_registry/update",
                "entity_id": row["from"],
                "new_entity_id": row["to"],
            }])
            ok = bool(replies) and replies[0].get("success", False)
        except Exception as exc:
            ok = False
            replies = [{"error": {"message": str(exc)}}]

        if ok:
            done += 1
            details.append({**row, "status": "renamed"})
            logger.info("entity rename: %s → %s", row["from"], row["to"])
        else:
            why = ""
            if replies:
                why = (replies[0].get("error") or {}).get("message", "") or "refused"
            failed.append({**row, "error": why})
            details.append({**row, "status": "failed", "error": why})
            logger.warning("entity rename: %s → %s refused: %s",
                           row["from"], row["to"], why)

    return {"renamed": done, "failed": failed, "details": details}
