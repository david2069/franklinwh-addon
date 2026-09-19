"""
tou_validator.py — FranklinWH TOU plan validation helper.

Validates a get_tou_dispatch_detail() response dict against:
  1. tariffSettingFlag — TOU endpoints available on this gateway
  2. Non-empty strategyList — at least one season defined
  3. All 12 months covered — no gaps or duplicates
  4. Buy rates populated — each dayType has non-null peak/shoulder/valley rates
  5. No overlapping time blocks within a dayType's detailVoList

Usage:
    from src.services.tou_validator import validate_tou_plan
    result = validate_tou_plan(last_data["tou_schedule"])
    # result = {"valid": True, "issues": [], "tariff_type": 1, "nem_type": 0, ...}
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# electricityType enum from FranklinWH Cloud
TARIFF_TYPE_LABELS = {1: "TOU", 2: "Flat", 3: "Tiered"}
NEM_TYPE_LABELS    = {0: "NEM 2.0", 1: "NEM 3.0"}


def validate_tou_plan(schedule: dict | None) -> dict:
    """Validate a FranklinWH TOU dispatch schedule.

    Parameters
    ----------
    schedule : dict | None
        The full result dict from get_tou_dispatch_detail() — i.e. the value of
        ``response["result"]``.  Accepts None gracefully.

    Returns
    -------
    dict
        {
          "valid": bool,
          "issues": [str],           # human-readable problems (empty if valid)
          "tariff_type": int | None, # 1=TOU, 2=Flat, 3=Tiered
          "tariff_type_label": str,
          "nem_type": int | None,    # 0=NEM2, 1=NEM3
          "nem_type_label": str,
          "season_count": int,
          "tariff_setting_flag": bool,
          "tariff_company": str | None,
          "tariff_company_id": int | None,
        }
    """
    if not schedule:
        return _fail(["No TOU schedule data available — gateway not polled yet."])

    # Normalise: accept both raw API response and pre-unwrapped result dict
    if "result" in schedule:
        schedule = schedule["result"]

    template      = schedule.get("template") or {}
    strategy_list = schedule.get("strategyList") or []

    tariff_type       = template.get("electricityType")
    nem_type          = template.get("nemType")
    tariff_flag       = bool(schedule.get("tariffSettingFlag", True))
    company_id        = template.get("eletricCompanyId")
    company_name      = (template.get("eleCompanyFullName") or template.get("electricCompany") or "").strip()

    issues: list[str] = []

    # ── Check 1: TOU endpoints available ─────────────────────────────────────
    if not tariff_flag:
        issues.append(
            "TOU scheduling is not available on this gateway model (tariffSettingFlag=false)."
        )

    # ── Check 2: Non-empty strategy list ─────────────────────────────────────
    if not strategy_list:
        issues.append(
            "No seasons defined in the TOU schedule — open the Schedule tab and configure at least one season."
        )
        return _fail(issues, tariff_type=tariff_type, nem_type=nem_type,
                     company_id=company_id, company_name=company_name)

    # ── Check 3: All 12 months covered ───────────────────────────────────────
    month_issues = _check_months(strategy_list)
    issues.extend(month_issues)

    # ── Check 4: Buy rates populated ─────────────────────────────────────────
    rate_issues = _check_rates(strategy_list, tariff_type)
    issues.extend(rate_issues)

    # ── Check 5: No overlapping time blocks ──────────────────────────────────
    overlap_issues = _check_overlaps(strategy_list)
    issues.extend(overlap_issues)

    valid = len(issues) == 0
    return {
        "valid":                valid,
        "issues":               issues,
        "tariff_type":          tariff_type,
        "tariff_type_label":    TARIFF_TYPE_LABELS.get(tariff_type, "Unknown") if tariff_type else "Not set",
        "nem_type":             nem_type,
        "nem_type_label":       NEM_TYPE_LABELS.get(nem_type, "Unknown") if nem_type is not None else "Not set",
        "season_count":         len(strategy_list),
        "tariff_setting_flag":  tariff_flag,
        "tariff_company":       company_name or None,
        "tariff_company_id":    company_id if company_id and company_id != -1 else None,
    }


# ── Private helpers ───────────────────────────────────────────────────────────

def _fail(issues: list[str], **kwargs) -> dict:
    return {
        "valid": False, "issues": issues,
        "tariff_type": kwargs.get("tariff_type"),
        "tariff_type_label": TARIFF_TYPE_LABELS.get(kwargs.get("tariff_type"), "Unknown"),
        "nem_type": kwargs.get("nem_type"),
        "nem_type_label": NEM_TYPE_LABELS.get(kwargs.get("nem_type"), "Unknown"),
        "season_count": 0,
        "tariff_setting_flag": False,
        "tariff_company": kwargs.get("company_name") or None,
        "tariff_company_id": kwargs.get("company_id") or None,
    }


def _check_months(strategy_list: list) -> list[str]:
    """Return issues if months are missing, duplicated, or out of range."""
    seen: set[int] = set()
    issues: list[str] = []
    for season in strategy_list:
        raw = str(season.get("month", ""))
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit():
                continue
            m = int(part)
            if not 1 <= m <= 12:
                issues.append(f"Season '{season.get('seasonName', '?')}' has invalid month: {m}.")
                continue
            if m in seen:
                issues.append(f"Month {m} appears in more than one season.")
            seen.add(m)
    missing = set(range(1, 13)) - seen
    if missing:
        issues.append(
            f"Not all 12 months are covered. Missing: {sorted(missing)}. "
            "Add seasons or adjust month assignments in the Schedule tab."
        )
    return issues


def _check_rates(strategy_list: list, tariff_type: int | None) -> list[str]:
    """Return issues if buy rates are missing for the configured tariff type."""
    issues: list[str] = []
    # Flat rate (type=2) — only valley rate needed; TOU/Tiered need peak at minimum
    required_keys = ["eleticRatePeak"] if tariff_type != 2 else ["eleticRateValley"]
    for season in strategy_list:
        sname = season.get("seasonName", "?")
        for dt in season.get("dayTypeVoList", []):
            dtname = dt.get("dayName", "?")
            for key in required_keys:
                if dt.get(key) is None:
                    issues.append(
                        f"Season '{sname}' / {dtname}: buy rate '{key}' is not set."
                    )
    return issues


def _check_overlaps(strategy_list: list) -> list[str]:
    """Return issues if any time blocks within a dayType overlap."""
    issues: list[str] = []
    for season in strategy_list:
        sname = season.get("seasonName", "?")
        for dt in season.get("dayTypeVoList", []):
            dtname = dt.get("dayName", "?")
            blocks = dt.get("detailVoList", [])
            intervals: list[tuple[int, int]] = []
            for blk in blocks:
                try:
                    s = _hhmm_to_min(blk.get("startHourTime", "00:00"))
                    e = _hhmm_to_min(blk.get("endHourTime", "24:00"))
                    if e == 0:
                        e = 1440  # midnight
                    intervals.append((s, e))
                except Exception:
                    continue
            intervals.sort()
            for i in range(1, len(intervals)):
                if intervals[i][0] < intervals[i - 1][1]:
                    issues.append(
                        f"Season '{sname}' / {dtname}: overlapping time blocks detected."
                    )
                    break
    return issues


def _hhmm_to_min(hhmm: str) -> int:
    parts = hhmm.replace(":", " ").split()
    return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
