"""What a period of electricity actually costs.

Split out of earnings_tracker because that module answered a narrower question:
how much energy was bought and sold. A bill is that plus the charges that accrue
whether or not a single kWh moves, and those were stored and never used —
`supply_charge_day`, `metering_fee` and `network_fixed_fee` appeared in the
schema, in the edit form and in no calculation anywhere.

For a plan with a 158.631 c/day supply charge, omitting it does not make
"today's cost" slightly low; it makes it low by a fixed amount every single
day — about $579 a year — and on a small bill the standing charge is frequently
the largest line on it.

Two conventions, both deliberate:

**Everything is cents.** Rates are c/kWh and fixed charges are c/day, matching
how retailers quote them, so a figure read off a bill can be typed in as it
appears. Conversion to dollars happens here and nowhere else.

**Rates are used exactly as entered.** No adjustment is ever applied to a rate
on its way through this module — not for tax, not for anything. Whether a
figure includes tax is the user's business and depends on their circumstances;
this code has no basis to decide it and does not try. That is the whole rule,
and it is asserted in the tests so nobody later adds a "helpful" adjustment.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

CENTS_PER_DOLLAR = 100.0

# Fixed charges, all c/day.
FIXED_CHARGE_FIELDS = ("supply_charge_day", "metering_fee", "network_fixed_fee")

# A supply charge is quoted in cents and is typically 60–250 c/day. A value in
# this range is almost certainly dollars typed into a cents field — a 100x
# understatement rather than the 100x overstatement the old $/day field invited.
# Zero is excluded: unset is not a mistake.
SUSPICIOUSLY_LOW_SUPPLY_CHARGE_C_DAY = 5.0


@dataclass(frozen=True)
class CostBreakdown:
    """A period's cost, itemised. All values AUD, all positive except `net_aud`."""

    import_aud: float       # energy bought; NEGATIVE when paid to import
    export_aud: float       # feed-in revenue; NEGATIVE when charged to export
    standing_aud: float     # supply + metering + network, accrued per day
    days: float             # days the standing charges were applied over
    net_aud: float          # import + standing - export; negative means in credit

    def to_dict(self) -> dict:
        return {k: round(v, 4) for k, v in asdict(self).items()}


# ── direction and sign ───────────────────────────────────────────────────────
#
# Direction is ALWAYS explicit. It is the channel for dynamic pricing
# (general/import vs feed-in/export) and the window type plus rate_kind for
# static plans. It is never inferred from a sign.
#
# Sign, where it exists, means the price moved AGAINST the normal direction of
# its channel. Both happen in the Australian market:
#
#   import, positive   you pay to import               — the usual case
#   import, negative   you are PAID to import          — rare; heavy oversupply
#   export, positive   you are paid to export          — an ordinary feed-in
#   export, negative   you PAY to export               — two-way tariffs,
#                                                        increasingly common
#                                                        around midday
#
# Static plans see the same four cases, but time-based rather than live:
# per hour, day or season, with direction carried by the window. The arithmetic
# below is identical for both, which is the point — one set of rules, whether
# the price came from an API or a form.
#
# Nothing here clamps a rate or a result at zero. build_breakdown used to,
# which floored away both of the interesting cases: a negative import cost and
# a negative export revenue were each silently discarded, so a household paid
# to consume saw nothing and a household charged to export saw nothing.

CHANNEL_IMPORT = "import"
CHANNEL_EXPORT = "export"


def import_cost_aud(kwh: float, rate_c_kwh: float) -> float:
    """Cost of energy bought, at the rate exactly as given.

    Negative when the rate is negative: the market paid the household to
    consume. That is a real outcome and is carried through rather than floored.
    """
    return max(0.0, float(kwh)) * float(rate_c_kwh) / CENTS_PER_DOLLAR


