"""A plain-language account of how a gateway is set up.

"You have your gateway set up as follows" — the same ground the CLI's
`discover` output covers, assembled for the web UI.

Reads the stored profile rather than calling the cloud: discovery is
install-centric and already cached by `refresh_gateway_profile`, so a page load
should not cost a multi-call round trip. Live state (mode, SoC, grid) comes from
the running service where one exists.

The profile can be stale — it is refreshed on demand, not on a timer — so the
summary always reports when it was captured. A setup summary that silently
describes an April install as though it were current is worse than no summary,
and that is exactly what this codebase did until the refresh existed.
"""
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# PTO — Permission To Operate. Without it the utility has not authorised grid
# interaction: the owner has app access and the battery runs, but nothing may
# import from or export to the grid. It explains a whole class of "why is my
# battery doing nothing" that looks like a software fault and is not.
PTO_EXPLANATION = (
    "Permission To Operate has not been granted by your utility. The system "
    "runs, but grid import and export stay disabled until it is issued."
)


def _flag(profile: dict, key: str) -> bool:
    return bool(profile.get(key))


def _solar_description(profile: dict) -> Optional[str]:
    """How solar is coupled, in words rather than flags."""
    if not _flag(profile, "has_solar"):
        return None
    parts = []
    if _flag(profile, "mppt_enabled"):
        parts.append("DC-coupled (MPPT)")
    if _flag(profile, "remote_solar") or _flag(profile, "has_apbox"):
        parts.append("remote PV via aPBox")
    if not parts:
        parts.append("AC-coupled")
    return ", ".join(parts)


def build_summary(gateway: dict, live: Optional[dict] = None) -> dict:
    """Assemble the summary. Pure — no I/O, so it is trivially testable."""
    try:
        profile = json.loads(gateway.get("profile_json") or "{}")
    except json.JSONDecodeError:
        profile = {}
    live = live or {}

    pto = (profile.get("pto_date") or "").strip()
    warnings: list[str] = []
    if not profile:
        warnings.append(
            "No profile captured yet — run a discovery refresh to populate this."
        )
    if not pto:
        warnings.append(PTO_EXPLANATION)
    if not _flag(profile, "has_solar"):
        warnings.append("No solar detected on this gateway.")

    apower_count = profile.get("apower_count") or 0
    total_kwh = profile.get("total_kwh")

    return {
        "identity": {
            "name": profile.get("gateway_name") or gateway.get("name") or "",
            "model": profile.get("model") or gateway.get("model") or "",
            "model_name": profile.get("model_name") or "",
            "sku": profile.get("sku") or "",
            "generation": profile.get("generation"),
            "firmware": profile.get("firmware") or "",
            "site_name": profile.get("site_name") or "",
            "activated_at": profile.get("activated_at") or "",
        },
        "storage": {
            "apower_count": apower_count,
            "total_kwh": total_kwh,
            "total_rated_kw": profile.get("total_rated_kw"),
            # Spelled out because "1 unit, 13.6 kWh" is what a person wants,
            # not two fields they must combine themselves.
            "description": (
                f"{apower_count} aPower"
                + ("s" if apower_count != 1 else "")
                + (f", {total_kwh} kWh" if total_kwh else "")
                if apower_count else "No aPower units detected"
            ),
        },
        "solar": {
            "present": _flag(profile, "has_solar"),
            "coupling": _solar_description(profile),
            "remote_pv": _flag(profile, "remote_solar") or _flag(profile, "has_apbox"),
        },
        "accessories": {
            "smart_circuits": _flag(profile, "has_smart_circuits"),
            "smart_circuit_count": profile.get("smart_circuit_count") or 0,
            "generator": _flag(profile, "has_generator"),
            # Whether the SPAN integration is CONFIGURED in the FranklinWH
            # app — not whether a panel exists. A panel can be installed and
            # unconfigured, and this flag reads 0 for it
            # (DEF-SPAN-FLAG-IS-CONFIG-NOT-DETECTION). Reporting it as presence,
            # which 0.6.21 did, tells such an owner they have no panel.
            "span_configured": _flag(profile, "span_configured"),
            "ahub": _flag(profile, "has_ahub"),
            "mac1": _flag(profile, "has_mac1"),
            "apbox": _flag(profile, "has_apbox"),
        },
        "grid": {
            "profile_name": profile.get("grid_profile_name") or "",
            "feed_max_kw": profile.get("feed_max_kw"),
            "import_max_kw": profile.get("import_max_kw"),
            "three_phase": _flag(profile, "three_phase"),
            # The field that explains the most support questions.
            "pto_date": pto or None,
            "pto_granted": bool(pto),
        },
        "operating": {
            "mode": live.get("work_mode_desc") or live.get("mode") or None,
            "soc": live.get("battery_soc"),
            "run_status": live.get("run_status_desc") or None,
            "grid_connected": live.get("grid_connection_state") == "Connected"
            if live.get("grid_connection_state") is not None else None,
        },
        "location": {
            "site_address": profile.get("site_address") or "",
            "timezone": profile.get("timezone") or "",
            "country_code": (profile.get("country_code") or "").split(",")[0] or "",
        },
        "provenance": {
            # Always surfaced. The profile refreshes on demand, so "when was
            # this true" is part of the answer, not a footnote.
            "discovered_at": profile.get("_discovered_at"),
            "profile_version": profile.get("_profile_version"),
            "installer": profile.get("installer_company") or "",
        },
        "warnings": warnings,
    }
