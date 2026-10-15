# Changelog

All notable changes to FranklinWH HA Integrator are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

Structure loosely follows [Keep a Changelog](https://keepachangelog.com/). Releases from 0.6.1 onward are not git-tagged; the version is recorded by `VERSION` and the release commits.

---

## [0.6.80] - 2026-10-16


### Changed

- **New installs publish MQTT under `fwhai/…`** instead of `franklinwh/…`,
  which the FranklinWH Modbus Bridge also used — its add-on received fwhai's
  commands and fwhai received its (`operating_mode` ran on both). Existing
  installs keep `franklinwh`. The MQTT Admin topic-root setting now takes
  effect at the next start; before, it was saved and never read. Home
  Assistant entities follow a change automatically; automations written
  against raw topics would need updating.

### Fixed

- **Command topics ignored the topic root.** Each entity's `command_topic`
  was built from a template that hard-coded `franklinwh/`, so under any other
  root Home Assistant sent commands where fwhai was not listening. The MQTT
  heartbeat topic had the same fault.
- **Deleting a gateway left its device in Home Assistant**, every entity
  unavailable, because its retained discovery configs stayed on the broker.
  They are now cleared on delete (only that gateway's own identifiers).
- **Dispatch Power in Home Assistant stopped at 5.0 kW**, one aPower's
  rating. Its maximum is now the installed fleet's rating — the figure the
  Control tab already used.
- **Smart Dispatch read load sensors as kW regardless of unit** and dropped
  anything at or above 100 as an "energy accumulator": a sensor in watts read
  60 W as 60 kW and dropped 1500 W. The sensor's unit is now used (W, kW, MW);
  energy units are rejected as not-power; the old guess applies only to
  sensors that report no unit.

## [0.6.79] - 2026-10-15


### Changed

- **The Home Assistant device is now "FranklinWH Cloud <short id>".** One
  device per integration per aGate (owner decision 2026-10-15): fwhai and the
  FranklinWH Modbus Bridge published the same identifier,
  `franklinwh_<SERIAL>`, and Home Assistant merged them into one device of 155
  entities. Installs on the stock name template move to the new name now —
  renaming keeps the device and every entity's history. A name you chose
  yourself is left alone.
- **New installs publish their device as `franklinwh_cloud_<SERIAL>`.**
  Existing installs keep `franklinwh_<SERIAL>` until switched with MQTT Admin →
  Device identifier, because a new identifier makes Home Assistant create a new
  device: entities keep their ids and history, but device-based automations
  need re-pointing.

### Fixed

- **The ghost-device sweep could have claimed another integration's device.**
  It treated any `franklinwh_…` identifier as fwhai's, which after the Modbus
  Bridge's move to `franklinwh_modbus_<SERIAL>` would have listed its device
  for removal. Only identifiers fwhai has published count, and a device whose
  `sw_version` names another producer is never touched.

## [0.6.78] - 2026-10-15


### Changed

- **Admin → Setup Features says what a tick means.** Feature modules read
  "Ticked = on"; sidebar tabs read "Ticked = HIDDEN", and each hidden tab
  carries a Hidden tag. The two sat one above the other with opposite
  meanings, which is how Solar Setup stayed hidden on a site with solar.
- **A warning when solar is installed but Solar Setup is switched off**,
  naming which setting does it (Solar Forecast module off, or the tab ticked
  to hide) with a one-click fix to save. Live solar output counts as
  evidence, since hardware discovery can fail.

---

Older releases: [full changelog](https://github.com/david2069/franklinwh-addon/blob/main/CHANGELOG-full.md)
