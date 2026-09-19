# Changelog

All notable changes to FranklinWH HA Integrator are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

---

## [0.6.54] - 2026-09-19

### Fixed

- **A site generating 4.35 kW of solar had no Solar Power entity.**
  `get_entities_for_profile` defaulted every `has_*` hardware flag to `False`,
  so a profile that simply never carried the key published none of the entities
  behind it. The gateway this was found on had **no `has_*` key at all**, which
  silently removed Solar Power, Daily Solar Energy and two other solar sensors
  — leaving nothing to feed solar production into the Energy Dashboard.

  Unknown now hides nothing; only an explicit `False` hides, which is the rule
  `CLAUDE.md` already states under `[BKL-CAPABILITY-GATING]`. Hiding a control
  the hardware lacks is tidiness. Hiding a measurement the hardware is
  producing is data loss.

  Grid was unaffected: `Grid Power`, `Daily Grid Import` and `Daily Grid Export`
  carry no hardware gate and were publishing correctly throughout.


### Added

- **`scripts/ci_local.sh`** — runs what CI runs, in a throwaway virtualenv.
  GitHub Actions minutes are exhausted until 2026-10-01, so runs fail before
  they start and "check CI after pushing" verifies nothing for twelve days. The
  working venv is not a substitute: it mounts `../franklinwh-cloud` over the
  installed package, so it can pass against library code no published version
  contains, and it runs whichever Python is default — 3.14 here, while CI uses
  3.11 and 3.12. The script builds a clean environment from `requirements.txt`
  on CI's Python and runs CI's exact pytest invocation.

  `CLAUDE.md` now also records how to poll CI correctly — by the pushed commit
  SHA, since `gh run list --limit 1` returns the newest run that *exists* and
  has already reported a stale green for a commit whose own run had not been
  created — and where to find the reason when a red run has no logs.


## [0.6.53] - 2026-09-19

### Removed

- **The "Last Update" sensor.** It published a timestamp of the last poll, so
  it changed every 30 seconds — a recorder row per poll, and a device history
  view that moved every time it was opened. Nothing could be done with the
  value: every other entity already carries its own freshness, and the
  integration's liveness is better told by Last Restart, which moves only when
  it should. Those non-churning timestamps are kept.

  Its retained discovery config is tombstoned rather than merely dropped.
  Deleting the definition alone would leave the entity to be rebuilt from the
  broker on the next Home Assistant restart, which is how the duplicate devices
  in this changelog got there.

### Verified

- The device block, read back from Home Assistant's own MQTT diagnostics rather
  than from our side of the wire:

  ```
  name        FranklinWH aGate X-01-AU 0091
  model       aGate X-01-AU
  sw_version  V12R02B30D06_260304 (FHAI: v0.6.52)
  hw_version  AGT-R1V1-AU · 1× aPower
  identifiers franklinwh_10060006A02F24170091
  ```

  All 85 entities are receiving state, and Home Assistant reports no issues
  against the device.

## [0.6.52] - 2026-09-19

### Changed

- **Hardware is the gateway SKU and the aPower count, without the aPower part
  numbers** — as asked for, and as it should have been in 0.6.50:

  ```
  Hardware: AGT-R1V1-AU · 1× aPower
  ```

  The part numbers were the wrong call on length alone. FEM's equivalent line
  is 30 characters and renders in full; one aPower SKU landed at 31, two at 51
  and three at 72, which wraps or clips depending on panel width. Every fleet
  size now stays within 30, and the test asserts that rather than trusting it.

---

Older releases: [full changelog](https://github.com/david2069/franklinwh-addon/blob/main/CHANGELOG-full.md)
