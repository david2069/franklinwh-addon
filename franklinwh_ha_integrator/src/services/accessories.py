"""What is actually installed on a gateway, from whatever source knows.

The setup wizard first read `profile.has_*` and nothing else, so on a gateway
whose profile never carried those keys — which is every gateway registered
before the registration path started writing them — it showed "Solar ? Smart
circuits ? Generator ? aPBox ?" and told the user nothing they did not already
know.

The Gateways tab had the better answer all along: take the profile flag *or*
the live telemetry, because a relay that is closed and a circuit that is
reporting power are evidence regardless of what the profile says. This is that
logic, in one place, used by both.

Three states, and the difference matters:

* ``True``  — the profile says so, or the hardware is reporting it.
* ``False`` — the profile explicitly says it is not there.
* ``None``  — nobody said, and nothing is reporting. Unknown, not absent.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _live_solar(data: dict) -> bool:
    """Solar is present if it is generating or its relay is closed."""
    current = (data.get("current") or {})
    if current.get("solar_production") not in (None, "", 0, 0.0):
        return True
    relays = (data.get("power") or {}).get("relays") or {}
    if relays.get("solar1") or relays.get("pv2"):
        return True
    return bool((data.get("solar_hardware") or {}).get("mppt_status"))


def _live_generator(data: dict) -> bool:
    if (data.get("status") or {}).get("generator_enabled"):
        return True
    if data.get("generator_enabled"):
        return True
    return bool(((data.get("power") or {}).get("relays") or {}).get("generator"))


def _live_smart_circuits(data: dict) -> bool:
    return any(str(k).startswith("smart_circuit_") for k in data)


def _live_apbox(data: dict) -> bool:
    return bool(((data.get("power") or {}).get("relays") or {}).get("apbox"))


#: accessory -> (profile key, live detector)
DETECTORS = {
    "solar": ("has_solar", _live_solar),
    "smart_circuits": ("has_smart_circuits", _live_smart_circuits),
    "generator": ("has_generator", _live_generator),
    "apbox": ("has_apbox", _live_apbox),
}

#: The accessory type ids the cloud uses in its own accessory list. The CLI's
#: `accessories` command keys off exactly these two.
ACCESSORY_TYPE_NAMES = {3: "generator", 4: "smart_circuits"}


def _from_items(profile: dict) -> dict[str, dict]:
    """Accessories the cloud has already named, keyed by our type name.

    `discover()` returns an AccessoryItem per registered accessory, carrying
    the serial and the product name — "Smart Circuits V1-AU". This is the
    cloud's own inventory of what is fitted, so it beats inference from a
    relay. Registration discarded it until 0.6.70, so older profiles have no
    `accessory_items` and fall through to the flags below.
    """
    found: dict[str, dict] = {}
    for item in (profile.get("accessory_items") or []):
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").strip().lower() \
            or ACCESSORY_TYPE_NAMES.get(item.get("type_id"), "")
        if kind:
            found.setdefault(kind, item)
    return found


def _profile_corroboration(profile: dict, name: str) -> bool:
    """Non-boolean profile fields that only exist when the thing is fitted.

    A site with three smart circuits has `smart_circuit_count: 3` whether or
    not `has_smart_circuits` was ever written.
    """
    if name == "smart_circuits":
        return bool(profile.get("smart_circuit_count")) or profile.get("sc_version") is not None
    if name == "solar":
        return bool(profile.get("mppt_enabled") or profile.get("ct_split_pv")
                    or profile.get("remote_solar"))
    return False


def detect(profile: dict | None, last_data: dict | None) -> dict[str, Any]:
    """Each accessory as True / False / None, with where the answer came from."""
    profile = profile or {}
    data = last_data or {}

    inventory = _from_items(profile)

    result: dict[str, Any] = {}
    for name, (key, live) in DETECTORS.items():
        reported = profile.get(key)

        try:
            seen = live(data)
        except Exception:
            logger.debug("accessories: live check for %s failed", name, exc_info=True)
            seen = False

        item = inventory.get(name)

        if item:
            # The cloud's own accessory list, with the product name on it.
            # Nothing outranks being told the serial of the fitted unit.
            result[name] = {
                "present": True,
                "source": "inventory",
                "label": (item.get("name") or "").strip(),
                "serial": (item.get("serial") or "").strip().upper(),
            }
        elif seen:
            # Hardware that is reporting outranks a profile that says nothing,
            # and outranks one that says no — the relay is the ground truth.
            result[name] = {"present": True,
                            "source": "reported" if reported else "detected"}
        elif reported:
            result[name] = {"present": True, "source": "profile"}
        elif _profile_corroboration(profile, name):
            # `has_smart_circuits` was never written, but the circuit count was.
            result[name] = {"present": True, "source": "profile"}
        elif reported is None:
            result[name] = {"present": None, "source": "unknown"}
        else:
            result[name] = {"present": False, "source": "profile"}

    # Detail worth showing beside the badge, where the profile has it.
    sc = result.get("smart_circuits")
    if sc and sc["present"] and profile.get("smart_circuit_count"):
        sc.setdefault("label", "")
        sc["detail"] = f"{int(profile['smart_circuit_count'])} circuits"
        if profile.get("sc_version"):
            sc["detail"] += f" · V{int(profile['sc_version'])}"

    return result


def names(profile: dict | None, last_data: dict | None) -> list[str]:
    """Just the accessories that are present — the Gateways tab's shape."""
    return [k for k, v in detect(profile, last_data).items() if v["present"]]
