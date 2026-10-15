"""
Energy-flow totals for Reporting, from one cloud energy response.

Both Reporting sub-tabs (Cloud Reporting and Energy Flow) show the same seven
flows — solar, battery and grid each into home, battery or grid — and they used
to compute them separately in the browser. They disagreed: Energy Flow divided
Day (Hourly) power samples by 1 instead of 12, and neither read the per-source
arrays Week/Month/Year/Lifetime return, so those periods had no breakdown.

Two response shapes, field meanings per franklinwh_cloud.const.energy_fields:

* Day (getPowerByDay) — 288 five-minute power samples (kW) per flow, e.g.
  ``powerSolarHomeArray``. Energy is the sample sum / 12.
* Week/Month/Year/Lifetime (getFhpElectricData) — kWh per bucket: cloud
  totals (``kwhSuArray`` …) plus per-source splits (``kwhSolarLoadArray``,
  ``soChBatArray`` …). Buckets not yet reached are ``null``, not 0.

For a period the headline totals are the cloud's own (the figures the app and
``franklinwh-cli energy`` show); the splits beneath them run 1-2.5% short of
those totals, which is the cloud's arithmetic, not ours.
"""

SAMPLES_PER_HOUR = 12

# flow -> (Day power array, period kWh array)
_FLOWS = {
    "solar_home":   ("powerSolarHomeArray", "kwhSolarLoadArray"),
    "solar_bat":    ("powerSolarFhpArray",  "soChBatArray"),
    "solar_grid":   ("powerSolarGirdArray", "soOutGridArray"),
    "bat_home":     ("powerFhpHomeArray",   "kwhFhpLoadArray"),
    "bat_grid":     ("powerFhpGirdArray",   "batOutGridArray"),
    "grid_bat":     ("powerGirdFhpArray",   "gridChBatArray"),
    "grid_home":    ("powerGirdHomeArray",  "kwhGridLoadArray"),
    "gen_bat":      ("powerGenFhpArray",    "genChBatArray"),
    "gen_home":     ("powerGenHomeArray",   "kwhGenLoadArray"),
}

# Period totals, as the cloud reports them
_PERIOD_TOTALS = {
    "solar":     "kwhSuArray",
    "discharge": "kwhFhpDiArray",
    "charge":    "kwhFhpChgArray",
    "import":    "kwhUtiInArray",
    "export":    "kwhUtiOutArray",
    "home":      "kwhLoadArray",
}


def _values(data: dict, key: str) -> list:
    arr = data.get(key)
    if not isinstance(arr, list):
        return []
    return [v for v in arr if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _rnd(v: float) -> float:
    return round(v, 3)


def flow_summary(data: dict) -> dict:
    """Totals and per-flow kWh for one Reporting response.

    ``hasData`` is False when the cloud returned nothing for the period, so the
    page can say so instead of showing zeros. ``isDetailed`` is True when the
    per-source breakdown is available.
    """
    data = data if isinstance(data, dict) else {}
    is_day = isinstance(data.get("powerSolarHomeArray"), list)

    if is_day:
        f = {k: sum(_values(data, day)) / SAMPLES_PER_HOUR for k, (day, _) in _FLOWS.items()}
        f["v2l_bat"] = sum(_values(data, "powerV2lFhpArray")) / SAMPLES_PER_HOUR
        f["v2l_home"] = sum(_values(data, "powerV2lHomeArray")) / SAMPLES_PER_HOUR
        totals = {
            "solar":     f["solar_home"] + f["solar_bat"] + f["solar_grid"],
            "discharge": f["bat_home"] + f["bat_grid"],
            "charge":    f["solar_bat"] + f["grid_bat"] + f["gen_bat"] + f["v2l_bat"],
            "import":    f["grid_home"] + f["grid_bat"],
            "export":    f["solar_grid"] + f["bat_grid"],
            "home":      f["solar_home"] + f["bat_home"] + f["grid_home"]
                         + f["gen_home"] + f["v2l_home"],
        }
        has_data = len(data["powerSolarHomeArray"]) > 0
        detailed = has_data
    else:
        f = {k: sum(_values(data, period)) for k, (_, period) in _FLOWS.items()}
        totals = {k: sum(_values(data, key)) for k, key in _PERIOD_TOTALS.items()}
        has_data = any(_values(data, key) for key in _PERIOD_TOTALS.values())
        detailed = any(_values(data, period) for _, period in _FLOWS.values())

    return {
        "solar": {
            "total": _rnd(totals["solar"]),
            "toHome": _rnd(f["solar_home"]),
            "toBattery": _rnd(f["solar_bat"]),
            "toGrid": _rnd(f["solar_grid"]),
        },
        "battery": {
            "total": _rnd(totals["discharge"]),
            "charged": _rnd(totals["charge"]),
            "toHome": _rnd(f["bat_home"]),
            "toGrid": _rnd(f["bat_grid"]),
            "fromSolar": _rnd(f["solar_bat"]),
            "fromGrid": _rnd(f["grid_bat"]),
        },
        "grid": {
            "total": _rnd(totals["import"]),
            "exported": _rnd(totals["export"]),
            "toHome": _rnd(f["grid_home"]),
            "toBattery": _rnd(f["grid_bat"]),
            "exportFromSolar": _rnd(f["solar_grid"]),
            "exportFromBattery": _rnd(f["bat_grid"]),
        },
        "home": {
            "total": _rnd(totals["home"]),
            "fromSolar": _rnd(f["solar_home"]),
            "fromBattery": _rnd(f["bat_home"]),
            "fromGrid": _rnd(f["grid_home"]),
        },
        "isDetailed": detailed,
        "hasData": has_data,
    }
