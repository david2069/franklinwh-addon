"""The dispatch codes, in one place, with what each one actually does.

Before this, the id→label map was written out six times — gateway_service,
api_control, the schedule tab's JS, two maps in smart_dispatch.js, and three
hand-typed <option> lists in schedule.html. They had already drifted: some
listed six codes, some five, none listed 0, and none said what any of them did.

A bare label is not enough. "Home Loads" names what the block powers and hides
the consequence: the battery is left alone and surplus solar goes to the grid.
That is a deliberate export strategy — solar sold, battery preserved — and
choosing it from a dropdown reading "Home Loads (1)" is guesswork.

Values are the authoritative set from franklinwh_cloud.const.tou.dispatchCodeType
(0, 1, 2, 3, 6, 7, 8). 4 and 5 are not defined; the gateway rejects them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DispatchCode:
    id: int
    label: str            # the short name, as the gateway and app show it
    intent: str           # what choosing it is *for*
    description: str      # solar, battery and grid behaviour
    charges_battery: bool
    discharges_battery: bool
    exports_surplus: bool
    # The cloud library's own one-liner for this id, from
    # franklinwh_cloud.const.tou.DISPATCH_CODES. Kept verbatim so a
    # disagreement between our prose and the gateway's is visible rather than
    # silently ours — reading only the enum, and not this map, is how code 1
    # came to be described as leaving the battery idle when it does not.
    cloud_description: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


DISPATCH_CODES: dict[int, DispatchCode] = {
    0: DispatchCode(
        id=0,
        label="Custom",
        intent="Hand-built block",
        description=(
            "No dispatch behaviour of its own — the block carries a custom or "
            "predefined configuration, so what the battery does depends "
            "entirely on that configuration rather than on the code. The "
            "gateway reports no expected run state, so a schedule using it "
            "cannot be health-checked."
        ),
        charges_battery=False,
        discharges_battery=False,
        exports_surplus=False,
    ),
    1: DispatchCode(
        id=1,
        label="Home Loads",
        intent="Export Solar to Grid",
        cloud_description="aPower to home",
        description=(
            "Solar and the battery power the house — the aPower discharges to "
            "cover home loads, but never charges, and never discharges to the "
            "grid. Any solar the house does not consume is exported. Choose it "
            "to sell surplus generation without sending stored energy out with "
            "it."
        ),
        charges_battery=False,
        discharges_battery=True,
        exports_surplus=True,
    ),
    2: DispatchCode(
        id=2,
        cloud_description="aPower on standby",
        label="Standby",
        intent="Park the battery",
        description=(
            "The battery does nothing: it will not charge, and it will not "
            "supply the house even while the house is drawing. Home loads come "
            "from solar and the grid. On a price-based schedule this is almost "
            "never right — during an expensive block it buys at peak what the "
            "battery was already holding."
        ),
        charges_battery=False,
        discharges_battery=False,
        exports_surplus=True,
    ),
    3: DispatchCode(
        id=3,
        cloud_description="aPower charges from solar",
        label="Solar Charging",
        intent="Charge from Solar",
        description=(
            "Solar charges the battery and powers the house. The grid is never "
            "used to charge. Once the battery is full, surplus solar exports."
        ),
        charges_battery=True,
        discharges_battery=False,
        exports_surplus=True,
    ),
    6: DispatchCode(
        id=6,
        cloud_description="Self-consumption",
        label="Self Consumption",
        intent="Use your own energy first",
        description=(
            "The battery charges from surplus solar and discharges to cover "
            "home loads, minimising both import and export. The sensible "
            "default for most of the day."
        ),
        charges_battery=True,
        discharges_battery=True,
        exports_surplus=True,
    ),
    7: DispatchCode(
        id=7,
        cloud_description="aPower to home/grid",
        label="Grid Export",
        intent="Sell stored energy",
        description=(
            "The battery discharges to the house and to the grid, on top of "
            "whatever solar is already exporting. Worth it only when the export "
            "rate beats what that stored energy would save by displacing an "
            "import later — this is the mode that empties the battery."
        ),
        charges_battery=False,
        discharges_battery=True,
        exports_surplus=True,
    ),
    8: DispatchCode(
        id=8,
        cloud_description="aPower charges from solar/grid",
        label="Grid Charge",
        intent="Buy energy to store",
        description=(
            "The battery charges from the grid as well as from solar, "
            "regardless of what the energy costs. Use it in the cheapest "
            "window to fill before an expensive one."
        ),
        charges_battery=True,
        discharges_battery=False,
        exports_surplus=False,
    ),
}

# The ids the gateway accepts. 4 and 5 are undefined.
VALID_DISPATCH_IDS = frozenset(DISPATCH_CODES)


def label_for(dispatch_id) -> str:
    code = DISPATCH_CODES.get(_as_int(dispatch_id))
    return code.label if code else f"Dispatch {dispatch_id}"


def intent_for(dispatch_id) -> str:
    code = DISPATCH_CODES.get(_as_int(dispatch_id))
    return code.intent if code else f"Dispatch {dispatch_id}"


def description_for(dispatch_id) -> str:
    code = DISPATCH_CODES.get(_as_int(dispatch_id))
    return code.description if code else "Unknown dispatch code."


def as_list() -> list[dict]:
    """Ordered for a dropdown: the ids as the gateway numbers them."""
    return [DISPATCH_CODES[i].as_dict() for i in sorted(DISPATCH_CODES)]


def labels() -> dict[int, str]:
    """The plain id→label map, for callers that only need the short name."""
    return {i: c.label for i, c in DISPATCH_CODES.items()}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