def export_revenue_aud(kwh: float, rate_c_kwh: float) -> float:
    """Revenue from energy exported, at the rate exactly as given.

    Negative when the rate is negative: the household paid to export. Same
    arithmetic as the import side on purpose — import and export are not
    adjusted relative to one another, and 28 c/kWh means 28 c/kWh.
    """
    return max(0.0, float(kwh)) * float(rate_c_kwh) / CENTS_PER_DOLLAR


def energy_value_aud(kwh: float, rate_c_kwh: float, channel: str) -> float:
    """One entry point for both directions, signed as a cash flow.

    Returns what the household is OUT OF POCKET: positive costs money,
    negative earns it. The channel decides the direction, the sign decides
    whether the price ran the usual way, and the two are independent — which
    is why direction is never read off the sign.
    """
    if str(channel).lower() == CHANNEL_EXPORT:
        return -export_revenue_aud(kwh, rate_c_kwh)
    return import_cost_aud(kwh, rate_c_kwh)


def standing_charges_aud(service: dict | None, days: float) -> float:
    """Charges that accrue per day regardless of consumption. Fields are c/day."""
    if not service or days <= 0:
        return 0.0

    cents_per_day = 0.0
    for field in FIXED_CHARGE_FIELDS:
        try:
            cents_per_day += float(service.get(field) or 0.0)
        except (TypeError, ValueError):
            continue
    return cents_per_day * float(days) / CENTS_PER_DOLLAR


def supply_charge_warning(service: dict | None) -> str | None:
    """Flag a supply charge that looks like dollars entered into a cents field.

    Returns None when the value is plausible or unset. A warning rather than a
    correction: 3.0 could be an unusual c/day figure, and silently multiplying
    someone's number by 100 is worse than asking.
    """
    if not service:
        return None
    try:
        value = float(service.get("supply_charge_day") or 0.0)
    except (TypeError, ValueError):
        return None

    if value <= 0 or value >= SUSPICIOUSLY_LOW_SUPPLY_CHARGE_C_DAY:
        return None
    return (
        f"Supply charge is set to {value:g} c/day (${value * 365 / CENTS_PER_DOLLAR:,.2f} a year), "
        f"which is unusually low. This field is in cents — if your bill says "
        f"${value:g} per day, enter {value * CENTS_PER_DOLLAR:g}."
    )


