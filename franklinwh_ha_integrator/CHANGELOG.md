# Changelog

All notable changes to FranklinWH HA Integrator are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

---

## [0.6.74] - 2026-09-27

### Fixed

- **"No credentials stored" beside "✅ Credentials stored".** The Gateways tab
  refused to validate a gateway that was polling normally, while the Edit
  Gateway dialog next to it reported credentials present. Both were honest —
  they were reading different tables.

  `gateway_credentials` is the real store: encrypted, audited, and what the
  poller reads. `gateways.credentials_json` is a legacy column the registry has
  been migrating away from for as long as it has existed. The Validate button
  read only the legacy one.

  Resolution now lives in `src/services/credentials.py` — modern store first,
  legacy second and migrated on sight — and the registry, the validator and the
  profile refresh all call it. A test fails the build if a fourth reader starts
  parsing `credentials_json` itself.

- **`upsert_gateway` blanked what it was not given.** Three callers pass
  `credentials={}` with the comment "no longer stored in gateway row", and the
  statement wrote `"{}"` straight over the legacy column — so it emptied itself
  on every profile refresh and every gateway edit, which is how Validate came
  to read nothing. `site_id` had the same flaw: its two neighbours
  `site_name` and `site_address` were guarded against blanks and it was not, so
  renaming a gateway erased its site id.

- **Registration stored nine fields out of sixty.** The root of the last four
  releases. `add_gateway` extracted nine keys inline from the same
  `discover(tier=3)` snapshot that `build_profile_from_snapshot` turns into
  about sixty — so a freshly registered gateway had no `pcs_enabled`, no
  `tariff_configured`, no `solar_detail`, no `smart_circuit_count`, and every
  screen reading those reported a configured site as unconfigured until someone
  happened to refresh the profile.

  The setup wizard detecting a stale profile and refreshing it was treating the
  symptom. Registration now calls the same builder the refresh path does, and a
  gateway added from the UI is complete immediately.

### Internal

- `tests/test_registration_profile_is_complete.py` checks the Hardware step
  reads correctly straight off a registration profile, with no refresh. Five
  of the new assertions fail against the previous release.

## [0.6.73] - 2026-09-27

### Added

- **The wizard header says which build is answering.** Asked directly — "no
  idea what version is running" — after an add-on update that may or may not
  have landed. The step number now reads `… · step 2 of 5 · v0.6.73`.

### Fixed

- **The failing refresh says what actually went wrong.** 0.6.72 logged the
  exception at debug and printed "Could not reach the FranklinWH cloud", which
  names no cause and leaves nothing to act on. The exception type and message
  are now shown under the warning and logged at warning level. A setup step
  that silently gave up is why this took four releases to see.

- **Discovery can no longer hang the step.** It is roughly twenty cloud calls,
  and it ran unbounded inside a GET that the wizard re-fetches on every step —
  long enough to outrun the ingress proxy, which reads as the wizard being
  broken. Now bounded at 45s, and a timeout is reported as one.

- **A gateway that cannot be refreshed is not retried on every poll.** Twenty
  cloud calls per state fetch, indefinitely, against a gateway that is not
  going to answer, is a way to get an account rate-limited. One attempt per
  gateway per five minutes; the reason survives the cooldown rather than
  blanking on the next fetch, and clears when a refresh succeeds.

## [0.6.72] - 2026-09-27

### Fixed

- **The Hardware step fetches what it needs instead of asking the user to.**
  0.6.71 detected that a profile predated the discover flags and then printed a
  notice telling the user to go and refresh it on another tab. The integration
  already knows how to make that call. On a site with solar, two smart circuits
  and a configured AGL tariff, the step was reporting "TOU / Tariff: Not
  configured", "PCS Power Control: Disabled" and "Smart Circuits: Not
  installed" — every one contradicted by `franklinwh-cli discover` run against
  the same gateway a minute earlier.

  Opening the step now re-runs discovery for any gateway whose profile is
  missing those flags, and renders the result on the same request. The notice
  survives only for the case it was ever true of: the cloud could not be
  reached at all.

- **The feature flags were read off the hydrated row rather than the profile.**
  `_hydrate_gateway_fields` copies a dozen named keys onto the row and then
  pops `profile_json`, so `pcs_enabled`, `tariff_configured` and `sc_names`
  were gone before the facets were built — they would have read as absent even
  from a freshly written profile.

### Added

- **The full discovery snapshot, viewable and downloadable.**

  ```
  GET /api/gateways/{short_id}/discovery.json[?refresh=1][&download=1]
  ```

  The same object `franklinwh-cli discover --json` prints. FHAI extracts a few
  dozen fields into the profile and discarded the rest — the smart circuit
  schedules, the apbox DI/DO states, the supported modes with their reserve
  SoCs, the region quirks, and the per-accessory notes recording what the cloud
  does and does not expose. Seeing any of it meant running the CLI against the
  same account.

  Stored on every profile refresh, served to the authenticated session only and
  never cached: it carries the site address, the coordinates and every serial.
  The wizard links to both view and download, and says so before anyone shares
  the file.

### Internal

- The relay states stay under the flags rather than beside them. A live
  snapshot from a gateway with no generator module reports
  `electrical.relays.generator: true` alongside `generator.present: false` —
  confirming a relay reading cannot stand in for an inventory.

---

Older releases: [full changelog](https://github.com/david2069/franklinwh-addon/blob/main/CHANGELOG-full.md)
