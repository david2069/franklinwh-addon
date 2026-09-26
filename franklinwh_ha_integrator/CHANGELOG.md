# Changelog

All notable changes to FranklinWH HA Integrator are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

---

## [0.6.70] - 2026-09-27

### Fixed

- **The setup wizard's Hardware step now shows the accessories, because it asks
  the screen that already knew.** 0.6.69 was still wrong: it kept its own
  hardware summary and read live telemetry from `gateway.last_data`, a column
  that does not exist, so every badge stayed at "?" on a site the Gateways tab
  described correctly one click away.

  The step now calls `_hydrate_gateway_fields()` — the Gateways tab's own
  hydrator — and renders what the dashboard row renders: the named accessories
  and the GRD / GEN / SOL contactor states, through the same
  `relayLabel` / `relayClass` / `relayTitle` helpers. There is no second
  derivation left to be wrong.

- **A gateway that is not polling reported no accessories at all.** The
  derivation sat inside `if live:`, so it never ran until the poller had
  produced a reading — which during first-run setup it has not. What is fitted
  does not depend on whether a poll is in flight.

- **The profile never reached the accessory list.** The derivation re-parsed
  `profile_json`, which `_hydrate_gateway_fields` pops thirty lines earlier, so
  it parsed `{}` on every call. `has_solar`, `has_generator` and the rest
  contributed nothing to the Gateways tab either — only live telemetry ever
  produced an accessory. It now reads the flags the same function already
  hydrated.

### Added

- **Registration keeps the accessories the cloud names.** `discover()` returns
  an `AccessoryItem` per registered accessory — serial, type and product name
  ("Smart Circuits V1-AU") — and the profile kept only the `has_*` booleans, so
  every screen could say whether something was fitted but never what. Stored as
  `accessory_items`, and preferred over inference: the cloud naming the fitted
  unit beats a relay state.

### Internal

- `tests/test_accessory_detection.py` covers the profile-only path with no
  registry, the live path, and that the wizard holds no derivation of its own.

## [0.6.69] - 2026-09-27

### Fixed

- **The setup wizard now shows what is actually installed, not four question
  marks.** The Hardware step rendered `Solar ? Smart circuits ? Generator ?
  aPBox ?` on a site that was generating 4.35 kW at the time, because it read
  `profile.has_*` and nothing else. Those keys are empty on every gateway
  registered before that path started writing them.

  The Gateways tab already had the better rule — the profile flag *or* live
  evidence — so it showed real information one click away. That rule is now
  `src/services/accessories.py`, used by both, and it reads the signals the
  hardware actually produces: solar output, MPPT status, relay states
  (`solar1`, `pv2`, `generator`, `apbox`), `generator_enabled`, and any
  `smart_circuit_*` key.

  Each badge now says where its answer came from — `live` for detected, a
  tooltip for reported — and the step lists the relay states underneath, as
  the Gateways tab does. A closed relay outranks a profile flag that says no;
  nothing reported and nothing running stays amber and unknown rather than
  claiming absence.

- **The red dot in the top bar.** The daemon chip tested `poll_status` inline
  against five values while the poller sets nine. `retrying`,
  `stale_window_dropped`, `auth_failed`, `circuit_breaker_tripped` and
  `no_credentials` all fell through to the red branch for colour and matched
  no label, so the chip rendered as a red dot with no text beside it — and
  `stale_window_dropped` happens roughly every other poll on this cloud
  account, which is why it looked permanent.

  Colour now comes from `pollSeverity()` and the label from a new
  `pollLabel()`, both in the store, so the chip cannot disagree with the globe
  icon again. A dropped stale reading reads "Waiting for fresh data" in amber,
  because it is the cloud library working as designed, not a fault.

- **The setup wizard can be closed.** The only exit was the word "Skip" in
  small grey text, which reads as "skip this step" rather than "leave" — so it
  was reported as impossible to exit. There is now a labelled `Close setup`
  button and a ✕ beside it, and Escape still works.

### Internal

- `tests/test_poll_status_always_has_a_label.py` scrapes every `poll_status`
  literal the source assigns and asserts each one has a label, so a new status
  cannot reintroduce a blank dot. 13 of its assertions fail against the old
  chip.
- `tests/test_accessory_detection.py` covers the profile-or-live rule,
  including that a closed relay outranks a profile flag and that unknown is
  not reported as absent.

## [0.6.68] - 2026-09-26

### Added

- **Export and import tariffs, utility services and presets as one file.**

  ```
  GET  /api/pricing/tariffs/export   → fhai-tariffs.json
  POST /api/pricing/tariffs/import   { "bundle": {...}, "replace": false }
  ```

  A tariff is typed in by hand from a bill and is easy to lose. When an AGL
  plan went missing there was nowhere to recover it from: a Home Assistant
  add-on uninstall can take `/data` with it, FHAI's own backups keep seven
  days, and the companion Bridges hold none of this — their `tariffs` and
  `utilities` tables exist but are empty. A file kept outside the add-on
  survives all three.

  **Account identifiers are stripped on export** — NMI, account number and
  meter serial identify a person and a property, and a file meant to be handed
  to another user must not carry them. The plan itself, its seasons, windows,
  rates and standing charges, is the retailer's published pricing and is the
  part worth exchanging.

  Rows merge by primary key, so re-importing your own export changes nothing
  rather than duplicating it. `replace: true` clears first. A bundle naming
  tables FHAI does not carry, or a version it does not understand, is refused
  outright — half-importing something unrecognised is worse than failing.

  Built-in presets are not exported: they are seeded on every install, and
  shipping them would overwrite the recipient's copies for no gain. An imported
  preset whose name already exists is kept, not overwritten.

---

Older releases: [full changelog](https://github.com/david2069/franklinwh-addon/blob/main/CHANGELOG-full.md)
