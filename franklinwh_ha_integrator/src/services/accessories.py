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


# ─────────────────────────────────────────────────────────────────────────
# Feature facets — what `franklinwh-cli discover` prints as "Feature Flags".
#
# The CLI has said "❌ Generator Module: Not installed" since before FHAI
# existed, while FHAI could only show a GEN relay reading 0 — which is what a
# fitted-but-idle generator also shows. Absence and idleness are different
# claims and the user could not tell them apart.
#
# Two facets, because they answer different questions:
#
#   hardware — what is physically fitted. Changes when someone visits the site.
#   setup    — how the system is configured. Changes from an app or a portal.
#
# The labels and the wording are the CLI's, deliberately: a user reading both
# must not have to work out whether "Not connected" here means what "Not
# installed" meant there.
# ─────────────────────────────────────────────────────────────────────────


def _solar_feature(p: dict, data: dict) -> tuple[bool, str]:
    detail = (p.get("solar_detail") or "").strip()
    present = bool(p.get("has_solar")) or _live_solar(data)
    if detail:
        return present, detail
    return present, "Installed" if present else "Not detected"


def _smart_circuit_feature(p: dict) -> tuple[bool, str]:
    """Count and names, as the CLI shows them — "V1, 2 circuits (Circuit 1, ...)"."""
    count = int(p.get("smart_circuit_count") or 0)
    if not count:
        present = bool(p.get("has_smart_circuits"))
        return present, "Installed" if present else "Not installed"

    label = f"{count} circuits"
    if p.get("sc_version"):
        label = f"V{int(p['sc_version'])}, {label}"
    names = [n for n in (p.get("sc_names") or []) if n]
    if names:
        label += f" ({', '.join(names)})"
    return True, label


def _grid_feature(p: dict) -> tuple[bool, str]:
    """Positive framing, so nobody has to read "Off-Grid: Grid-connected"."""
    if p.get("off_grid_simulated"):
        return False, "Simulated off-grid (contactor opened by user)"
    if p.get("off_grid_permanent"):
        return False, "Permanent off-grid (no utility service)"
    if p.get("off_grid"):
        return False, f"Grid outage detected (reason: {p.get('off_grid_reason', 0)})"
    return True, "Connected"


def features(profile: dict | None, last_data: dict | None = None) -> dict[str, list[dict]]:
    """The two facets, each a list of {key, label, present, detail}.

    `present` is a plain bool here rather than the tri-state `detect()` uses.
    These flags are written together at registration, so a missing one means
    the profile predates this code, not that the cloud declined to answer —
    and `stale` on the facet says so once instead of fifteen times.
    """
    p = profile or {}
    data = last_data or {}

    solar_present, solar_detail = _solar_feature(p, data)
    sc_present, sc_detail = _smart_circuit_feature(p)
    grid_present, grid_detail = _grid_feature(p)

    generator = bool(p.get("has_generator") or p.get("generator_enabled"))
    v2l = bool(p.get("v2l_enabled") or p.get("v2l_eligible"))

    hardware = [
        {"key": "solar", "label": "Solar",
         "present": solar_present, "detail": solar_detail},
        {"key": "smart_circuits", "label": "Smart Circuits",
         "present": sc_present, "detail": sc_detail},
        {"key": "generator", "label": "Generator Module",
         "present": generator,
         "detail": "Installed" if generator else "Not installed"},
        {"key": "apbox", "label": "Remote Solar (aPBox)",
         "present": bool(p.get("has_apbox") or p.get("remote_solar")),
         "detail": "Connected" if (p.get("has_apbox") or p.get("remote_solar"))
                   else "Not connected"},
        {"key": "ahub", "label": "aHub",
         "present": bool(p.get("has_ahub") or p.get("ahub_detected")),
         "detail": "Detected" if (p.get("has_ahub") or p.get("ahub_detected"))
                   else "Not detected"},
        {"key": "mac1", "label": "MAC-1 (MSA)",
         "present": bool(p.get("has_mac1") or p.get("mac1_detected")),
         "detail": "Detected" if (p.get("has_mac1") or p.get("mac1_detected"))
                   else "Not detected"},
        {"key": "mppt", "label": "MPPT (DC-coupled)",
         "present": bool(p.get("mppt_enabled")),
         "detail": "Enabled" if p.get("mppt_enabled") else "Not available"},
        {"key": "ct_split_grid", "label": "CT Split — Grid",
         "present": bool(p.get("ct_split_grid")),
         "detail": "Installed" if p.get("ct_split_grid") else "Not installed"},
        {"key": "ct_split_pv", "label": "CT Split — PV",
         "present": bool(p.get("ct_split_pv")),
         "detail": "Installed" if p.get("ct_split_pv") else "Not installed"},
        {"key": "v2l", "label": "V2L",
         "present": v2l,
         "detail": (p.get("v2l_note") or "").strip()
                   or ("Enabled" if p.get("v2l_enabled") else "Not enabled")},
    ]

    setup = [
        {"key": "grid_tied", "label": "Grid-Tied",
         "present": grid_present, "detail": grid_detail},
        {"key": "tariff", "label": "TOU / Tariff",
         "present": bool(p.get("tariff_configured")),
         "detail": "Configured" if p.get("tariff_configured") else "Not configured"},
        {"key": "pcs", "label": "PCS Power Control",
         "present": bool(p.get("pcs_enabled")),
         "detail": "Enabled" if p.get("pcs_enabled") else "Disabled"},
        {"key": "three_phase", "label": "Phase",
         "present": bool(p.get("three_phase")),
         "detail": "Three-phase" if p.get("three_phase") else "Single-phase"},
        {"key": "vpp", "label": "VPP Programme",
         "present": bool(p.get("vpp_enrolled")),
         "detail": "Enrolled" if p.get("vpp_enrolled") else "Not enrolled"},
    ]

    # One honest caveat instead of fifteen shrugs: a profile written before
    # these flags were stored reports every one of them as absent, which would
    # read as "nothing is installed" rather than "nobody asked yet".
    stale = "tariff_configured" not in p and "pcs_enabled" not in p

    return {"hardware": hardware, "setup": setup, "stale": stale}