def build_breakdown(
    *,
    import_aud: float,
    export_aud: float,
    service: dict | None,
    days: float,
) -> CostBreakdown:
    """Assemble a period's cost from its energy totals and the service record."""
    standing = standing_charges_aud(service, days)
    # Not clamped. max(0.0, ...) floored away both of the cases that matter
    # under negative pricing: a household PAID to import saw its credit
    # discarded, and one CHARGED to export saw its cost discarded. Either way
    # the money vanished and the bill could not be reconciled.
    return CostBreakdown(
        import_aud=round(float(import_aud), 6),
        export_aud=round(float(export_aud), 6),
        standing_aud=round(standing, 6),
        days=round(float(days), 4),
        net_aud=round(float(import_aud) + standing - float(export_aud), 6),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full tariff model — ported from the Modbus Bridge's tariff editor.
#
# The plan shapes below existed in the Bridge and had no representation here, so
# a bill computed by FWHAI silently omitted them:
#
#   * a peak-demand charge, billed on the highest averaging interval in a
#     window rather than on energy at all
#   * a battery-export bonus, credited per kWh exported inside a window
#   * a two-way export charge, levied per kWh exported inside a window above a
#     free daily allowance
#   * a minimum monthly bill, which floors the whole thing
#
# Every window can be restricted by month and by weekday, because seasonal
# plans are the norm — AGL's peak season is Nov–Mar *and* Jun–Aug, so the mask
# is membership, not a range.
# ─────────────────────────────────────────────────────────────────────────────

# Demand charge bases, matching the Bridge's selector.
DEMAND_BASIS_KW_RATE_DAYS = "kw_rate_days"    # peak kW x rate x days in period
DEMAND_BASIS_KW_RATE = "kw_rate"              # peak kW x rate, once per period
DEMAND_BASES = (DEMAND_BASIS_KW_RATE_DAYS, DEMAND_BASIS_KW_RATE)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _mask(value) -> set[str]:
    """A CSV window mask. Empty means *every* — matching the form's own note,
    'no months/days selected = every month / every day'. Treating empty as
    'none' would silently switch off every window a user left unrestricted."""
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    return {str(i).strip().lower() for i in items if str(i).strip()}


def window_applies(service: dict | None, when, *, months_field: str,
                   days_field: str | None = None) -> bool:
    """Whether a window's month/day mask admits this moment."""
    if not service or when is None:
        return False
    months = _mask(service.get(months_field))
    if months and str(when.month) not in months and f"{when.month:02d}" not in months:
        return False
    if days_field:
        days = _mask(service.get(days_field))
        if days and WEEKDAY_KEYS[when.weekday()] not in days:
            return False
    return True


def _minutes(value) -> int | None:
    """'16:00' or '04:00 PM' to minutes past midnight."""
    if not value:
        return None
    text = str(value).strip().upper()
    suffix = ""
    for marker in ("AM", "PM"):
        if text.endswith(marker):
            suffix, text = marker, text[: -len(marker)].strip()
            break
    try:
        parts = text.split(":")
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if suffix == "PM" and hh != 12:
        hh += 12
    if suffix == "AM" and hh == 12:
        hh = 0
    return hh * 60 + mm


def in_window(start, end, when) -> bool:
    """Whether a local wall-clock moment falls inside a window.

    Handles the wrap past midnight, because an evening peak that runs to 01:00
    is a normal plan and a naive start <= t < end would exclude all of it.
    """
    s, e = _minutes(start), _minutes(end)
    if s is None or e is None or when is None:
        return False
    t = when.hour * 60 + when.minute
    return (s <= t < e) if s < e else (t >= s or t < e)


def window_matches(window: dict | None, when) -> bool:
    """Whether a utility_service_windows row admits this local moment.

    The row's own vocabulary: `months` is a CSV or NULL for all year, and
    `day_type` is one of all / everyday / weekdays / weekends.
    """
    if not window or when is None:
        return False
    months = _mask(window.get("months"))
    if months and str(when.month) not in months and f"{when.month:02d}" not in months:
        return False

    day_type = str(window.get("day_type") or "everyday").lower()
    is_weekend = when.weekday() >= 5
    if day_type == "weekdays" and is_weekend:
        return False
    if day_type == "weekends" and not is_weekend:
        return False
    return in_window(window.get("start_time"), window.get("end_time"), when)


def windows_of_type(windows: list[dict] | None, window_type: str) -> list[dict]:
    return [w for w in (windows or [])
            if isinstance(w, dict) and w.get("window_type") == window_type]


def demand_charge_aud(service: dict | None, window: dict | None,
                      peak_kw: float, days: float) -> float:
    """The peak-demand charge for one window.

    Billed on the highest demand recorded in the window, not on energy — so a
    single spike sets the charge for the whole period. The rate and the window
    come from utility_service_windows; how the peak is *measured* (the
    averaging interval) and how the rate is *applied* (the basis) are
    plan-level and come from the service.
    """
    if not window or peak_kw <= 0:
        return 0.0
    # A demand window has no credit reading — it is always a charge — so its
    # magnitude is what counts and a stray sign cannot invert the bill.
    try:
        rate_c = abs(float(window.get("rate") or 0.0))
    except (TypeError, ValueError):
        return 0.0
    if rate_c <= 0:
        return 0.0

    basis = str((service or {}).get("demand_charge_basis") or DEMAND_BASIS_KW_RATE_DAYS)
    multiplier = max(0.0, float(days)) if basis == DEMAND_BASIS_KW_RATE_DAYS else 1.0
    return float(peak_kw) * rate_c * multiplier / CENTS_PER_DOLLAR


def demand_interval_minutes(service: dict | None) -> int:
    """Total averaging window for the demand peak, in minutes.

    '30 min x2' is a single 60-minute average, not two separate half-hours —
    reading it as the former is what makes a short spike not set the charge.
    """
    if not service:
        return 30
    try:
        each = int(service.get("demand_interval_min") or 30)
        count = int(service.get("demand_interval_count") or 1)
    except (TypeError, ValueError):
        return 30
    return max(1, each) * max(1, count)


# A window's rate is always stored positive; this says which way the money
# goes. Sign is reserved for dynamic pricing, where a negative price is real
# data from the market rather than a statement about direction.
RATE_KIND_CREDIT = "credit"     # the retailer pays you — a feed-in tariff
RATE_KIND_CHARGE = "charge"     # you pay — a two-way export tariff
RATE_KINDS = (RATE_KIND_CREDIT, RATE_KIND_CHARGE)


def window_rate_signed(window: dict | None) -> float:
    """A window's rate as a signed figure: positive credits, negative charges.

    Signing happens here and nowhere else, so the rule has one home. A rate
    stored negative is honoured as a charge regardless of its kind — it can
    only have been entered under the old reading, and treating it as a credit
    would silently reverse someone's bill.
    """
    if not window:
        return 0.0
    try:
        rate = float(window.get("rate") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if rate < 0:
        return rate
    kind = str(window.get("rate_kind") or RATE_KIND_CREDIT).lower()
    return -rate if kind == RATE_KIND_CHARGE else rate


def export_window_aud(window: dict | None, kwh_in_window: float,
                      free_kwh: float = 0.0) -> float:
    """Value of energy exported inside an 'export' window.

    Positive is money owed *to* the household. A two-way tariff is the same
    window with rate_kind='charge', so one function covers both rather than two
    that could disagree about direction. A free allowance applies only to a
    charge — applying it to a credit would quietly withhold the first hundred
    kWh of someone's feed-in.
    """
    if not window or kwh_in_window <= 0:
        return 0.0
    rate_c = window_rate_signed(window)
    if rate_c == 0:
        return 0.0

    kwh = max(0.0, float(kwh_in_window))
    if rate_c < 0:
        kwh = max(0.0, kwh - max(0.0, float(free_kwh)))
    return kwh * rate_c / CENTS_PER_DOLLAR


def export_free_allowance_kwh(service: dict | None, days: float) -> float:
    """The free export allowance accrued over a period.

    Quoted per day and accrued across the billing period, so only export above
    `kWh/day x days` is charged. Applying it per day instead would charge a
    household that exports nothing for six days and a lot on the seventh.
    """
    if not service or days <= 0:
        return 0.0
    try:
        per_day = float(service.get("export_free_kwh_day") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, per_day) * max(0.0, float(days))


def apply_minimum_bill(net_aud: float, service: dict | None) -> float:
    """Floor the bill at the plan's minimum.

    Only ever raises. A plan in credit stays in credit unless a minimum is set,
    and a minimum of zero is not a minimum.
    """
    if not service:
        return net_aud
    try:
        minimum_c = float(service.get("min_monthly_bill_c") or 0.0)
    except (TypeError, ValueError):
        return net_aud
    if minimum_c <= 0:
        return net_aud
    return max(net_aud, minimum_c / CENTS_PER_DOLLAR)


def list_standing_charges_aud(charges: list[dict] | None, days: float,
                              month: int | None = None) -> float:
    """Extensible standing charges, on top of the three fixed columns.

    `basis` is per_day or per_period: a membership fee billed monthly is not
    the same shape as a supply charge, and treating it as c/day would multiply
    it by thirty.
    """
    if not charges or days <= 0:
        return 0.0
    total_c = 0.0
    for charge in charges:
        if not isinstance(charge, dict):
            continue
        months = _mask(charge.get("months"))
        if months and month is not None and str(month) not in months:
            continue
        try:
            amount = float(charge.get("amount_c") or 0.0)
        except (TypeError, ValueError):
            continue
        basis = str(charge.get("basis") or "per_day")
        total_c += amount * float(days) if basis == "per_day" else amount
    return total_c / CENTS_PER_DOLLAR
