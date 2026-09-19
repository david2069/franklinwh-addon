# Changelog

All notable changes to FranklinWH HA Integrator are documented here.

Format: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH`

---

## [Unreleased]

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

## [0.6.51] - 2026-09-19

### Fixed

- **The resolved model never reached the device block**, so 0.6.50 still showed
  `FranklinWH aGate 0091` with model `aGate`, and `Hardware: 1× APR-05K13V1-AU`
  with the gateway's own SKU missing.

  One cause for both: `profile["model"]` and `profile["sku"]` are routinely
  empty, while the resolved `aGate X-01-AU (AGT-R1V1-AU)` sits in
  `context["device_model_full"]`. The publisher was reading the empty keys and
  falling back to the bare family name. This is the same defect as the firmware
  field in 0.6.49 — three values, all resolved correctly elsewhere, none of
  them reaching the payload that publishes them.

  Discovery is now handed the combined string and splits it with the rule it
  already applies, so the device reads `FranklinWH aGate X-01-AU 0091`, model
  `aGate X-01-AU`, and `Hardware: AGT-R1V1-AU · 1× APR-05K13V1-AU`.

  A model already present on the profile still wins; this only fills the gap.

## [0.6.50] - 2026-09-19

### Fixed

- **The Home Assistant device info panel now reads the way FEM's does**, which
  was the requirement:

  ```
  aGate X-01-AU
  by FranklinWH Technologies Co., Ltd
  Firmware: V12R02B30D06_260304 (FHAI: v0.6.50)
  Hardware: AGT-R1V1-AU · 1× APR-05K13V1-AU
  Serial number: 10060006A02F24170091
  ```

  **Firmware** carries the gateway's cloud firmware followed by this
  integration's version. The shape was already right; the gateway firmware was
  missing, so only "FHAI: v0.6.46" was left in the field (fixed in 0.6.49).

  **Hardware** now carries the full aGate SKU plus the aPower fleet — how many
  and which. FEM shows `AGT-R1V1-AU (Hybrid) [default]`; the fleet is the part
  FEM never had, and is what makes the line worth reading on a site with more
  than one unit.

  A SKU is printed only when it was actually resolved. `SOURCE_FALLBACK` means
  nothing identified the unit and aPower X's figures were assumed, and such a
  unit is counted but not named — printing an assumed part number as though it
  were read off the hardware is precisely how "aGate 102" reached users.

## [0.6.49] - 2026-09-19

### Fixed

- **The MQTT device name now matches FEM, by reading FEM rather than deciding
  again.** `gateway_service` builds the model as `"{model} ({sku})"` —
  `aGate X-01-AU (AGT-R1V1-AU)` — and the publisher rendered it whole, so the
  SKU ended up bolted into the device name. FEM's own publisher splits that
  string and names the device from the model alone, and its test asserts
  `"FranklinWH aGate 1234"`. FWHAI now applies the same rule, and
  `tests/test_device_name_matches_fem.py` pins it to FEM's behaviour so it is
  not re-decided a fourth time.

  The SKU is not lost: it moves to `hw_version`, as it does in FEM, and the
  **Model entity is untouched** — it reads `device.model` from telemetry and
  still shows the full `aGate X-01-AU (AGT-R1V1-AU)`.

- **Home Assistant showed "Firmware: FHAI: v0.6.46" — this integration's own
  version — as the gateway's firmware.** The device block reads
  `profile["firmware"]`, the cloud profile's key, which is frequently absent;
  when it is, only FHAI's version was left in the field. The gateway's real
  firmware was never missing — `context["firmware_version"]` holds it, which is
  why the Firmware Version entity beside it read `V12R02B30D06_260304`
  correctly the whole time. Discovery is now passed the resolved value, on a
  copy rather than by mutating the shared context profile.

## [0.6.48] - 2026-09-19

### Fixed

- **Each writer of the notification settings now sends only the fields it
  owns.** 0.6.38 stopped a partial save erasing the rest, but a screen sending
  a *stale* value for a field it does not edit still reverts it, and the
  endpoint cannot tell that from a deliberate change. The Dynamic Pricing tab
  spread its whole loaded settings object into the body, so it wrote back every
  field it had read — a lost update against the Control Center, and a silent
  revert of any field added to the store later.

  The Control Center's global save also sent `ha_host` and `ha_token`, which
  the endpoint explicitly ignores, implying the form saved credentials when it
  never did. Both are removed.

  Correcting an earlier claim: **two files write these settings, not four.**
  `dynamic_pricing.html` and `smart_dispatch.js` only read. The count came from
  grepping for the URL without checking the method.

### Added

- **`docs/notifications_and_actionable.md`** — notifications and actionable
  notifications are separate features that fail differently, and the document
  is organised around that. One-way versus two-way; why the return leg is the
  hard half; add-on and standalone setup side by side; how to verify each half
  independently; and which of the two screens owns which setting.

- `tests/test_notification_settings_writers.py` fails the build when a writer
  spreads the settings object, sends a field the endpoint ignores, or sends one
  it does not persist. Its first version silently skipped an entire file —
  `dynamic_pricing_tab.js` builds `const body = {…}` and sends
  `JSON.stringify(body)`, which the extractor did not match — so it passed
  against the exact bug it was written for. It now handles both shapes, and a
  test asserts the extractor actually inspected every writer.

### Changed

- `[BKL-HA-CONSOLIDATE]` re-scoped after measuring it. The entry claimed the SD
  Notification Control Center "duplicates the Home Automation tab almost
  completely". That is wrong: SD issues no create, update or delete against
  `api/ha/instances` or `api/ha/devices` and already links back for edits,
  while Home Automation has no templates, cooldowns, audit log or diagnostics.
  The split is plumbing vs behaviour, and only the Connectivity sub-tab
  overlaps. Revised recommendation is to keep both pages and narrow the work to
  that mirror. The three defects the entry cited as cost are all fixed.

### Added

- `[BKL-UI-THEME-DENSITY]` in `docs/backlog.md` — a sequenced plan for the
  theming and density work, sized against the codebase rather than estimated:
  8,099 hardcoded Tailwind colour classes against 1,769 semantic `var(--…)`
  uses, 13 hues across 15 shade steps. That ratio is why a screen can be
  correct in dark or light but not both. Worst offender is
  `tabs/smart_dispatch.html` at 1,533. Includes the Control-vs-Dashboard
  comparison: Dashboard has zero hardcoded border classes, Control has 73.

  The plan starts with a ratchet test rather than a conversion, because
  without one the count refills as fast as it drains.


## [0.6.47] - 2026-09-19

### Fixed

- **"aGate 102" is repaired, not merely prevented.** Discovery used to paste
  `sysHdVersion` after "aGate" when the cloud sent no model name, showing an
  integer as a product name. 7f10d51 fixed that derivation — but
  `gateways.model` is only ever written at registration, and nothing
  re-resolves it. Every install that registered before that fix still showed
  `aGate 102`, so the fix was real and changed nothing on screen.

  The fabricated name carries the number that made it, so it is exactly
  recoverable: `aGate 102` → hardware version 102 → the catalog's `aGate X`
  (`AGT-R1V1-AU`). Repaired at startup.

  Deliberately narrow: only the exact `aGate <digits>` shape is touched, and
  only when the catalog knows that version. A real model name is never
  rewritten, and an unknown version is left alone rather than replaced with a
  guess.

- **Three ERROR lines on every add-on start, for probes that are expected to
  fail.** `bashio::log.error` writes to stdout, so the `2>/dev/null` on
  `bashio::supervisor.ping` and `bashio::services.available` suppressed
  nothing, and "Unable to access the API, forbidden" appeared at ERROR on
  every boot. Both calls are probes whose failure is already handled — the app
  asks the Supervisor directly a moment later and succeeds. Both streams are
  now redirected.

## [0.6.46] - 2026-09-19

### Fixed

- **One misconfigured entity id produced a warning every 30 seconds, forever.**
  A Home Load had `ha_binary_entity_id = "energipays"`. How that value was
  stored is not known — nothing in the code writes it, and the picker only
  emits on selecting a real entity. What is certain is that nothing rejected
  it: Home Assistant ids are `domain.object_id`, a bare word can never
  resolve, and the Smart Dispatch micro ticker re-read it on every tick:

  ```
  HA API error /states/energipays: Client error '404 Not Found'
  ```

  Nothing in that line said the value was malformed rather than merely absent,
  and nothing rejected it when it was saved. Validating an entity id is this
  code's job — accepting one that cannot resolve is the defect, wherever the
  value came from.

  An entity id with no domain is now recognised as unresolvable and never sent
  to Home Assistant, with a single warning naming the expected shape. A valid
  entity that is missing is reported once and then dropped to DEBUG until it
  returns — a configured entity that does not exist is one standing fault, not
  one fault per poll. Recovery clears the latch, so a later disappearance is
  still reported.

## [0.6.45] - 2026-09-19

### Added

- **`docs/standalone_deployment.md`** — running FHAI outside the add-on, and
  what differs. Covers pointing at a broker and a Home Assistant you already
  run, creating the long-lived token and why it behaves differently from the
  Supervisor's, the two legs of an actionable notification and how to test each
  separately, and troubleshooting for the cases that actually bite: a reverse
  proxy that drops the `Upgrade` header, self-signed certificates, Tailscale,
  and duplicate devices. Served at `/docs/standalone_deployment`.

- **`HA_HOST` and `HA_TOKEN` are passed through `docker-compose.yml`.** Both
  were read by the code and documented in `.env.example`, but compose never
  forwarded them, so a scripted standalone install could not configure Home
  Assistant without opening the UI. Values entered in the UI are stored and
  still take precedence.

- `tests/test_standalone_docs_are_true.py` fails the build when the guide
  drifts from the code: every log string it tells you to grep for must still be
  logged, every environment variable it documents must still reach the
  container, and the callback path it names must still be a route. Documentation
  goes stale silently, which is exactly how the changelog got 125 commits
  behind.

### Verified

- The WebSocket return leg now confirmed end-to-end on a standalone Docker
  install, not just unit-tested: `watching Primary Home Assistant
  (ws://192.168.0.109:8123/api/websocket)` followed by `subscribed to
  mobile_app_notification_action`, with no Supervisor and a user-created
  long-lived token.

## [0.6.44] - 2026-09-19

### Fixed

- **A self-hosted Home Assistant served under a `/core` path was sent to the
  wrong socket.** The Supervisor proxy was detected by path suffix, so a
  standalone install at `https://example.com/core` would have been asked for
  `/core/websocket` — where Home Assistant does not listen. The Supervisor
  base is now matched exactly.

- **Connection failures off-Supervisor now name their likely cause.** Under
  the Supervisor the socket is plain HTTP on the Docker network and little can
  go wrong. A self-hosted Home Assistant is reached through whatever the user
  has in front of it, and the raw errors are close to useless: "Invalid
  response status" is what a reverse proxy that drops the `Upgrade` header
  looks like, and a certificate error is what a self-signed Home Assistant
  looks like. Both are now explained, along with a rejected token.

  Note the WebSocket suits a standalone install better than the webhook it
  replaces. The webhook required Home Assistant to reach *in* to FHAI — the
  hard direction behind NAT, firewalls and container networks. The WebSocket
  is FHAI reaching *out*, the same direction as every REST call it already
  makes, so an install that can send a notification can receive the tap.

## [0.6.43] - 2026-09-18

### Fixed

- **A 51-entity duplicate of the live gateway is now swept automatically.** A
  0.6.42 boot reported `left 'aGate 24170091'
  (franklinwh_10060006a02f24170091) in place — 51 entity config(s)`. That is
  the same hardware as the live gateway under a lowercase serial:
  `gateway_service` upper-cases the serial before publishing, so a lowercase
  identifier on the broker was written by an older build. Home Assistant keys
  devices on the identifier string, so the two are separate devices with the
  same display name, and the old one rebuilt all 51 entities from its retained
  configs on every restart — which is why entity pickers showed two of
  everything.

  A case variant of a serial that is registered here is provably our own
  retired device, so it is now swept with the `franklinwh_{short_id}` form
  rather than merely reported. Only the serial varies: the prefix has always
  been written lowercase, and upper-casing the whole identifier would invent a
  shape that was never published. The live identity is excluded whatever case
  it is in, and a serial belonging to no registered gateway is still left
  alone.

## [0.6.42] - 2026-09-18

### Added

- **A subscription per Home Assistant, not one for the add-on.** 0.6.41 held a
  single socket on the add-on's own connection. A device route is foreign-keyed
  to an `ha_instances` row, and the tap event is raised by the Home Assistant
  the companion app is registered with — so a phone attached to a second
  instance raises its event there and nowhere else, and one socket would never
  have seen it. Outbound does not care which instance a device belongs to;
  inbound does.

  FHAI now holds a subscription on every instance that has at least one enabled
  device, plus its own connection. Instances with no devices are not watched,
  and two rows describing the same Home Assistant collapse to one socket — the
  seeded local instance and the default connection are the same server.

  The target set is re-evaluated every 60s, so an instance added in the UI
  starts being listened to without restarting the add-on, and one that loses
  its last device is dropped. Each connection resolves its own token at connect
  time rather than capturing it once, because the Supervisor re-mints its token
  on every container start.

## [0.6.41] - 2026-09-18

### Fixed

- **"Your Integrator IP and Callback URL mismatch" was a false alarm on every
  add-on install.** It compared the callback URL's hostname to
  `window.location.hostname`. Under ingress those are different by
  construction: the browser reaches the panel through Home Assistant's own
  address, while the callback URL is the add-on's hostname on the Docker
  network — which is exactly what Home Assistant must use to reach it. The
  warning could therefore never be absent, and it spent the whole session
  reporting a fault that did not exist. It is skipped under the Supervisor,
  where the setup audit now explains that taps arrive over the WebSocket and
  the callback URL is not used at all.

### Added

- **Actionable taps now arrive over the Home Assistant WebSocket.** The return
  leg was a webhook: a `rest_command` and an automation the user pastes into
  `configuration.yaml`, pointing at a URL that has to stay reachable from Home
  Assistant. Every part of that is theirs to maintain, and every failure of it
  is silent here — a tap against a wrong URL produces nothing at all, so "no
  response recorded" and "nobody tapped" are the same observation. That was the
  exact state of this integration: pushes delivering 5/5 at 200, and not one
  response ever recorded.

  FHAI now subscribes to `mobile_app_notification_action` directly. No YAML, no
  URL, no port, nothing to keep in sync. It works in both deployments, which
  the webhook never really did: as an add-on over the Supervisor proxy at
  `ws://supervisor/core/websocket` with the Supervisor token — plain HTTP
  inside the Docker network, so no TLS and no reachable-from-HA question — and
  standalone against the user's own Home Assistant with their long-lived token.

  The webhook path is left in place; anyone whose YAML works keeps working.
  Both routes end in the same handler, and whichever arrives first clears the
  pending record while the other finds nothing to do. Disable with the
  `ha_event_listener_enabled` config value.

  Note the two deployments do not share a path: Home Assistant serves the
  socket at `/api/websocket`, but the Supervisor proxy exposes it at
  `/core/websocket` — `/core/api/` is the REST proxy and answers a socket
  request with a 404 rather than an upgrade.

## [0.6.40] - 2026-09-18

### Fixed

- **The Home Automation tab advertised a callback URL that does not exist.**
  It displayed `/api/automation/notifications/callback` as the "HA Callback
  Target"; the only route is `/override`. Anyone who copied it into their
  `rest_command` had every tapped response 404 in silence — the push arrived,
  the tap worked, and the audit ledger stayed empty while the console said
  "Test timed out waiting for action." The displayed URL is corrected, and the
  retired path now answers as well, logging a WARNING that names the URL to
  change to, so an existing wrong config starts working *and* becomes visible.

### Added

- **Test Callback Path** (Notification Control Center, beside Fire Test
  Notification). The outbound leg is self-evident — the ledger shows 5/5 at
  200. The return leg had no signal at all: a tap against an unreachable
  `rest_command` URL produces nothing here, so "no response recorded" and
  "nobody tapped" look identical.

  This asks Home Assistant to run `rest_command.franklinwh_action_callback`
  itself with a sentinel request id, and reports which half is broken: HA
  unreachable, the rest_command not defined, HA ran it but nothing arrived
  (the URL is wrong), or the path works — in which case the remaining fault is
  the automation trigger or the phone. No phone tap required.

### Added

- `tests/test_standalone_deployment.py` pins the behaviour of both deployments.
  FHAI runs as a Home Assistant add-on, where the Supervisor supplies a token
  re-minted on every container start, and standalone, where there is no
  Supervisor and the user enters a host and a long-lived token by hand. The
  0.6.35 token fix resolves the local instance's token from the environment at
  the moment of use; these tests hold it inert everywhere else — a typed-in
  token is never replaced, seeding and connection-populate both decline
  without a Supervisor, a hand-entered `ha_host` is never overwritten, and a
  standalone install dispatches to its own Home Assistant with its own token.
  No behaviour change; this records guarantees that were previously untested.


## [0.6.39] - 2026-09-18

### Added

- **Notification settings changes are now audited.** Turning Global
  Notifications on or off left no trace anywhere — the switch wrote the
  settings row and returned `ok`. It is precisely the change someone needs to
  find later, when nothing alerted them overnight. Every change to `enabled`,
  `ha_target`, `actionable`, `actionable_ttl` or `triggers` is now written to
  the notification Audit Log as a `CONFIG` entry recording the before and
  after, and to the admin audit log. A save that changes nothing writes
  nothing.

  `CONFIG` entries render in their own colour: the Audit Log styled any
  unrecognised direction as a red error, so a routine settings change would
  have looked like a failure.

## [0.6.38] - 2026-09-18

### Fixed

- **The Global Notifications switch never saved.** It flipped a local flag and
  stopped there, so it read "System Live" in green while the server still had
  notifications off — and the test button answered "Global Notifications are
  toggled OFF" against that green toggle. It now persists on click and puts
  itself back if the save fails.
- **Saving any notification setting erased the others.** The settings row is
  replaced wholesale, and the PUT payload defaulted every missing field to
  ""/False/[]/1800 — so a partial save from any of the four screens that use
  this endpoint reset `ha_target`, `triggers`, `actionable` and
  `actionable_ttl`. Omitted fields are now carried forward; an explicitly sent
  empty value is still honoured.
- **`/ha/repair` would tombstone any entity it was handed.** Tombstoning
  publishes into the discovery prefix shared with every other MQTT integration,
  and the endpoint applied no ownership check at all. It now refuses anything
  whose unique_id is not a FranklinWH entity, and reports it as refused.


- **The gateway table said "Charging" while the battery was discharging.** The
  dashboard and gateways tables each carried their own copy of the direction
  test, and both had it backwards: the server signs `battery_kw` negative for
  charging, and both templates read `> 0.05` as charging. With solar at 0.00,
  grid at 0.01 and the house drawing 0.23 kW from the battery, the table
  reported Charging while the top bar reported Discharging.

  Both copies also read `run_status_dec` — a field nothing produces; the real
  one is `run_status_desc` — so the server's own description never applied and
  the inverted guess always won. Both now call
  `$store.app.getBatteryStateText()`, which already implemented the convention
  correctly and is what the top bar was using all along.

## [0.6.37] - 2026-09-18

### Fixed

- **The add-on log said nothing about a push test having run.** Every path in
  the sender logs, but the ones that matter logged at INFO, and a send that
  reaches nobody was filed as routine information. The test endpoint now logs
  its outcome at WARNING with the per-device rows inline — including an
  explicit "NO PER-DEVICE RESULTS — nothing was dispatched to any device" —
  so the answer is in the log being read rather than only in the dispatch
  console on screen.

### Fixed

- **The web UI came up half-drawn on first open.** The root `x-init` — on the
  element wrapping the entire app shell — called `applyDarkClass()` and
  `applyAccentVars()`, neither of which exists anywhere in the codebase. Alpine
  threw `Can't find variable: applyDarkClass` on every page load, so everything
  after that point in the init never ran. Both were duplicates of work the app
  store already does in its own `init()`: `applyTheme()`, `applyIconTheme()`,
  the `data-accent` attribute and the `prefers-color-scheme` listener. The
  calls are removed; the store was already the one source of truth.

  The same two phantoms were wired to the theme and accent buttons in the top
  bar, so every theme change threw as well.

- `tests/test_no_phantom_template_helpers.py` now fails the build when an
  Alpine expression calls a function that is defined in no JavaScript file, no
  inline `<script>`, and no `x-data` literal.

## [0.6.36] - 2026-09-18

### Fixed

- **The push test reported a device count it had invented.** The 0.6.35 toast
  fell back to `ok || 1`, so a dispatch that returned *zero* per-device results
  still announced "sent to 1 device(s)" — the same false success it was meant
  to fix, one layer down. It now distinguishes zero results from a real send.
- **"Send failed" in the Control Center now says why.** The endpoint returns
  its reason in `detail`; the handler replaced it with the constant string
  `Send failed` and erased that four seconds later, which is why the error
  could not be read or scrolled back to. The server's reason and any
  per-device errors are shown, and failures are no longer auto-cleared.
- When no per-device results come back, the dispatch console now prints the
  server's raw reply instead of asserting what must have happened.

### Added

- **Remove old / duplicate devices** (MQTT Admin → Old / Duplicate Devices).
  Home Assistant keys a device on `device.identifiers`, not on its name. Ours
  changed from `franklinwh_{short_id}` to `franklinwh_{full_serial}`, which
  declares a *new* device rather than renaming the old one. Nothing published
  to the old identifier again, and because discovery messages are retained,
  Home Assistant rebuilt the old device — with every entity it had ever been
  given — on every restart. That is the second "aGate X" device appearing
  beside the live one in entity pickers. The scan lists each such device with
  its entities; removing one clears the retained discovery configs that
  recreate it.

  The existing retained-message cleanup could not do this: it subscribes to
  `{topic_prefix}/#` and refuses anything outside it, while discovery configs
  live under `homeassistant/`. It also had no UI of any kind — the endpoints
  existed but nothing called them.

  Removal is guarded twice: a topic must be structurally ours (its object_id
  starts with `franklinwh`), and it must still be classified as a ghost by a
  fresh scan at the moment of removal. A registered gateway's own device can
  never be removed, and another integration's devices are never touched.

## [0.6.35] - 2026-09-18

### Fixed

- **Notifications were never reaching any device, and said they had.** The
  Supervisor mints a new `SUPERVISOR_TOKEN` on every container start. The local
  Home Assistant instance was seeded once with a copy of that token and nothing
  ever refreshed it, so from the first add-on restart onward notify discovery
  returned `401: Unauthorized` and every companion device hanging off that
  instance was dropped before dispatch. The token is now resolved from the
  environment at the moment of use; stored tokens are still honoured for remote
  instances the user configured by hand.
- **Devices were skipped silently.** Two bare `continue`s discarded any device
  whose parent instance was missing or whose config was incomplete — no log
  line, no entry in the returned results. Five devices showing as `Enabled` in
  the UI could vanish from a dispatch with no trace. Skips are now logged and
  returned with the reason.
- **The Unified Push test claimed success it had not verified.** The toast fired
  on HTTP 200 alone, so "dispatched successfully" appeared when nothing had been
  delivered and only the legacy fallback had been tried. It now counts actual
  per-device outcomes. The dispatch console's "No device targets registered"
  line no longer asserts that fallback routing completed.

### Added

- **Discover button in the device mapping dialog.** Discovery ran on open and a
  failure was terminal — the only way to retry was to close and reopen the
  dialog. It can now be re-run in place.

## [0.6.34] - 2026-09-18

### Fixed
- **"DISCONNECTED / Host Address: Not Set" now resolves itself everywhere.**
  Several panels read the *stored* `ha_host`/`ha_token` rather than the
  effective connection, so an add-on with a working Home Assistant connection
  reported itself disconnected — in the Smart Dispatch Notification Control
  Center, the notifications panel, and the Home Automation tab. The same false
  negative, reported three times as three separate bugs.

  The stored values are now populated from the Supervisor connection at startup,
  which fixes every panel at once — including any not yet found. Two details
  make it safe:

  - **rewritten every boot, not seeded once** — `SUPERVISOR_TOKEN` is issued per
    container start, so a value stored once is stale after the next restart,
    which is worse than empty because it looks configured;
  - **only values we own are touched** — a host set by hand, pointing at a
    remote Home Assistant, is left alone rather than silently replaced on every
    boot.

  This is a stop-gap and is labelled as one. `[BKL-HA-CONSOLIDATE]` is raised to
  **urgent**: Smart Dispatch carries an entire second Notification Control
  Center — its own engine version, tabs, templates, audit log and test console —
  making five surfaces over four stores. That duplication has now produced three
  real defects in one day, not just untidiness.

## [0.6.33] - 2026-09-18

### Fixed
- **Auto-added companion devices delivered nothing.** All five showed
  *Enabled*, and every test push went nowhere. Three faults, all in the
  auto-configuration added in 0.6.15:

  - **The notify target was stored with a `notify.` prefix.** The sender builds
    `/api/services/notify/{target}`, so it needs the **bare** service name —
    the stored value produced `/api/services/notify/notify.mobile_app_x`, a path
    Home Assistant does not route. The table rendering it as
    `notify.notify.mobile_app_x` is what gave it away.
  - **Discovery was a second copy of an existing function.**
    `notification_sender.discover_notify_targets()` already did this and already
    returned bare service names. The duplicate is gone; the original is reused.
  - **Notify discovery returned `401: Unauthorized`.** `_get_ha_credentials()`
    resolved its own way and got it half right — it picked up `SUPERVISOR_TOKEN`
    but never the Supervisor *host*, so the add-on held a valid token and an
    empty host. It now resolves through `api_ha._get_ha_client()`, the same
    resolver everything else uses.

  **Rows already written wrong are repaired at startup**, stripping the
  duplicated prefix — a fix that only corrected new rows would have left all
  five broken.

## [0.6.32] - 2026-09-18

### Added
- **Slugs are definable, like the prefix.** The entity prefix has been
  templated, previewed and migratable for a long time; the slug stayed hardcoded
  in `models/entities.py`. So an install whose dashboards were built against
  another FranklinWH integration could match that integration's prefix and still
  miss on a measurement it named differently — with nothing here able to fix it.

  `GET`/`PUT /api/mqtt/slug-aliases` sets overrides, applied **before** the
  prefix because the slug is part of the `unique_id`. `battery_soc` →
  `state_of_charge` is offered as a known preset.

  With the `franklinwh_` prefix and that one alias, all five entities a
  power-flow card uses now match exactly:
  `battery_power`, `solar_power`, `grid_power`, `home_loads`,
  `state_of_charge` — no card edits.

  Changing a slug **is a migration**: Home Assistant sees a new entity and
  orphans the old one, exactly as a prefix change does. The response says so.
  Aliases are validated as entity-id-safe, and a slug mapped to itself is
  dropped rather than stored as clutter.

## [0.6.31] - 2026-09-18

### Added
- **The Home Assistant device name is now a setting, not a decision in the
  code.** MQTT Admin → *Home Assistant device name*, with presets and a live
  preview. It changed three times in one release cycle while it was hardcoded,
  and every change churns the device in Home Assistant for a reason nobody
  chose. The default still matches FEM and the earlier integrations —
  `FranklinWH aGate X-01-AU 0091`.

  Safe to change: renaming a device does not move its entities or their history,
  because entity ids follow the `unique_id`, which this does not touch. A
  template with no token is refused — it would render the same name for every
  gateway on a multi-gateway install.
- **`franklinwh_` added as an entity-prefix preset**, labelled *matches older
  dashboards*. This is the one that repairs a dashboard built against an earlier
  FranklinWH integration: with it, four of the five entities a power-flow card
  uses match exactly, because the slugs never changed —
  `franklinwh_battery_power`, `franklinwh_solar_power`, `franklinwh_grid_power`,
  `franklinwh_home_loads`. The fifth, `state_of_charge`, is published here as
  `battery_soc` and needs a one-line card edit.

  Delete any orphaned registry rows holding those ids first, or the new ones
  arrive as `_2`.

## [0.6.30] - 2026-09-18

### Fixed
- **Dropped the brackets from the MQTT device name.** 0.6.25 produced
  `FHP (FranklinWH aGate 0091)` — neither the requested format nor the one the
  sibling devices already use, and it looked wrong beside them. The device name
  is now brand, model and serial and nothing else:
  **`FranklinWH aGate X-01-AU 0091`**. The name you gave the gateway belongs on
  the entities, not bolted onto the device identity.
- **The device reported the model family instead of the model.** It read
  `FranklinWH aGate 0091` beside a sibling called
  `FranklinWH aGate X-01-AU 0091` — the same hardware, looking like two
  different products. The cloud's `model_name` is often just "aGate" while
  `model` carries the specific one, and since 0.6.23 the catalog resolves the
  hardware version into that field too. The specific model now wins.

## [0.6.29] - 2026-09-18

### Fixed
- **`grid_charge_enabled` and `grid_discharge_enabled` were advertised and
  implemented nowhere.** `/gateways/{id}/control/commands` offered them and the
  module docstring documented them, but `dispatch_command()` has no branch for
  either — so calling one returned *"Unknown or untranslated command slug"*.
  The endpoint accepted a command the dispatcher then called invalid.

  Marked `"unimplemented": True` rather than deleted: the capability is real in
  the cloud API, and removing the advertisement would hide the gap instead of
  recording it.
- **Generator manual start/stop was undiscoverable.** It works — `manuSw` is the
  correct field for start/stop, whatever the method is named — and it was absent
  from the control catalogue, so no caller could find it. Now advertised, with
  the two caveats attached: a manual start is only permitted off-grid, and the
  value mapping is unverified upstream.

### Added
- **`tests/test_control_catalogue.py`** holds the advertised catalogue and the
  dispatcher together in both directions: advertised-but-unimplemented is a
  command the caller is told is invalid, and implemented-but-unadvertised is a
  capability nobody can find. Generator mode and the SoC thresholds stay
  unadvertised by design, with the reason recorded — the cloud API has no setter,
  so advertising them would invite an attempt that can only refuse.

### Documentation
- Step 1 of `docs/capability_gating_plan.md` is recorded as **not needed**: the
  three dead generator controls are not exposed anywhere, so there was nothing
  to hide. The audit that established it found the catalogue drift instead.

### Documentation
- **`docs/capability_gating_plan.md`** — a plan for turning off what cannot
  work, resting on a distinction that decides the whole shape of it:

  | Reason | Treatment |
  |---|---|
  | **Cannot work** — hardware absent, or no setter in the cloud API | hide |
  | **Harmful here** — works, and breaks something | disable, and say why |
  | **Not configured yet** | show, with a path to configure |

  The third is the one that goes wrong when decluttering gets enthusiastic:
  hiding an unconfigured feature makes it undiscoverable, which is worse than
  clutter. Unconfigured tariffs, notifications and weather providers stay
  visible deliberately.

  It extends `_compute_persona_gates()`, which already gates server-side,
  defaults every gate to `True` and bypasses until persona detection has run —
  rather than adding a second mechanism. Its docstring has invited exactly this
  since it was written; two tabs are gated today.

  Ordered by value against risk: hide the three generator controls the cloud API
  cannot drive; gate on hardware flags, where **`None` means unknown and must
  hide nothing** — the only rule that can break a working install; label the
  Supervisor-supplied fields rather than hiding them.

## [0.6.28] - 2026-09-18

### Fixed
- **Native TLS could make the add-on unreachable, and the switch was right
  there.** Home Assistant's ingress proxy speaks **plain HTTP** to
  `ingress_port`; binding a TLS socket there breaks the panel — and the setting
  that broke it sits behind the panel it broke, with no way back through the UI.

  The control is now disabled as an add-on and says why, rather than vanishing:
  a control someone was told exists, silently absent, sends them hunting.

  The first version of this fix **failed open** — the template branched on a
  variable nobody passed, which Jinja treats as falsy, so the toggle rendered
  under the add-on exactly as before. Caught by its own test.

### Documentation
- `[BKL-ADDON-SURFACE]` — what else a deployment cannot use. The surface is
  already small: `config.yaml` publishes no port and exposes no TLS option.
- `[BKL-METRICS-DUPLICATION]` — **`gateway_metrics` is not a straight duplicate
  of Home Assistant's recorder, and switching it off would break tariff
  costing.** `bill_period.py` derives two quantities HA's statistics cannot
  answer: the **demand peak** (highest average over the demand interval, import
  only — HA keeps hourly mean/min/max, a different window and a different
  statistic) and **energy integrated across arbitrary tariff windows**, which
  hourly buckets do not align to.

  The overlap is real but partial. What should happen instead: surface that the
  store exists and that retention is 14 days by default; make the real question
  "do you use the costing engine" rather than "record metrics"; and consider
  reading HA's statistics directly, which is the honest de-duplication and a
  design change rather than a switch.

## [0.6.27] - 2026-09-18

### Documentation
- **Corrected `docs/setup_and_access.md`: the deployment decides how much the
  account advice matters, and the previous version did not say so.**

  As an **add-on there is no inbound port at all** — `ingress: true`, no `ports:`
  key — so every request arrives through Home Assistant's proxy already
  authenticated as a Home Assistant user. Accounts here are a second layer
  inside an already-authenticated session, not the perimeter.

  In **Docker or standalone, port 8099 is the front door**, and the accounts
  here are the only thing behind it. That is where user profiles do real work
  and where TOTP earns its place — and where Tailscale is doing something load
  bearing rather than convenient.

- **Also corrected: what a role actually restricts.** It restricts what you can
  *run*, not what you can *see*. Roles are enforced per route tier, so a
  `viewer`'s writes are refused — but only `mqtt_admin`, `terminal` and
  `security` are withheld from the markup. **The other 21 tab panels render in
  full, data included.**

  For a household that is usually the right trade; for a shared display it is
  not, which is what the `guest` and `inkypi2` dashboards are for.
  `[BKL-RBAC-VISIBILITY]` tracks extending it, with the note that gating must
  stay server-side — markup the browser never receives cannot be revealed in
  devtools.

  Both claims are pinned: the build fails if a `ports:` key appears, or if the
  set of admin-gated tabs changes without the document following.

## [0.6.26] - 2026-09-18

### Documentation
- **`docs/setup_and_access.md`** — what setup should ask, and how to give other
  people access. Two unrelated questions, answered separately.

  Setup should ask two kinds of question only: what nothing else can supply
  (under the Supervisor, just the FranklinWH credentials), and what is cheap now
  and expensive later. There is exactly one of the latter and it is not obvious:
  **the entity-id prefix**. Changing it afterwards recreates every entity and
  orphans the old ones, breaking every dashboard at once — so the entity survey
  belongs *before* registration, the only moment when "adopt the old scheme" and
  "start clean" are both open. Everything else — tariff, solar, dispatch,
  notifications — is better once telemetry is on screen and the user is
  confirming rather than guessing.

  On access: **`config.yaml` does not set `panel_admin`, and Home Assistant
  defaults it to true — so only Home Assistant administrators can see this
  add-on's panel at all.** A family member cannot reach it whatever accounts
  exist here. The recommendation is not to give them this UI: 85 entities are
  published into Home Assistant, so a Home Assistant dashboard gated by Home
  Assistant's own accounts is the better surface — no second identity system, it
  works in the companion app, and it survives this add-on being replaced.
  Accounts here are for people who need this application's own interface, plus
  the dedicated-display case the `guest` and `inkypi2` dashboards exist for.

  Tailscale changes who can *reach* Home Assistant, not who may *use* it — both
  Home Assistant and this add-on still authenticate every request.

  The claims are pinned by `tests/test_setup_and_access_doc.py`: the roles named
  are the roles enforced, the dashboards named exist in the schema, and the
  `panel_admin` claim fails the build if anyone sets it — because that changes
  who can reach the add-on, and the advice would then be wrong.

## [0.6.25] - 2026-09-18

### Changed
- **MQTT devices are named `FranklinWH aGate X-01-AU 0091` again**, as FEM and
  the earlier integrations did. A gateway you named still leads —
  `FHP (FranklinWH aGate X-01-AU 0091)` — but the brand, model and serial
  suffix follow it. An install can carry three FranklinWH devices at once, and
  "FHP" beside "FranklinWH aGate X-01-AU 0091" gave no clue both described the
  same hardware.

### Documentation
- **`docs/entity_migration.md`** — what is actually possible when a previous
  FranklinWH integration has left entities behind, and three strategies for it:
  match the old entity-id prefix, retire and move forward, or adopt the ids.

  Findings worth the read:
  - **Disabling another integration does not release its entity ids.** A
    disabled config entry still holds them; only deleting it frees the names. So
    the "Disable it for me" action added in 0.6.14 ends the polling contention
    and does nothing for adoption.
  - **`storm_decision_strategy`, `dispatch_action` and `dispatch_detail` are
    this integration's own vocabulary.** A device full of unavailable entities
    with those names is a *previous install of this add-on*, not a competitor —
    which is the favourable case, because its ids are orphaned and therefore
    free.
  - A real install carried **four** naming schemes plus the current one, and its
    power-flow card pointed at the oldest, which is why every leg read 0 W while
    the hardware was fine.
  - Setting the prefix to `franklinwh_` reproduces four of that card's five
    entities exactly; `state_of_charge` vs our `battery_soc` is the one that
    needs a card edit. Delete orphaned rows holding those ids first, or ours
    arrive as `_2`.
  - Statistics import is deliberately separate: it writes into the recorder
    directly, sum-type statistics carry a running total, and units must match.

### Fixed
- **The defect register matched filenames as methods.** `` `discover.py:551` ``
  counted as a defect in `discover()`, in both the test and the generator —
  which then disagreed with each other. One shared matcher now requires an
  actual call form, taking the register from 30 entries to 19 real ones.

## [0.6.24] - 2026-09-18

### Added
- **The cloud library is now checked, not assumed.** Instructions had not
  stopped an agent calling `Client.raw()` (never existed), ignoring
  `stats.is_stale` (which the library sets and requires consumers to check),
  leaving `get_span_setting()` unused while asserting SPAN could not be
  discovered, or reading `spanFlag` as a detection when it records a
  configuration. Every one was found by accident.

  `tests/test_cloud_api_surface.py` fails on:
  - a method called on a cloud client that does not exist;
  - a keyword argument the signature rejects;
  - an upstream defect affecting a method we call that is not registered;
  - an installed library version that does not match the `requirements.txt` pin.

  `docs/cloud_library_defects.md` registers the **30 documented defects**
  affecting methods this integration calls — none of which was mentioned
  anywhere here before. Regenerate with `scripts/sync_cloud_defects.py`.

### Fixed
- **`apply_tariff_template(tariff_id=…)` — the parameter is `template_id`.**
  Every call raised `TypeError` into a broad `except`, which logged it as
  "apply_tariff_template failed" and returned a cloud error for a local mistake.
  Applying a tariff template has never worked.
- **Generator start/stop SoC could not be set, and said so as a cloud failure.**
  `set_generator_schedule()` does not exist on the client and never has; the
  library offers no setter for those thresholds at all. Both controls now refuse
  and point at the FranklinWH app. Same shape as `Client.raw()`.
- **The development virtualenv held franklinwh-cloud 0.4.7 while
  `requirements.txt` pinned 0.4.9.** Three methods read as non-existent that
  ship perfectly well, and every `hasattr` check made while reasoning about the
  library was answering about code nobody runs. Now enforced by a test.

## [0.6.23] - 2026-09-18

### Fixed
- **"aGate 102" is not a product FranklinWH sells.** When the cloud's device
  list carried no model, discovery pasted the hardware version after the brand
  — `f"aGate {sysHdVersion}"` — and presented the result as a product name.
  `sysHdVersion` is an integer, and the device catalog has mapped it all along:
  **102 is `aGate X-01-AU`, the Australian aGate X**. The dashboard was showing
  a made-up name for a unit the catalog could identify exactly.

  Models now resolve through `db.get_device_model()`. A hardware version the
  catalog does not know is reported as one — "aGate (hw 107)" — rather than
  dressed up as a model: "we do not have this one yet" is true, a fabricated
  model is not.
- **The same fabrication had a second home in the browser.** Fixing the backend
  left `fmt.model()` in `app.js` mapping hardware versions to product families
  by hand: `101 → aPower`, `102 → aGate`, `103 → aHub`. The app's own seeded
  catalog contradicts that — **100-104 are every one of them `device_class`
  "agate", name "aGate X"**, revisions of a single product told apart by SKU
  (`AGT-R1V1-US`, `AGT-R1V2-US`, `AGT-R1V1-AU`, `AGT-R1V3-US`). A **US gateway
  on 101 rendered as "aPower"**, and no version maps to an aHub at all.
  `sysHdVersion` comes from `getHomeGatewayList` and describes the gateway, so
  an aPower cannot be reporting one.

  The table is gone; the UI displays what `_resolve_models()` resolved, which
  honours admin overrides a hardcoded map would have silently outranked. A bare
  integer reaching the formatter now reads "aGate (hw N)" — the backend's own
  wording for a version the catalog cannot name — instead of being returned
  unlabelled, which is how "102" reached the dashboard as a product name.

  Guarded by tests that assert against the **seed** rather than a literal list,
  so they follow the data if a real aPower or aHub version is ever added, and
  verified by re-introducing the mapping to confirm they fail.
- **`run.sh` no longer announces failures the app fixes two seconds later.** Its
  bashio calls fail on this install, so every boot opened with "Timezone
  unavailable — schedules will be offset" and "install the Mosquitto broker
  add-on" — both untrue by the time startup finished, and both the first thing
  read when something else goes wrong. The warnings now fire from the
  application, where the outcome is known, and still fire when the app itself
  cannot resolve them.

### Changed
- **Open-Meteo is the default weather provider.** It needs no API key, so a
  fresh install has working weather without signing up for anything. The
  previous default reported the gateway's own cloud weather record, which on a
  real install rendered 0.0 °C / "Unknown" / "Last updated: Never" — a panel
  that looks broken rather than unconfigured. Existing installs keep their
  setting; this is the default for a config never written.
- **One coordinate now reaches every store that holds it.** Latitude lived in
  four places — per-gateway config, the solar forecast config, the weather
  config, and Home Assistant. Open-Meteo read through to the solar config while
  the weather config was blank, which worked until someone typed a latitude on
  the Weather tab: after that the two diverged permanently and silently, and the
  weather panel described one place while the solar forecast described another.

  `site_location.propagate()` writes them together. A value already set is never
  overwritten — filling a blank is help, changing an answer is not — and Home
  Assistant's own location is never written at all.

### Documentation
- `docs/geolocation.md` — every store of the site's coordinates, the decimal-
  degrees-signed format, the sign rule (never inferred from a coordinate), the
  precedence order, and why a forecast and a weather panel describing different
  places is worse than either being absent.

## [0.6.22] - 2026-09-18

### Fixed
- **0.6.21 read the SPAN flag as detection. It is not.** `spanFlag` records that
  an installer *configured* the SPAN integration in the FranklinWH app; `0` does
  **not** mean no panel is present (`DEF-SPAN-FLAG-IS-CONFIG-NOT-DETECTION`).
  Read as presence — which 0.6.21 did, storing it as `has_span` — it tells an
  owner with an installed-but-unconfigured panel that they have none. Now
  recorded and reported as `span_configured`.
- **The Generator Mode control wrote a field that is not the mode.**
  `set_generator_mode()` posts `manuSw`, a manual start/stop acting on generator
  *state*; the mode lives in a separate `mode` field and the library exposes no
  setter for it (`DEF-GEN-MODE-WRITES-MANUSW`, from 35 correlated writes). The
  control could therefore never work — and would instead send an unrecognised
  manual command to a machine that burns fuel, because the `manuSw` value
  mapping is explicitly assumed upstream from a single sample.

  It now refuses, and says to change the mode in the FranklinWH app.
- **Generator start/stop keeps working but records the uncertainty.** `manuSw`
  *is* the right field for start/stop — the method is misnamed, not wrong, for
  that use. But the one clean sample shows `manuSw: 2` preceding
  Running → Cooldown, which reads as a stop, while this passes `2` for ON. Each
  call is logged so a surprise is traceable.
- **The `start_generator` automation action no longer reports a mode change it
  did not make.** Its "1=auto-schedule, 2=manual" wording came from the
  library's old docstring. The action still fires — it runs only from a rule
  somebody wrote deliberately, and silently breaking that would be worse — but
  now names the field it actually writes.

### Documentation
- `[BKL-CLOUD-NEW-CAPS]` — smart circuit schedules, the generator charge
  schedule (gateway-local wall clock, unconverted), `get_generator_detail()`,
  the `genStartElec`/`genCloseElec` naming, aHub circuits that cmdType 311
  cannot represent, and the WiFi/SPAN hazard.
- `[BKL-CLOUD-SURFACE-AUDIT]` — three faults this session came from assuming
  what the library does instead of reading it, each found by accident. A
  scripted diff of its public surface against our call sites would find the rest
  at once.

## [0.6.21] - 2026-09-18

### Added
- **SPAN panel detection.** The cloud library has exposed `get_span_setting()`
  — returning `{"spanFlag": 0|1}` — all along, and this integration never called
  it, so an install with a SPAN panel looked identical to one without. That
  matters: a SPAN sits between the aGate and the loads, so it changes what
  "home load" means.

  `discover()` does not carry it, so it is fetched and patched into the profile
  exactly as the grid profile already is. A failed lookup records `None`
  — unknown — which the merge reads as "keep what was known", rather than
  `False`, which would assert there is no panel. It now appears in the setup
  summary alongside the other accessories.

### Documentation
- Corrected `[BKL-SETUP-ADVANCED]`: an earlier draft stated SPAN could not be
  discovered and had to be asked for. That was wrong, and the correction is
  recorded rather than quietly edited out.

## [0.6.20] - 2026-09-18

### Added
- **"Existing FranklinWH entities" on the HA Entities tab.** Shows the leftovers
  the 0.6.19 survey finds, with the two things needed to act on them:

  - **How much recorder history each carries.** Long-term statistics are keyed
    on `entity_id`, so an entity adopting an old id inherits that history — the
    Energy dashboard keeps its years of data. An orphan with no statistics is
    only worth adopting to save editing automations; one with three years is
    worth real effort. The column says which.
  - **Which entity here does that job now** — the point of the exercise. While
    another integration's config entry exists its ids cannot be taken at all, so
    naming the replacement is often the only available remedy: an automation
    driving `switch.franklinwh_smart_circuit_2` can be corrected by hand.

  Matching is on what an entity *measures*, not its name, because the two
  integrations name things differently — that is the whole problem. Domains must
  agree, so a sensor is never proposed as the replacement for a switch, and a
  numbered circuit never matches a different number.

### Fixed
- **The entity matcher recognised almost nothing.** Its patterns used `\b`, and
  underscore is a word character — so `\bsoc\b` never matched
  `sensor.fw_battery_soc`. Only `smart_circuit` worked, because it has a literal
  prefix to anchor on. Entity ids separate words with `_`, `.` and `-`, which
  the patterns now bracket explicitly.

### Documentation
- `[BKL-SETUP-ADVANCED]` specifies the post-registration advanced setup in full:
  entity adoption with history, and the topology confirmation screen modelled on
  FEM's "Confirm Your Site Topology" — including the **default operating mode**,
  stored and readable from automations, so a gateway has an explicit baseline to
  return to rather than one Smart Dispatch infers.

## [0.6.19] - 2026-09-17

### Added
- **Entity survey (`GET /api/ha/entity-survey`).** Reports every FranklinWH
  entity already in Home Assistant and who owns it, in three buckets: ours,
  owned by another integration, and *orphaned* — registry rows no config entry
  owns, left by a previous install, which nothing will ever update while
  automations still point at them.

  It also states the fact that decides whether adoption is possible at all:
  **an entity id owned by another integration cannot be taken while its config
  entry exists, and disabling that integration is not enough — a disabled entry
  still holds its ids. It has to be deleted.** Read-only; adopting an id is a
  separate, previewed, confirmed action.
- **Stale retained-topic cleanup** (`GET /api/mqtt/retained/orphans`,
  `POST /api/mqtt/retained/clear`). Retained messages are why entities survive a
  restart — the broker replays the last message to every new subscriber — and
  equally why deleted ones come back: a retained message persists until
  something overwrites it, and nothing ever overwrites a topic we stopped
  publishing to.

  A real case found on a live system: `franklinwh//status/battery_status`, with
  an empty segment where the gateway short id belongs, still retained six months
  after the build that wrote it stopped existing. Clearing publishes a
  zero-length retained payload — the only way to remove one; MQTT has no delete.

  Scoped to our own topic prefix and refuses anything outside it even if asked,
  so a caller bug cannot wipe another integration's retained state. Confirmation
  is typed, because this is destructive.

## [0.6.18] - 2026-09-17

### Fixed
- **The dashboard still showed "Site 3447" after the name was stored.**
  `_hydrate_gateway_fields` overwrote the `gateways.site_name` column with
  `profile_json`'s copy, unconditionally. On an install whose profile predates
  site capture that copy is empty, so a good name was replaced with `""` and the
  heading fell back to the id — while the address directly beneath it, read
  straight from the column and untouched by the hydrator, displayed correctly.
  That asymmetry is what identified it.

  The column now wins and `profile_json` remains the fallback; an unparseable
  profile no longer erases a name either.

  This is the fourth place one value had to be fixed: store it (v61), widen the
  SELECT, guard the write against blanks, and now stop a second store
  overwriting it on the way out.

## [0.6.17] - 2026-09-17

### Fixed
- **Stale readings were published to Home Assistant as though they were live.**
  The `get_stats: API payload was empty` warnings are a known FranklinWH cloud
  glitch — HTTP 200 with `result: null` — documented upstream as
  `DEF-BLANK-STATS-PASSTHROUGH`. The library mitigates it by returning the last
  good reading with `is_stale = True` instead of zeros, which would have read as
  "battery dead, no solar". Those warnings are that mitigation working, not a
  fault here.

  But the library's `CACHING_STRATEGY.md` requires consumers to check
  `stats.is_stale` before writing to HA/MQTT, and we never did. We had our own
  guess instead — a `soc == 0` sentinel — which detects the symptom from
  *before* the library began substituting last-known-good values. Since then the
  readings look entirely plausible, so the heuristic never fires and every
  stale poll was published unmarked.

  The library's flag is now read at the call and takes precedence; the existing
  heuristics stay as a backstop for a glitch it has not classified, and the log
  says which signal fired.

## [0.6.16] - 2026-09-17

### Fixed
- **The Docker path failed silently when no broker was configured.** A Docker
  deployment has no Supervisor to ask, so an unset `MQTT_HOST` fell back to
  `localhost`, found nothing listening, and repeated a bare "connection
  refused" every 60 seconds. Because entities are published over MQTT
  Discovery, the visible symptom was Home Assistant showing no entities at all
  while the app looked healthy.

  Startup now says so plainly and looks for a broker on the usual names
  (`mosquitto`, `mqtt`, `broker`, `emqx`, `homeassistant`), naming the one it
  finds and the exact variable to set. It reports rather than connects: the
  add-on can assume `core-mosquitto` because the Supervisor vouches for it, but
  here the candidates are guesses, and publishing your telemetry to a broker you
  did not name is not a guess to make on your behalf.
- **`localhost` is now distinguished from "unset".** It is both a legitimate
  choice and the built-in default, so the value alone could not tell them
  apart — and someone who had deliberately set it was told it was not set.
- **A refused connection explains itself**, and is reported once rather than
  once a minute. It is no longer conflated with a credential rejection: the
  broker never answered, so the question is where it is, not who we are.

### Documentation
- `INSTALL.md` covers the Docker MQTT path, including what the failure looks
  like and why nothing can discover the broker for you.

## [0.6.15] - 2026-09-17

### Added
- **Home Assistant configures itself under the Supervisor.** There was nothing
  for you to enter — the add-on already holds a token and already knows where
  Home Assistant is — yet it showed three empty forms. This instance is now
  registered automatically as a Home Assistant Instance, and companion apps
  (`notify.mobile_app_*`) are discovered as notification devices. Both are
  idempotent and neither touches anything you set up yourself.

  This is what made notification routing impossible rather than merely untidy:
  a device route is foreign-keyed to an `ha_instances` row, and no row existed,
  so the Companion Devices table could not be filled in even by hand.

### Fixed
- **"Host Address: Not Set / Access Token: None Set / DISCONNECTED" about a
  working connection.** The panel reported *stored* values, which are
  legitimately empty under the add-on because nothing writes them — while the
  Home Automation tab, reading the effective connection, said "HA Integration
  Started" at the same time. Both were true of different things. The panel now
  reports the connection actually in use and says the Supervisor supplies it;
  the "Configure HA Connection" button is hidden when there is nothing to
  configure.
- **MQTT Explorer was rejected with HTTP 500 before its own auth ever ran.**
  `require_tier` declared `request: Request`, which FastAPI cannot inject into a
  WebSocket route — `TypeError: _dependency() missing 1 required positional
  argument`. The browser saw a failed handshake and showed a bare
  "Disconnected", so this looked like an auth or broker fault for days. The
  dependency now takes `HTTPConnection`, the base class of both, and defers to
  the route's own check for WebSockets.
- **Every "site-local" scheduled job was firing on UTC.** A trigger built
  explicitly — `CronTrigger(hour=3)` — takes the *process* timezone, not the
  scheduler's; only the string form inherits. Giving the scheduler a zone in
  0.6.11 therefore changed nothing for these jobs. In Sydney the macro planner's
  03:00 was running at 13:00, and the meso planner's 00:05/06:05/12:05/18:05 at
  10:05/16:05/22:05/04:05. TOU blocks and demand windows are local wall-clock,
  so this decided whether a plan matched the tariff. The telemetry rollup stays
  deliberately UTC.

  The test for it runs in a forced-UTC subprocess, because on a machine already
  set to the site's zone the bug is invisible — which is how it survived.
- **Every MQTT Explorer close now says why**, instead of closing silently with
  code 1000 and leaving the cause in the log only.

## [0.6.14] - 2026-09-17

### Added
- **Detects another FranklinWH integration polling the same cloud account.**
  FranklinWH serves one client per account; a second one gets HTTP 200 with
  `result: null`, so telemetry silently goes stale and the log fills with
  `get_stats: API payload was empty`. The Gateways tab now says so, and offers
  to disable the other integration.

  The check reads Home Assistant's **loaded** components (`/api/config` →
  `components`), not what HACS has downloaded. Those are different: a HACS
  download with no config entry sits on disk and polls nothing, and reporting it
  would send you hunting in Settings for something that is not there.

  Disabling is never automatic and never a side effect of detection. It needs a
  typed confirmation, because it changes an integration this add-on does not
  own, and it *disables* rather than deletes — Home Assistant's own Integrations
  page puts it back.

## [0.6.13] - 2026-09-17

### Changed
- **`amber_eval_log` is now `pricing_eval_log` (schema v62).** The table records
  every dynamic-pricing evaluation, whichever provider produced the prices —
  Amber, Localvolts, ComEd, AEMO or a flat tariff. Naming it after one of them
  made the scheduler report `source=amber_eval_log` on a site with no Amber
  account, which reads as a fault rather than a filename. The accessors follow:
  `log_pricing_eval`, `get_pricing_eval_log`, `get_latest_pricing_eval`.

  Amber remains a real provider and keeps its name in
  `src/services/pricing/amber.py` — a blanket rename would have broken the
  adapter. Only the generic engine concepts move.

  Existing rows are carried across, not dropped. A plain
  `ALTER TABLE ... RENAME` would have silently done nothing: the schema block
  creates `pricing_eval_log` before migrations run, so the destination already
  exists and a rename guarded on its absence skips, stranding the history. The
  migration copies and then drops, and a test with rows in it proves the
  history survives — the empty-table version of that test passed against the
  broken migration.

## [0.6.12] - 2026-09-17

### Added
- **Map Basemap setting on the Weather & Radar tab.** CARTO's public basemaps
  now require an API key; without one the tiles arrive stamped "API KEY
  REQUIRED" across both the radar map and the API Metrics map. The key is
  entered in the UI — add, replace or remove — and stored in this instance,
  never in the source: the add-on repository is public, and a committed key
  would be scraped. Saving a blank field keeps the existing key, so re-saving a
  form you did not retype cannot erase it; removing is a separate, explicit
  action. The stored key is shown masked.
- **A keyless fallback**, so an install with no key gets a plain working map
  rather than a watermarked one. The maps report which provider they are using.

### Changed
- **One place builds tile URLs.** The radar map and the API Metrics map each
  held their own copy, which is how they came to disagree about theme handling.

## [0.6.11] - 2026-09-17

### Fixed
- **0.6.10 set the container's timezone, which made stored timestamps mean two
  different things.** Setting the process to `Australia/Sydney` fixed the
  scheduler and broke the database: 132 columns default to SQLite's
  `datetime('now')`, which is UTC whatever the process says, while 53 Python
  call sites use naive `datetime.now()`, which is local. On a UTC container
  those agree, which is why neither this project nor FEM had ever shown the
  problem — the inconsistency was real all along and simply invisible. Once the
  process moved, the same column started receiving values ten hours apart
  depending on which line wrote them, and the log file carried both.

  The timezone is now *resolved* and handed to APScheduler, which takes its own,
  so TOU blocks and demand windows still fire on local wall-clock while stored
  timestamps stay UTC and comparable with every row already written. Nothing
  touches the process clock, and a test fails if anything tries.

### Known issue
- Rows written between the 0.6.10 upgrade and this release by a naive
  `datetime.now()` call site carry local time in an otherwise-UTC column. The
  window is short and mostly affects log lines and mode transitions; the
  underlying inconsistency between those 53 call sites and the other 175
  predates all of this and is tracked separately.

### Changed
- **MQTT Explorer says why it disconnected.** A bare "Disconnected" cannot be
  acted on; the close code separates our own auth check refusing the handshake
  (1008 — the session cookie did not survive the ingress hop) from the socket
  never opening at all (1006 — nothing proxied the upgrade).

## [0.6.10] - 2026-09-17

### Fixed
- **Regression from 0.6.8: the MQTT Explorer rendered its own source code.**
  An explanatory comment I added contained a double quote, and it sat inside a
  double-quoted `x-data` attribute — which ended the attribute there and spilled
  the rest of the component onto the page as text. A test now parses every
  Alpine JS attribute and fails if its brackets do not balance, which is what a
  truncated attribute looks like.
- **The timezone and the MQTT broker are now resolved in Python.** Moving the
  token export earlier (0.6.8) was not enough: every bashio Supervisor call
  still returned "Unable to access the API, forbidden", while the same token
  worked from Python in the same boot — `/addons/self/info` succeeded. So the
  add-on kept running on UTC, with TOU blocks and demand windows ten hours out
  in Sydney, and kept connecting to MQTT anonymously. Both settings are now
  filled in over the path that works, before the scheduler starts and before the
  publisher is built. Neither overrides a value you set. `run.sh` also exports
  `HASSIO_TOKEN` as well as `SUPERVISOR_TOKEN`, since which one bashio reads
  depends on its version.
- **A healthy scheduler was rebuilt three times, then stood down.** The micro
  ticker fired every 30s and succeeded throughout, but `mark_tick()` was only
  reached inside per-gateway work and no gateway had opted in — so a tick with
  nothing to do stamped no heartbeat. The monitor fell back to `amber_eval_log`,
  a change log a site without dynamic pricing never writes, and read "no rows
  ever" as "stopped". The tick now stamps the heartbeat before it looks for
  work, and an absent signal is no longer treated as a stall. A heartbeat that
  *does* stop is still a stall — the monitor is not disarmed.

## [0.6.9] - 2026-09-16

### Fixed
- **The dashboard showed "Site 3447" above "SITE ID: 3447".** Discovery returns
  the site's name ("Home") and address for every gateway, and registration kept
  neither — only the id. The header template already preferred a name; nothing
  ever supplied one, so it fell back to the id and then printed the same number
  again underneath. Names and addresses are now stored (schema v61), the
  secondary line shows the address when there is one, and the id appears only
  when it is not already the heading.
- **Installs registered before this get their names filled in** by a background
  backfill at startup: one discovery call, only while a name is missing, and a
  failure leaves the id showing exactly as before. A migration cannot do this —
  the names live in the cloud.
- **A later write no longer erases a known site name.** Most callers of
  `upsert_gateway` know nothing about sites, so the first one to run would
  otherwise blank the name and silently revert the header to an id.

## [0.6.8] - 2026-09-16

### Fixed
- **Every Supervisor call from `run.sh` was failing with "forbidden", and the
  add-on was running on UTC.** bashio authenticates with `SUPERVISOR_TOKEN`,
  and s6-overlay does not inherit Docker ENV — the reason `PYTHONPATH` is
  exported explicitly. The token recovery sat just above the uvicorn exec, so
  the timezone lookup and the MQTT service lookup, both earlier in the script,
  ran unauthenticated and silently fell back. That single ordering mistake
  caused both of the symptoms chased separately:
  - no timezone, so the container ran UTC while TOU blocks, demand windows and
    export windows are local wall-clock — ten hours out in Sydney;
  - no MQTT service, so no broker credentials, so an anonymous connection the
    Mosquitto add-on refuses with `[code:135] Not authorized`.

  The token is now exported before anything calls bashio, and a test fails if a
  Supervisor call ever precedes it again.
- **MQTT Explorer connected to Home Assistant instead of the add-on.** It built
  its WebSocket as `window.location.host + 'api/ws/mqtt'` — no separator, so
  `…:8123api/ws/mqtt`, and no ingress prefix, so it aimed at Home Assistant's
  own port. It now resolves against the page, keeping the ingress path.

## [0.6.7] - 2026-09-16

### Fixed
- **The Logs tab showed "0 app entries" while the log file was filling up.**
  It fetched `/api/logs/file` — absolute. Under Home Assistant ingress the
  add-on is served from `/api/hassio_ingress/<token>/`, so a leading slash
  leaves the add-on and asks Home Assistant for its own `/api/logs/file`. A
  test now rejects any absolute `/api/` fetch in the frontend.
- **"Sync Location from Cloud" could never have worked.** It called
  `client.raw("get_equipment_location", gatewayId=…)`. The cloud client has no
  `raw` attribute, and the method it was reaching for takes no arguments — so
  every press raised `AttributeError` into a broad `except` and returned 502.
  Two call sites had it. The existing test passed because it mocked `raw`.
- **Coordinates now resolve automatically**, at gateway registration, from
  Home Assistant's own configured latitude/longitude first (no cloud call) and
  the FranklinWH cloud second. Without them there is no solar forecast, and the
  only way to set them was the button that never worked.
- **A latitude sign was guessed from longitude.** `100 < lng < 180` was read as
  the southern hemisphere, which flips Tokyo, Seoul, Beijing, Taipei and Manila
  into the Southern Ocean — and reported a spurious "Latitude sign error" to
  correctly configured users there. A sign is now only corrected against a
  source that has one, never inferred from a coordinate.
- **`[code:135] Not authorized` repeated once a minute, forever.** A credential
  rejection cannot be retried into success, and the code names neither cause nor
  fix. It is now reported once, as an error that says which broker refused us,
  whether we sent a username at all, and which setting to change. `run.sh` also
  logs whether Home Assistant actually supplied broker credentials — it hands
  over the host even when it has none, and the connection is then attempted
  anonymously, which the Mosquitto add-on refuses by default.
- **A stray `print()` ran on every automation signal.** stdout is not the log
  file, so it appeared in `docker logs` and nowhere the app could show it.

## [0.6.6] - 2026-09-16

### Fixed
- **The setup wizard had no stacking order, so the dashboard painted over it.**
  It was written with `z-[10000]`, but the Tailwind stylesheet is prebuilt and
  committed (the add-on image has no node) and had not been rebuilt since the
  wizard was added — so the class did not exist and resolved to nothing. A
  gateway card, `position: relative` and later in the document, then covered the
  modal. A test now asserts every arbitrary `z-[…]` used in a template is
  present in the built stylesheet, and that nothing outranks the wizard.
- **"Install the Mosquitto broker add-on" shown to users who already have one.**
  The wizard read `/mqtt/status`, which reports only whether *our* publisher has
  a connection, and rendered any falsy answer as a missing broker. It now asks
  the Supervisor (`GET /services/mqtt`) whether any add-on provides a broker,
  and distinguishes *install one* from *connect to the one you have*. Outside
  the Supervisor it no longer claims a broker is missing, because it cannot see.

### Added
- **The wizard reports FranklinWH entities already in Home Assistant.**
  `/ha/status` already counted them; the wizard discarded the field, so a
  re-install looked identical to a first install.

## [0.6.5] - 2026-09-16

### Fixed
- **Setup wizard could not register a gateway.** Sign-in succeeded, then
  registration posted `{email, password, gateways[]}` to `/gateways/linked`,
  which takes a *single* gateway with `full_serial` at the top level and reads
  credentials that a fresh install has not stored yet. It now posts each chosen
  gateway to `/api/gateways`, and one gateway failing no longer abandons the
  rest.

### Added
- **Wizard groups gateways by site** when an account has more than one, instead
  of listing every gateway flat — two sites can reasonably give a gateway the
  same name. A single-site account sees no heading.
- **Wizard warns on installer accounts.** `/api/gateways/discover` already
  returned an account profile and the wizard discarded it, making the wizard the
  one path into FWHAI that could enrol a customer's gateway without saying so.
  It now applies the same rule as the Gateways tab — the cloud's own answer
  first, site count only as a softer fallback — and a test fails if the two
  paths drift apart. The warning renders above the picker, because the wizard
  pre-ticks everything it finds.

## [0.6.4] - 2026-09-16

### Sign-in reported no gateways on an account that had one (2026-09-16)

Two bugs the first real sign-in exposed, both the same kind: reading something
that is not produced.

- **`_discover_gateways` returns a flat `gateways` list** and always has — the
  headless path in `main.py` reads it that way. The wizard read
  `sites[].gateways`, got nothing, and reported "Signed in, but no gateways are
  linked to this account" on an account with one. The picker also keyed on
  `short_id`, which is optional; it keys on `serial` now, which is what
  registration needs and what every gateway carries.
- **`SUPERVISOR_TOKEN` never reached the application.** s6-overlay does not
  inherit Docker environment variables — `run.sh` already exports `PYTHONPATH`
  for exactly that reason. Without the token the add-on reported itself as
  "Docker" rather than "Home Assistant add-on", offered to collect an HA URL
  and a long-lived token it did not need, and `api_ha.py` could not
  self-configure the connection at all. It is read from s6's container
  environment and exported, and its absence is now logged.


## [0.6.3] - 2026-09-16

### MQTT: the broker details were offered and ignored (2026-09-16)

`config.yaml` has always declared `services: ["mqtt:want"]`, which makes the
Supervisor hand over the host, port, username and password of whichever broker
add-on is configured. Nothing read it. `run.sh` guessed `core-mosquitto` with
no credentials, which fails outright against a broker requiring auth and is
simply wrong for anyone running a different one.

The add-on now asks the Supervisor and uses what it gets, including the
credentials. A value the user typed still wins — they may be pointing at a
broker the Supervisor knows nothing about — and the `core-mosquitto` guess
remains only for the case where the Supervisor has nothing, which is where the
guess is the best available answer.

When no broker exists at all, the log and the wizard now name the fix rather
than the symptom: "Install the Mosquitto broker add-on" instead of "not
connected". Entities are published over MQTT Discovery, so without a broker the
integration runs and Home Assistant sees nothing — which is worth saying out
loud rather than leaving as an empty entity list.

Nothing is installed automatically. Installing software on someone's system to
satisfy a dependency is a decision for them.


## [0.6.2] - 2026-09-16

### The setup wizard rendered unbound (2026-09-16)

0.6.1 shipped the wizard loading after Alpine. Deferred scripts run in document
order, so Alpine initialised first, evaluated `x-data="setupWizard()"` against a
function that did not exist yet, and left the element in the DOM with nothing
bound to it: visible, blank step number, empty body, only Skip working.

Tab modules avoid this by being fetched on demand when their tab opens. The
wizard lives in the shell and cannot, so it must be defined before Alpine
starts — it now loads beside `app.js`, ahead of it. Tests pin the ordering and
the cache-busting version, without which a browser keeps the first copy it saw
and a fixed module goes on behaving like the broken one.

Also: an empty `[Unreleased]` heading is no longer published. In the repository
it is where the next change goes; in the Changelog tab it was dead space above
the release the reader came for.


## [0.6.1] - 2026-09-16


### Phone rendering: unreadable values, and 56px of undismissable rail (2026-09-15)

- **`--primary-light` was a 15% alpha glow.** It aliased `--accent-glow`,
  `rgba(249,115,22,0.15)` — a background tint. Used as a *text* colour, which
  is what the active tab label, "Force Re-evaluate", "24h Engine" and the
  section chevrons all do, it is nearly transparent; on white it vanished.
  Every accent now defines opaque `--accent-on-dark` / `--accent-on-light`
  variants, and light themes take the darker one, because a light tint on white
  is the same bug one step less severe. The glow remains for the backgrounds it
  was designed for.

- **`text-gray-700` was a surface colour, not a text colour.** The Tailwind
  config maps gray-700/800/900 to `--surface-*` because `bg-gray-800` (522
  uses) and `bg-gray-900` (290) need them. The same names as *text* then
  resolved to a pale fill on a pale card — in light mode `text-gray-700` was
  `#e2e8f0` on white, which is why "Current SOC: 66.842%" was near-invisible.
  Dark mode hid it, `--surface-2` being a readable mid grey there. The text
  utilities now route to text variables while the bg utilities keep the
  surfaces, correcting 60 call sites without touching a template.
- **The collapsed sidebar reserved a 56px rail on a phone** — 14% of a 390px
  screen, permanently, and not dismissable — while the open one sat in the
  flow and squeezed the content it was covering. Below 1024px it is now a
  drawer: zero width closed, an overlay open. The rail stays on desktop.
- **98 grids declare a fixed column count with no responsive prefix.** At ~80px
  a column the content collides; the solar forecast card rendered "31.9",
  "5.48" and "02:00 pm" on top of one another. Below 480px, three or more
  columns collapse to two and wide two-column layouts to one — one rule rather
  than 98 template edits, and left above 480px so tablets keep their layouts.

### First-run setup wizard (GH [#1](https://github.com/david2069/franklinwh-ha-integrator/issues/1)) — 2026-09-16

Headless provisioning already existed and kept working, with two gaps closed —
one of them created by the wizard:

- **It registered one gateway and dropped the rest.** `cloud_gateway` was
  matched against a single gateway, so a two-gateway account provisioned half
  and looked like it had worked. It is now a filter, not a picker: blank means
  every gateway on the account, and a comma-separated list selects several. A
  suffix matching nothing logs the serials that were available rather than
  provisioning nothing in silence, and one failure does not abandon the rest.
- **It did not mark setup complete**, so the new wizard would have opened on an
  install that provisioned itself and asked for credentials it had already been
  given. It is set only after at least one gateway registers, so a failed
  provision still gets the wizard.

Documented in the add-on's DOCS.md; the same option names work as environment
variables for a plain Docker deployment.

Queued 2026-08-07 and never built, while the app referred to it constantly:
startup logged "Awaiting Setup Wizard completion", gateway discovery answered
"Complete the setup wizard first", and `setup_required` was returned by the API
and read by nothing. A first-time user landed on the admin shell and had to
know to visit Gateways, then HA Entities, then MQTT Admin, in that order.

Three steps, one unavoidable input, per CONTRIBUTING's zero-input principle:

1. **Account** — FranklinWH email and password, then pick the gateways found.
   The only thing the integration cannot work out for itself. Everything
   discovered is pre-selected; unticking beats hunting for the one missed.
2. **Home Assistant** — under the Supervisor the connection and the MQTT broker
   configure themselves, so this reports what was found rather than asking two
   questions with one answer. Elsewhere it says where to set them and that
   entity discovery works without them.
3. **Ready** — what is configured, and that pricing is set on its own tab.

`GET /api/setup/state` reports each step from the source of truth that already
owns it — the gateway list, `/ha/status`, `/mqtt/status` — rather than forming
its own view, because a second opinion about whether MQTT works is a second
thing to be wrong. Every check is wrapped: the wizard runs before anything is
configured, so each one is expected to fail at least once.

`setup_complete` is a "do not ask again" marker, not a claim that the install
works — an install with no gateway is incomplete whatever the flag says, which
is what makes it safe to set early. Completing and skipping are audited
separately so a support conversation can tell them apart.

The startup message and the discovery error now describe a wizard that exists.

### First-boot fixes found by installing the add-on for real (2026-09-16)

- **The add-on ran on UTC.** Its first boot logged "Can not find any timezone
  configuration, defaulting to UTC": compose mounts the host's
  `/etc/localtime`, an add-on cannot, and nothing set one. Not cosmetic — TOU
  blocks, demand windows and export windows are local wall-clock and the
  scheduler fires on local time, so in Sydney every one of them was ten hours
  out. `run.sh` now takes the timezone from the Supervisor and sets both `TZ`
  and `/etc/localtime`, because tzlocal reads the latter first; the image gains
  `tzdata`, without which a zone name resolves to nothing and the container
  stays on UTC silently. When the Supervisor cannot supply one it now says so
  rather than running offset in silence.
- **A fresh install had no way to sign in.** Gateways → Discover returned "No
  account credentials stored. Complete the setup wizard first" — a dead end,
  and one naming a wizard that does not exist for credentials; the only wizard
  in the app is the security one. The API now returns a `needs_credentials`
  flag rather than a sentence to string-match, and the discovery modal asks for
  the account email and password, then goes straight on to discovery.

### The add-on is built by the Supervisor, not pulled from a registry (2026-09-16)

The prebuilt-image route was built and then abandoned, for two reasons found by
using it: GHCR package visibility has no API — a package is private on first
push and `PATCH /user/packages/container/<name>` is a 404, a missing endpoint
rather than a scope error, so it can only be changed in a browser — and a public
manifest pointing at private images gives an add-on that appears in the store
and then fails on an unauthorised pull. That state happened here.

`franklinwh-addon` now carries the full build context and `config.yaml` has no
`image:` key, so the Supervisor clones and builds. One push per release, no
registry, no manual step. The generated context was built against the real
`aarch64-base` before publishing, so a missing `COPY` source fails here rather
than on a user's machine hours later.

`.github/workflows/addon-image.yml` is removed and `publish_addon.sh` is down to
what remains: preflight, regenerate, push, verify. `franklinwh-ha-integrator`
stays private — preflight fails if it is ever made public, since it carries a
`repository.json` and would serve the same add-on from the private tree.

The trade is that the add-on source is public. The Changelog tab and update
notifications are unaffected: both come from the manifest, not from how the
image is produced.

### The public add-on repository is now the add-on, not FEM (2026-09-16)

The add-on's Changelog tab is trimmed to `[Unreleased]` plus the last three
releases — 122 KB and 1,394 lines was a wall on the one tab people open to
answer a single question, what changed in the version being offered. The full
history ships alongside as `CHANGELOG-full.md` and is linked from the foot of
the tab. That link points at the **public** add-on repository; pointing it at
the source repo would have 404'd for every reader, the same trap as the
container image's source label.

Images for 0.6.0 are built and pushed. Two notes from doing it for real:

- **`gh api user/packages/...` needs a `read:packages` scope** that
  `gh auth login` does not grant, so it answered 403 and
  `publish_addon.sh` reported a successful build as "missing after the build".
  Checks now ask the registry for an anonymous manifest instead — no scope, and
  it answers what actually matters: can Home Assistant pull this?
- **Package visibility is genuinely UI-only.** `PATCH
  /user/packages/container/<name>` is a 404 — a missing endpoint, not a scope
  error. It is one visit per package, ever: a package keeps its visibility
  across later pushes, so releases after the first need no intervention.

The image's `org.opencontainers.image.source` now points at the **public**
add-on repository rather than the private source one, so the package page sends
visitors somewhere that exists instead of a 404.

`franklinwh-addon` still held FranklinWH Energy Manager: 349 files, of which
128 were FEM source and 193 vendored dependencies, plus `sync.sh` and an
`ENTITIES.md` describing a retired product. Anyone adding the repository read
instructions for the wrong integration, and publishing it would have exposed
FEM and its whole history.

Replaced with a seven-file manifest on an orphan branch — no source, no FEM
history, one commit. `scripts/make_public_addon_repo.sh` generates it, and its
README is now a complete install guide rather than a stub: the exact menu path,
what each step does, requirements including why 32-bit is not offered, and
where updates and support come from.

The previous contents are mirrored to `build/backup/franklinwh-addon.git`
(86 commits) before anything is overwritten. Repository visibility is
deliberately still private — that step is irreversible in practice and remains
a maintainer decision.

### Tailwind is now a prebuilt stylesheet, not a browser JIT (GH #4 item 1)

`admin.html` loaded the Tailwind Play CDN build: 403 KB of JavaScript that
scans every element's classes, generates CSS at runtime, and installs a
document-wide MutationObserver that recompiles on every DOM change. Alpine
mutates the DOM constantly, so that is a compounding CPU and memory load — the
leading suspect in the iOS Safari crashes. Tailwind documents the Play build as
development-only.

- **161 KB of CSS (22 KB gzipped) replaces 403 KB of JS**, and nothing blocks
  the parser any more. Built by `scripts/build_css.sh` with the standalone CLI,
  so no node is needed; the output is committed because the add-on image has no
  node and the Supervisor runs no build step.
- The inline `tailwind.config` block moves to `tailwind.config.js`. It only
  existed because the JIT needed it at parse time.
- Not minified. Minifying dropped 59 selectors: the config maps the gray
  palette onto CSS custom properties, and Tailwind cannot apply an alpha
  modifier to a variable, so `bg-gray-500/10` compiles to a declaration the
  minifier discards. Those classes never worked under the JIT either — but
  shipping what Tailwind generated removes a variable from a subtle change.
  Gzip recovers most of the difference.
- **The budget guard could not have caught this.** `BLOCKING_JS_KB` was 450
  while Tailwind alone was 403 — set just above the offender, so the rule added
  for this crash could never fail on it. Ratcheted to 60 blocking / 550 total,
  both below current actuals, and verified to fail when a 201 KB script is made
  synchronous again.
- The icon check reported any `href="#..."` as a broken icon reference, so an
  ordinary in-page anchor failed the run. Narrowed to `<use href="#...">`,
  which is what its own docstring always said.

Verified: no class names are composed at runtime (the one thing a prebuilt
stylesheet cannot see), the content globs cover every file carrying a class,
and the built stylesheet contains the classes in use.

### Mobile shell: the sidebar covered the page it was needed to escape (2026-09-15)

Safari on iPhone killed the tab on Smart Dispatch, and getting back was worse
than it needed to be.

- **The sidebar defaulted open on every screen size.** On a phone it covered
  the content on load, and the only control that closes it — the hamburger —
  sits underneath it. It now starts closed below 1024px and open above.
- **Choosing a tab closes the overlay**, on narrow screens only.
- **The backdrop was gated on `window.innerWidth < 1024` inside an `x-show`.**
  Alpine evaluates that once, so after a rotation it kept whatever the first
  render decided. It now follows a `matchMedia` listener, and returning to a
  wide screen reveals the sidebar rather than leaving a desktop layout with no
  navigation.

The crash itself is not fixed by this — see the note below.

### Publishing the add-on without publishing the source (2026-09-15)

The Supervisor clones a **public** repository to install an add-on, so the
add-on repo cannot be private — while FWHAI's source is a beta that should not
be browsable.

- **Prebuilt images, thin public repo.** `config.yaml` in the public repo
  carries `image: ghcr.io/david2069/fwhai-{arch}`, so the Supervisor pulls
  rather than clones and builds. The public repo is seven files —
  `repository.yaml`, `config.yaml`, `CHANGELOG.md`, `DOCS.md`, icon, logo,
  README — and no source. It also makes installation seconds rather than
  minutes on a Pi.
- **`.github/workflows/addon-image.yml`** builds both architectures on a `v*`
  tag and pushes to GHCR, running the add-on package tests first so a stale
  build context cannot ship as a release.
- **`scripts/make_public_addon_repo.sh`** generates the public contents.
- **`franklinwh_ha_integrator/build.yaml`** for the build-from-source path,
  pointing at the Home Assistant base images. FEM's equivalent pointed at
  `python:3.12-slim`, which is why its entrypoint had to differ — `run.sh` is
  `#!/usr/bin/env bashio`, which exists only on the HA bases.
- **`docs/addon_publishing.md`** records the decision and the steps, including
  the one that matters: `franklinwh-addon` currently holds FEM's source (128
  files) and vendored dependencies (193), so toggling it public as-is would
  publish FEM and its entire history. Reuse the repo, replace the contents via
  an orphan branch, and toggle visibility only afterwards.
- Tests assert the generator never copies source, the workflow's architectures
  match the manifest's, the context check runs before the push, and
  `build.yaml` uses the HA bases.

Not done here, deliberately: tagging a release, making the GHCR packages
public, and changing repository visibility are outward-facing and irreversible
in practice.

### Home Assistant add-on: first real build, and the assets the Supervisor shows (2026-09-15)

- **The add-on has now actually been built.** Every prior build was
  `docker compose` on `python:3.12-slim` — glibc, amd64 — so the Alpine/musl
  path was verified only by querying PyPI. Both declared architectures now
  build against the real Home Assistant base images and import the full
  application: `aarch64` natively and `amd64` emulated, with cryptography,
  pydantic_core and bcrypt loading on musl.
- **armv7 confirmed unbuildable, for a stronger reason than recorded.** The
  build fails with `Computed rustc target triple:
  arm-unknown-linux-musleabihf / Target triple not supported by rustup` —
  adding a toolchain would not have helped, because the target does not exist
  as far as rustup is concerned. The note in `config.yaml` said "needs cargo
  and rustup"; it now records what actually happens.
- **`icon.png` and `logo.png`** — an add-on without them renders as a blank
  tile in the store. Generated by `scripts/make_addon_assets.py` so the mark
  can be regenerated if the brand colour moves, rather than being an
  unexplainable binary.
- **`DOCS.md`** — the add-on's Documentation tab was empty. Covers
  installation, every option in the manifest, what the integration provides,
  supported hardware and the four things most likely to go wrong.
- **Tests** pin all of it: images are validated from the PNG header rather than
  the filename, every option in `config.yaml` must appear in `DOCS.md`, and the
  asset generator must still parse.

### Tariff model, billing, and the device registry (2026-08-08 → 2026-09-14)

125 commits since 0.6.0. The through-line is the same defect shape found in
several places: a value stored, editable and never read — so the UI looked
configured and the behaviour was a constant.

**Billing became real.** FWHAI's pricing was the FranklinWH Cloud API's shape
(seasons × day types × four wave types, buy and sell each), which is device
configuration and cannot express how a bill is built.

- **`tariff_costing`** — standing charges, demand, export windows, minimum
  bill. Cents throughout; no rate is ever adjusted on its way through.
- **`bill_period`** — derives the demand peak (highest *average* over the
  plan's interval, import only) and in-window energy (integrated from
  instantaneous kW, since the cumulative counters reset daily) from
  `gateway_metrics`.
- **`cost_breakdown`** — `get_daily_cost` / `get_monthly_cost` now itemise
  energy, fixed charges, demand, export windows and listed standing charges.
  Previously they added the three fixed columns and nothing else; the demand
  window, export window and free allowance were stored and never read.
- **Direction is explicit, sign is data.** The channel (dynamic) or window type
  + `rate_kind` (static) carries direction; a sign means the price moved
  against the normal direction of its channel. All four cases occur — including
  being paid to import and charged to export. `build_breakdown` clamped both
  sides with `max(0.0, …)`, discarding exactly those two.
- **Schema v58–v60** — plan timezone/type/export limit/permission flags,
  minimum bill, demand measurement (interval × count, basis), free export
  allowance, `utility_standing_charges` as a list, and `rate_kind`.
  Window rates converted from `$/kW/month` to cents (v59): they had been
  understating every demand charge 100× while the export rate in the same
  column was correct.
- Documented in **`docs/tariff_model.md`**.

**Device registry drives capability.** `_rated_power_per_unit = 5.0` and
`_rated_kwh_per_unit = 13.6` were aPower X specs applied to whatever was
installed. Limits now resolve per unit from the catalog, keyed on
`peHwVersion`, and are **summed** — an aGate X 1.3 can carry aPower X, 2 and S
together. Per-mode figures the cloud never reports (AC-solar and MPPT ceilings)
come from the registry, which is what it is for. The Control tab's dispatch
slider follows the fleet instead of a hardcoded 5 kW.

**Dispatch codes have one home.** The id→label map was written out six times
and had drifted. `dispatch_codes.py` is now the only copy, with an intent and a
description per code, served at `/api/dispatch-codes`. Code 1 (`aPower to
home`) is documented as an export strategy — it discharges to the house, never
charges, and sends surplus solar to the grid.

**Optimiser.** Generates a dispatch schedule from the tariff as a preset,
planned against a typical day for the season from site history. Never emits
Standby (it parks the battery, so a cheap block spent there buys what the
battery was holding). Block names carry intent. SOC bounds are sized from
forecast peak load, and a battery that cannot carry the peak is reported rather
than silently clamped.

**Snapshots.** Push history is findable, readable and removable — paged,
labelled, deletable — and a snapshot can be promoted to a preset. Cloud ids are
stripped on the way (they belong to the source gateway); operational fields are
preserved (the push path defaults them, so dropping them resets SOC limits
silently).

**Fixes worth naming:**
- `franklinwh_tou` shipped an empty forecast for its entire life — it read
  `touDispatchList`, a key nothing produces.
- Saving a utility service could write defaults over stored settings.
- Relays always displayed `O`: the template compared a boolean to `0`.
- "Last Seen" showed when the container last started — `touch_gateway` existed
  and was called from nowhere — and rendered naive UTC as local.
- Envoy live polling used `http://` against HTTPS-only v7 firmware, while the
  capability probe reported success having reached nothing.
- The add-on declared five architectures; three cannot build (Alpine/musl with
  no toolchain, and eleven dependencies without musllinux wheels).

**Regression: 1686 passed, 2 skipped.**

---


### Admin tab rename + Device Registry structural classification (GH [#18](https://github.com/david2069/franklinwh-ha-integrator/issues/18) / BD-06)

Half-shipped delivery for the "retire SysAdmin tab + refresh Device Registry" brain-dump item. Full retire of the tab needs sub-tab-by-sub-tab home decisions (System / Storage / Database / Environment / Device Registry each have real content that has to live somewhere) — scoped out for now, ships as a rebrand + fixing the recurring "silent misclassification" bug in the seed logic.

**UI changes:**
- **Sidebar label "SysAdmin" → "Admin"** (URL slug `?tab=support` stays for backward compat with external bookmarks / HA REST sensors)
- **Tab title, all in-body guidance text ("Configure in Admin", "Diagnostics (excludes Admin)", "Admin →", etc.) updated** across `support.html`, `smart_dispatch.html`, `health.html`, `smart_dispatch.js`
- **Dead "Notifications" sub-tab entry removed** — nav item existed at `support.html:22` but had no content div behind `sysTab === 'notifications'` (clicking did nothing). User's brain-dump confirmed notification config moved elsewhere.

**Device Registry seed fix:**
- **`_APOWER_HW = {0, 1, 2, 3, 4, 5, 6}` hardcoded whitelist replaced with structural derivation** in `db.py::seed_device_models` — previously, any new aPower hw_version added by a future `franklinwh-cloud` release silently misclassified as "agate" until someone remembered to bump the constant. New rule: SKU prefix `AGT-*` (or name starts with `aGate`) → agate; anything else → apower. Verified against all 12 installed models (0-6, 100-104) — classification unchanged for every existing entry.
- **`has_mppt` derivation improved** — prefer the cloud catalog's own `has_mppt` field (available from v0.4.9+), fall back to the name-string match for older client versions. Removes the assumption that "aPower S" is the only MPPT-capable variant.
- **15 new tests in `tests/test_device_registry_seed.py`** covering: every installed model classifies correctly, hypothetical future `hw_version=7` (aPower X-02-AU) auto-classifies, catalog `has_mppt` flag wins over name-string, defensive test that any cloud-client model with hw_version ≥ 100 must be agate.

**Not shipped (deferred, tracked separately):**
- Full retirement of the Admin tab (needs sub-tab-by-sub-tab home decisions — System settings could move under a new "Settings" pane; Database browser probably stays somewhere admin-gated; Environment info could fold into Diagnostics Dashboard [#2]; Storage/Backup UI needs a natural home).
- Device Registry UI refresh (the sub-tab exists; content refresh from the newer catalog happens naturally on next `franklinwh-cloud` upgrade to v0.4.9+).

Regression: 667 passed, 1 skipped, 0 failed.

### Scheduler → Automations tab: invert filter to structural allow-list (GH [#33](https://github.com/david2069/franklinwh-ha-integrator/issues/33))

Fixes a maintenance-heavy defect class that has now blanked the Automations tab twice in the last 24 hours. `/api/scheduler/jobs` previously used a block-list of known system-owned job ID prefixes (`sd:*`, then also `persona:*`); every new system-owned APScheduler job added anywhere in the codebase had to remember to update the filter, or the endpoint 500'd (Pydantic `RuleResponse` validation failure on the missing `gateway_serial`) and the Automations tab silently went empty.

Inverted the filter to a structural allow-list: return only jobs whose kwargs match `RuleResponse` (must have `gateway_serial: str` AND `conditions: list`). System-owned jobs are naturally excluded regardless of ID prefix — no per-prefix maintenance ever needed.

- **New helper `_is_user_rule(kwargs)`** at `src/routes/api_scheduler.py` — module-scope pure function, easy to test
- **Comment updated** at the filter site explaining the choice + linking GH #33
- **13 new unit tests** in `tests/test_scheduler_jobs_filter.py`:
  - Typical user rule passes
  - SD Macro / persona:weekly jobs excluded
  - Missing / empty / None `gateway_serial` excluded
  - Missing / non-list `conditions` excluded
  - Empty conditions list still passes (users can legitimately save 0-condition rules)
  - Non-dict kwargs excluded (defensive)
  - **Future system jobs auto-excluded** — proves that a hypothetical new system-owned scheduler job with any kwargs shape will NOT crash the endpoint

Live-verified: `GET /api/scheduler/jobs` returns 200 with all 7 user automations. Regression: 652 passed, 1 skipped, 0 failed.

### Zero-input design principle codified (GH [#13](https://github.com/david2069/franklinwh-ha-integrator/issues/13) / BD-18)

Locks in the design discipline that Phase A (v0.6.0) + subsequent per-tab gating already reflects. Prevents FEM-style feature creep from creeping back in as contributions ramp up.

- **`CONTRIBUTING.md` new "Zero-input principle" section** — states the three-touch install target ("install Add-on, enter creds, click Start") and the four questions every PR touching config/wizard/toggles must answer.
- **`.github/pull_request_template.md` created** — includes a "Zero-input review" checkbox block with the four questions inline; contributors delete the block when it doesn't apply.
- **New label `needs:zero-input-review`** (yellow #fbca04) — reviewers apply to any PR adding config surface / user question / toggle so triage flags them for the checklist.
- Cross-references `docs/persona_matrix_plan.md` and BD-04 / BD-16 / BD-18 for the underlying architecture.

Not a code change; a discipline change. Applies retroactively to all future PRs.

### Services tab cleanup + Pricing tab persona-gating (GH [#19](https://github.com/david2069/franklinwh-ha-integrator/issues/19) / BD-11)

Second-half cleanup of the Services (formerly "Services Setup") tab. The Live Pricing card and 24h Energy Forecast strip are removed from Services — they duplicated richer per-provider displays already present on the Pricing tab and cluttered the Services concern (topology + utility service subscriptions).

**UI changes:**
- **Services tab Overview** — deleted the Live Pricing 3-column card (Grid Import / Grid Export / Price Window) and the 24h Energy Forecast strip (48 half-hour slots). When either data source was populated, the tab now shows a blue-tinted callout: *"Live pricing + 24h forecast moved. The at-a-glance import/export prices and forecast strip now live on the Pricing tab, alongside per-provider detail."* with a clickable jump to `?tab=dynamic_pricing`.
- No new duplicate blocks added to Pricing tab — it already has fuller live-price displays per provider (line 219+ import/export, line 433+ forecast entries per-provider under Amber/AEMO/ComEd/etc.).

**Persona gating (Phase B):**
- New `persona_gates.dynamic_pricing` — false when `persona.is_pricing_available()` is false (i.e., tariff_type is `flat` or `none`). Hides the Pricing tab from off-grid and flat-rate users.
- Permissive-until-detected bypass still applies (existing installs before first-run silent detect see the tab).
- Override precedence carried through — `persona.override.tariff_type=tou` force-shows the tab.
- 5 new tests in `tests/test_persona.py`:
  - `test_persona_gates_pricing_shown_for_dynamic_tariff`
  - `test_persona_gates_pricing_shown_for_tou_tariff`
  - `test_persona_gates_pricing_hidden_for_flat_rate`
  - `test_persona_gates_pricing_hidden_when_off_grid_none`
  - `test_persona_gates_pricing_override_forces_visible`

**Not shipped in this commit (deferred to per-rule Phase B wiring):**
- SD engine rule gating on `pricing_available` / `solar_available` / `grid_charging_permitted` — flags exist on `persona.sd_engine_flags()`, per-rule consumption is separate PRs (each pricing-related rule needs its own guard).
- Services Setup sub-tab audit (Sites/Gateways / Electricity Utility) — sub-tabs are still visible; no persona gates applied within them yet.

Regression: 639 passed, 1 skipped, 0 failed (36 persona tests total).

### Reporting drill-down: honest placeholders for missing time-span data (GH [#23](https://github.com/david2069/franklinwh-ha-integrator/issues/23) / BD-20)

Cloud Reporting summary cards previously rendered `0.00 kWh` on Week/Month/Year/Lifetime views when the underlying cloud endpoint returned no energy arrays for that time span. Users had no way to tell "you had zero solar last year" from "the view isn't wired". This commit replaces the silent zeros with an honest UI state; the root-cause fix (why the cloud API returns empty for these periods) is deferred to follow-up work with live captures.

**Frontend fix:**
- `cloudFlowSummary.hasData: boolean` — new field on the summary state. True when the API returned energy arrays for the selected period (regardless of whether their sums happen to be zero); false when arrays are absent (endpoint responded but coverage for that time span is unimplemented / empty).
- All 4 summary card totals (Solar Production, Battery Flows, Grid Flows, Home Load) + 4 sub-row totals (Total Discharged, Total Charged, Total Import, Total Exported) render `—` instead of `0.00 kWh` when `hasData === false`.
- New banner above the card grid on non-Day views when `hasData === false`: *"Historical data not yet available for this time span."* with quick-jump buttons to Day (5-Min) / Day (Hourly) for reliable numbers, plus a link to the BD-20 backlog anchor.
- "Energy Analysis Target" chart heading now shows *"No historical data for this time span"* instead of `"Ready"` when applicable.
- Day view (5-Min or Hourly) always keeps its detailed breakdowns — `hasRaw` path sets `hasData: true` unconditionally so a genuine zero-solar overnight day still renders `0.00 kWh` (as it should).

**Coverage matrix documented:**
- New `docs/reporting_coverage_matrix.md` — full 120-cell status matrix (4 sub-tabs × 5 categories × 6 timespans) with per-cell status. Cloud Reporting cells for non-Day views annotated `📝 placeholder` post-fix. Energy Flow / Power History / Device Telemetry sub-tabs marked `❓ unverified` (deferred audit; different UX models).

**Deliberately not shipped in this commit (queued as follow-up):**
- Root cause of Year/Month/Week zero returns — needs live capture of raw `/api/metrics/historical?type=4` responses per gateway config, compared against Day-view field shapes. May turn out to be a cloud-API-tier limitation, in which case a local `reporting_monthly_rollup` table populated by an APScheduler daily job becomes the fix.
- Playwright cell-by-cell sweep in CI — reasonable once the matrix stabilises.
- Persona-gating the Reporting surface (basic user gets 3-tile summary vs full matrix) — separate Phase B work per BD-04/#11.
- Audit of Energy Flow / Power History / Device Telemetry sub-tabs — different UX models; deferred.

Regression: 634 passed, 1 skipped, 0 failed.

### Rename cleanup — `amber` → `smart_dispatch` slug + Services/Pricing labels (GH [#14](https://github.com/david2069/franklinwh-ha-integrator/issues/14) + [#15](https://github.com/david2069/franklinwh-ha-integrator/issues/15) / BD-01 + BD-02)

Naming normalisation ahead of the public repo flip. Legacy `?tab=amber` continues to work forever (client-side alias); sidebar labels stop confusing users about what each tab actually is.

**Renames applied:**
- URL slug `?tab=amber` → `?tab=smart_dispatch` (canonical); old slug aliased in `app.js:_slugAliases` so external bookmarks + HA REST sensors keep working
- Sidebar label `"Services Setup"` → `"Services"` (URL slug stays `?tab=pricing` for backward compat — see "Deferred" below)
- Sidebar label `"Pricing Model"` → `"Pricing"` (URL slug stays `?tab=dynamic_pricing`)
- Tab titles + all body-copy guidance text updated to match new labels ("Services → Edit Utility Service", "Pricing tab")
- `MODULE_TAB_MAP` in `api_system.py` + `support_tab_v2.js` updated so `smart_dispatch` module maps to the canonical slug; legacy `"amber"` entry retained for DB backward compat
- SysAdmin tab-suppression checkboxes now use canonical slugs + updated labels

**Files touched:**
- `src/static/js/app.js` — `_slugAliases`, `_canonicalSlug()` normaliser used by both `setTab()` and `initRouting()`; `isTabSuppressed` MAP updated
- `src/templates/partials_v2/sidebar.html` — NAV_LABELS keys, dedicated Smart Dispatch button
- `src/templates/admin_v2.html` — `activeTab === 'amber'` → `'smart_dispatch'`
- `src/templates/tabs_v2/pricing.html` — H1 title + one guidance-text reference
- `src/templates/tabs_v2/dynamic_pricing.html` — H1 title + 2 "Services Setup → …" guidance references
- `src/templates/tabs_v2/support.html` — tab suppression checkboxes
- `src/routes/api_system.py` — `MODULE_TAB_MAP["smart_dispatch"]`
- `src/routes/api_pricing.py` — 2 docstring references
- `src/static/js/pricing_tab_v2.js` + `src/static/js/support_tab_v2.js` — comments + MODULE_TAB_MAP mirror

**Deferred (documented rationale):**
- **URL slug swap `?tab=pricing` ↔ `?tab=dynamic_pricing`** — the plan (`docs/backlog.md` BD-02) proposed swapping these so `?tab=pricing` becomes the canonical for the Pricing tab. Doing this would silently redirect existing external bookmarks from Services Setup → Pricing (semantic collision, not just a rename). Deferred to a coordinated major-version bump with prominent release notes. For now: sidebar labels are clean, URL slugs stay put.
- **Pricing provider live-test audit** — inspection confirms all 6 providers (amber, localvolts, aemo, comed, franklinwh_tou, flat_rate) have real implementations (test_connection + get_snapshot at minimum; Amber has 7 async methods incl. usage/site info). Runtime reliability per provider needs live testing — separate work.

Regression: 634 passed, 1 skipped, 0 failed. Existing `?tab=amber` bookmarks smoke-tested via `_canonicalSlug('amber') === 'smart_dispatch'` behaviour verified.

### Persona Phase B — Solar Setup gating (GH [#20](https://github.com/david2069/franklinwh-ha-integrator/issues/20) / BD-14)

First Phase B tab-gating landing. Proves the persona-plumbing round-trip end-to-end: server-side compute → features API → Alpine store → `isTabSuppressed()`.

- **`GET /api/system/features` returns `persona_gates: dict`** — new field. Server computes visibility from `persona.*` flags. `persona_gates.solar_setup` is `false` when detection has run AND no gateway has `persona.solar_present` AND no gateway has `persona.enphase_present`.
- **`isTabSuppressed(tab)` extended (`app.js`)** — checks `personaGates[tab] === false` before existing `enabledModules` / `suppressedTabs` logic. Missing key = permissive.
- **Permissive-until-detected bypass** — if `persona.detected_at` is null, `_compute_persona_gates()` returns all-True. Existing installs before the first-run silent detect (v0.6.0 startup hook) see the full UI. No silent tab loss on upgrade.
- **Enphase-only installs still show Solar Setup** — the gate checks BOTH `solar_present` AND `enphase_present` for each gateway. AC-coupled Enphase without built-in PV correctly shows the tab.
- **Override precedence carried through** — `persona.override.solar_present.{serial}=true` forces Solar Setup visible even when detection says no PV (installer testing / user with solar arriving next month).
- **5 new tests** in `tests/test_persona.py`:
  - `test_persona_gates_permissive_when_detection_not_run`
  - `test_persona_gates_hide_solar_setup_when_no_pv_anywhere`
  - `test_persona_gates_show_solar_setup_when_any_gateway_has_pv`
  - `test_persona_gates_show_solar_setup_when_only_enphase`
  - `test_persona_gates_solar_setup_override_respected`

**Not touched (intentional):**
- Sidebar template — no `{% if %}` wrap needed since `isTabSuppressed('solar_setup')` already gates the button via existing `x-show="!$store.app.isTabSuppressed(item.tab)"` pattern
- Enphase integration test pass (the other half of #20) — needs real hardware + Installer creds; separate work

Regression: 634 passed, 1 skipped, 0 failed.

---

## [0.6.0] - 2026-08-07

**Persona matrix Phase A — foundational plumbing (GH [#11](https://github.com/david2069/franklinwh-ha-integrator/issues/11) / BD-04).** Behaviour-neutral: no UI feature hidden that wasn't hidden before. Establishes the read-side API + admin surface every subsequent brain-dump issue (#14, #15, #17, #19, #20, #21, #22, #23 …) will consume when gating tabs/cards/rules in Phase B.

### Added
- **`src/services/persona.py`** — read-side helper API for the persona matrix. Public surface: `get_persona(gateway_serial)`, `is_grid_connected`, `is_solar_present`, `is_apower_s`, `is_pricing_available`, `is_grid_charging_permitted`, `is_australian`, `sd_engine_flags(gateway_serial)`, `has_override`, `set_override`, `clear_overrides`, `list_overrides`, `any_gateway_has`. Override precedence enforced via a single internal `_read_with_override` helper — override keys stored as `persona.override.{axis}` in `app_config`, always beat detected values.
- **`src/services/persona_detector.py`** — adapter that reads cloud client outputs and writes `persona.*` keys. Consumes `client.discover(tier=2)` (gateway type, `flags.solar`, `flags.mppt_enabled`, `accessories.has_ahub/has_mac1`, country) + `client.get_stats().current.grid_connection_state` (4-state `GridConnectionState` enum) + `client.get_tou_info(1)` (TOU wave variety). Grid mapping treats `OUTAGE` as transient (doesn't flip persona); `NOT_GRID_TIED` and `SIMULATED_OFF_GRID` both map to `off_grid`. Tariff detection is a three-signal decision (FHAI pricing_config + tariffSettingFlag + TOU wave variety).
- **`persona_detection_log` table** (schema v55) — audit trail for every detection run. Columns: `detected_at`, `gateway_serial`, `axis`, `value`, `source`, `confidence`, `previous_value`. Index on `detected_at DESC` for the "recent activity" view. Written on-change only (idempotent writes are silent).
- **`src/routes/api_persona.py`** — 5 endpoints:
  - `GET /api/persona` — snapshot (all gateways + global + active overrides)
  - `GET /api/persona/log?limit=N` — recent detection log entries
  - `POST /api/persona/detect` — force fresh detection (admin only)
  - `POST /api/persona/override` — set persona.override.{axis} (admin only, validated whitelist of axes)
  - `DELETE /api/persona/overrides` — clear all overrides (admin only)
- **First-run silent auto-detect** (`main.py` Stage 9b) — if `persona.detected_at` is null at lifespan startup, run detection once in a background task. Non-fatal; log outcome. Ensures existing installs get baseline persona flags without wizard interruption.
- **Weekly persona re-detect** — new APScheduler job `persona:weekly` fires Sun 03:30 site-local, re-runs `detect_and_persist(force=True)`. Silent + non-fatal. Handles gateway/tariff drift over time.
- **26-test unit + integration suite** (`tests/test_persona.py`) — covers grid state mapping (all 5 enum values incl. OUTAGE non-flip), tariff three-signal decision (all 4 outcomes), TOU wave extraction from attr and dict shapes, country ID → code, `detect_for_gateway` end-to-end with mocked client, override precedence, clear-all overrides, per-persona `sd_engine_flags` conservatism, `any_gateway_has` case-insensitive substring, global-only snapshot when serial is None.

### Design notes
- **All four axes read from cloud client outputs, not re-derived.** Prior plan draft (v1 at commit `9bc6581`) proposed bespoke detection cascades per axis; @david2069 caught that `franklinwh-cloud` v0.4.9 already curates every axis on `DeviceSnapshot` and `Stats.current`. v2 plan (`ef851ce`) rewrote the detector as a thin adapter. Detection code dropped from ~200 LOC to ~80 LOC of adapter.
- **`persona.*` config schema stored in existing `app_config` KV** — no new tables (except the audit log). Migrations trivial.
- **Persona axis semantics documented in `docs/persona_matrix_plan.md`** (v2). Reference for every follow-up issue that gates on `persona.*` flags.
- **Phase A = plumbing only.** No sidebar tabs hidden, no SD rules gated, no cards omitted. Per-tab gating happens issue-by-issue in Phase B (BD-11/#19, BD-14/#20, BD-15/#21, BD-20/#23, etc.) — each PR small and independently mergeable.

### Not yet
- **CLI `fhai persona *` commands** — deferred until BD-17/#12 lands the `fhai` binary entry point + `src/cli/` package.
- **SD engine rule gating on `sd_engine_flags`** — Phase B (per-rule wiring).
- **UI persona badge in topbar** — Phase B.
- **Setup Wizard integration** — Phase C, folded into GH #1.

---

## [0.5.6] - 2026-08-07

GitHub Issues scaffolding — second half of the pre-public-launch prep. Templates, labels, Discussions, and 10 pinned issues so the repo goes public with a populated tracker instead of an empty one.

### Added
- **`.github/ISSUE_TEMPLATE/`** — four templates driving structured issue submission:
  - `bug_report.yml` (form-based; requires version + environment + repro + logs, plus pre-flight checkboxes for de-duplication and log-redaction)
  - `feature_request.yml` (form-based; distinguishes problem vs proposal vs alternatives, contributor-availability dropdown)
  - `known_issue.md` (maintainer-facing, for publicly documenting workarounds)
  - `config.yml` — disables blank issues; redirects Q&A to Discussions and security reports to private security advisories per SECURITY.md
- **11-label taxonomy** via `gh label create`: `type:bug`, `type:feature`, `type:docs`, `type:known-issue`, `priority:P1/P2/P3`, `good-first-issue`, `help-wanted`, `status:wip`, `status:blocked`.
- **GitHub Discussions enabled** via `gh repo edit --enable-discussions`.
- **10 issues migrated** from `docs/backlog.md`:
  - **Pinned (3):** #1 Setup Wizard MVP, #2 Gateway / Cloud Diagnostics Dashboard, #3 True CLI Terminal + button refresh
  - **P1 defects (7):** #4 Mobile browser crash, #5 Schedule pricing dropped, #6 Control-tab telemetry sync, #7 SD dormant 14 days, #8 DISPATCH MODE pill mismatch, #9 INTERACTIVE radio not persisting, #10 actionable=0 global block

Each migrated issue links back to `docs/backlog.md` and notes that the symptom should be verified against the current release — several may have been silently fixed by later batches.

### Changed
- **`docs/backlog.md`** — added top-of-file callout listing the 10 migrated issues, pointing contributors at the Issues tab + Discussions, and clarifying that this file remains the internal archive for the ~80 P2/P3 items that didn't make the migration cut.

### Migration notes
- **Issue #4 (Mobile crash)** may be largely closable — R.1 + R.2 addressed the main symptoms. User confirmed "oh it works" in incognito on 2026-08-02. Left open pending explicit verification.
- **Issue #7 (SD dormant)** is likely resolved by v0.4.8 scheduler_liveness monitor. Left open for verification.

### Post-public-flip follow-ups (not blocking)
- Full docs refresh — some `docs/*.md` may be stale from Batches A through Q.
- Repo topics / metadata via GitHub UI (recommended: `home-assistant`, `franklinwh`, `mqtt`, `battery-storage`, `smart-dispatch`).
- Public announcement post (HA community forum, r/homeassistant, etc.).

---

## [0.5.5] - 2026-08-07

Pre-public-flip housekeeping. Repo is ready to switch from private to public — this release adds the standard OSS project scaffolding, tidies the root, and formally backlogs the three big remaining features (Setup Wizard, Diagnostics Dashboard, True CLI Terminal).

### Added
- **`LICENSE`** — MIT. Was missing entirely; without a license the default "all rights reserved" would have blocked contributions.
- **`SECURITY.md`** — vulnerability reporting policy pointing at GitHub private security advisories + email fallback. Scope + out-of-scope items enumerated.
- **`CONTRIBUTING.md`** — contributor guide: dev setup (Docker + local venv), test running, commit style (conventional commits), PR expectations, brief architecture pointers.
- **`.env.example`** — safe template of the env vars users actually need (`HA_HOST`, `HA_TOKEN`), with comments explaining each. The real `.env` remains gitignored and has never been in git history.

### Changed
- **`README.md`** — removed the `YOUR_ORG` placeholder in the clone URL, removed the reference to a "setup wizard" that doesn't exist yet (with a pointer to the backlog entry that will build it), added Smart Dispatch + LP-optimiser features to the list, added tests / CI credibility line, expanded Docs section to reference the local docs viewer (v0.5.4) and the new SECURITY / CONTRIBUTING files.
- **Root-level cleanup** — moved 7 top-level scratch scripts (`add_ha_api.py`, `check_admin.py`, `check_dp.py`, `check_extra_tags.py`, `check_tags.py`, `check_tags_lines.py`, `scratch.py`) into `scratch/` where they belong.
- **`.gitignore`** — added `scratch/*` (with a `!scratch/.gitkeep` negation) so dev-only files under `scratch/` never leak into git. Untracked 34 previously-tracked scratch files that shouldn't have been under version control.

### Backlogged (public-launch roadmap)
Three substantial features formally added to `docs/backlog.md`, called out during the pre-public fitness assessment:
- **Setup Wizard (P1 for public UX)** — minimum 3-step MVP for first-run onboarding. FEM (`franklinwh-energy-manager`, READ-ONLY sibling) has a comprehensive one to model against. 4-6 h MVP / 12-16 h for FEM parity.
- **Gateway / Cloud Diagnostics Dashboard (P2)** — consolidates scattered diagnostic surfaces (cloud poll health, stale-window drops, MQTT connectivity, HA reachability, scheduler_liveness) into one page. 4-6 h.
- **True CLI Terminal (P2)** — xterm.js emulator + backend PTY OR CodeMirror-based editor upgrade for the input line. Also refreshes the button set to match the current `franklinwh-cli` command surface. 6-8 h / 3-4 h.

---

## [0.5.4] - 2026-08-07

Docs served locally by FHAI — no more GitHub dependency for in-UI doc links (repo is private, working branches drift from main, HA add-on installs may have no internet at all).

### Added
- **`markdown>=3.5` dependency** — pure-Python, MIT-licensed, ~100 KB. Server-side rendering of `.md` → HTML.
- **`src/routes/api_docs.py`** — new FastAPI router mounted at `/docs`:
  - `GET /docs` — index page listing all markdown files under `docs/`.
  - `GET /docs/{name}` — reads `docs/{name}` from disk, renders to HTML via python-markdown with GFM-ish extensions (`fenced_code`, `tables`, `toc`, `sane_lists`, `nl2br`), wraps in a minimal dark-theme HTML template. Zero external assets — works in air-gapped installs.
  - Path-traversal safe: rejects any `{name}` containing `/`, `\`, or `..`; rejects non-`.md` extensions; `Path.resolve()` follow-up ensures the resolved path stays inside `DOCS_DIR`.
  - `ETag` cache header from file `mtime + size`. Browser gets a 304 on subsequent loads until the file changes.
  - Docs directory resolved from two candidates (repo-root sibling `../docs` for dev, `/app/docs` for container) — first existing wins.
- **`docs/` mounted read-only in `docker-compose.yml`** at `/app/docs:ro` so the container has access to the markdown files. Read-only — the app never writes to this dir.

### Changed
- **Engine Diagnostics "About the Decision Chain" links** now point at local `/docs/…` URLs instead of `github.com/…/blob/{branch}/…`. Links open in a new tab, work offline, work in the HA add-on ingress, work regardless of branch state or repo visibility.

### Verified
- `/docs` returns 200 with an index of 40 markdown files.
- `/docs/sd_decision_chain.md` returns 200 with styled HTML (title, breadcrumb, GFM tables, code blocks all rendered).
- `/docs/nonexistent.md` returns 404 with a clear detail message.
- `/docs/..%2Fetc%2Fpasswd` returns 404 (path-traversal guard).
- Container-side `ls /app/docs` shows the docs mount is live after `docker compose up`.

---

## [0.5.3] - 2026-08-07

### Changed
- **Added "How to read this page" summary panel at the top of Engine Diagnostics**. Plain-English 3-paragraph explainer answering what the page shows, how the precedence chain works ("your explicit rules always beat the optimiser"), and how to read the sections in order. Addresses the "no idea what this page is saying" feedback — the summary now sits above the Latest Decision card so first-time visitors get context before the data.

### Fixed
- **Doc links pointed at `main` branch which never received the files** — `docs/sd_decision_chain.md` and the §3.2 addition to `smart_dispatch_architecture.md` were only pushed on the `fix/ipad-webkit-crash-payload` working branch, so external `github.com/…/blob/main/…` URLs returned blank pages. Updated the two `<a href>` in the About panel to point at the working branch. This is a stopgap — after the branch merges to main the URLs should revert. Better long-term: serve docs locally via a FastAPI route so the app never depends on GitHub availability + branch state. Backlogged.
- **Icon lookup miss for `#info-circle`** in the summary panel — my initial pass used a symbol name that doesn't exist in either the inline dict or lucide.svg. Fixed to `#info` (which does exist in both).

---

## [0.5.2] - 2026-08-06

Phase 3.C — Engine Diagnostics view. Dedicated read-only visualisation of the SD decision precedence chain, LP output, engine config, and strategy matrix. Sits under Diagnostics in the sidebar.

### Added
- **`GET /api/smart_dispatch/decision_chain?gateway_id=...`** — read-only endpoint returning `winner`, `winner_step_id`, `lp_plan_first_slot`, `lp_plan_horizon`, `precedence_chain` (6 steps with role + wins_when descriptions), `engine_config`, `strategy_matrix`. All values sourced from tables the engine already writes to — no engine invocation, safe to poll.
- **`src/templates/tabs_v2/engine_diagnostics.html`** — new v2 tab under Diagnostics. Sections: Latest Decision card, Decision Precedence Chain (6-step ladder with the winning step highlighted), Optimiser Output (LP or greedy first-slot recommendation independent of who won), Engine Configuration table, Strategy Matrix rows, About collapsible with doc links. Alpine `engineDiagnostics()` component polls every 15 s.
- **Sidebar entry `Engine Diagnostics`** under Diagnostics section with `activity` icon. `engine_diag` tab key added to `NAV_LABELS`. Template wired through `admin_v2.html` `<template x-if>` block.
- **Docs:** new `docs/sd_decision_chain.md` — narrative walk-through of the 6-step chain, per-step semantics, downstream gates (Batch M/O/C/P/v0.4.13), Q&A. Also added §3.2 to `docs/smart_dispatch_architecture.md` cross-referencing the new doc + Engine Diagnostics view.

### Design notes
- **LP output is shown even when a matrix rule won.** The LP always runs (its output is an input to the chain, not just a step 4 candidate). Displaying it independently makes the "what would the optimiser have done" visible without changing decision behaviour — the exact answer to "does LP replace or complement my rules" (it complements; matrix rules always win over LP).
- **`winner_step_id` heuristic**: `amber_eval_log` has no `rule_id` column, so the step is inferred from `reason` prefix (`"Matrix match"` → step 1) and `trigger_category`/`rule_name` patterns. Falls back to `custom_automations` for anything unmatched.

### Verified
- Endpoint returns 200 with real data for gateway 24170091. `winner_step_id="strategy_mixer"`, `matrix_count=16`.
- Served `/admin?tab=engine_diag` HTML contains all four expected markers (`engineDiagnostics`, template include, "Engine Diagnostics" title).

---

## [0.5.1] - 2026-08-06

Phase 3.B — real LP dispatch planner. The v0.5.0 scaffold's delegate-to-greedy body is replaced with a MIP formulation using PuLP + bundled CBC.

### Added
- **Real LP model in `smart_dispatch/lp_optimizer.py::LPOptimizer._solve_lp`** — up to 48-slot horizon (24 h @ 30 min), decision vars per slot for `charge_kwh`, `dchg_kwh`, `soc_kwh`, plus binary `is_charging` for complementarity. Battery-balance recurrence, SoC min/max bounds, charge/discharge power caps, initial SoC seeded from live `soc_pct`. Objective: minimise total grid cost `Σ (charge*import_c - dchg*export_c)` in cents. Returns first-slot `(action, projected_soc_pct)` matching the greedy tuple contract.

### Fixed (during implementation)
- **Complementarity double-dip**. First-pass LP had no constraint preventing simultaneous charge + discharge in the same slot. On tariffs with `import_c_kwh < 0` (offpeak pay-you-to-consume windows) AND `export_c_kwh > 0`, the LP happily did both — economically profitable in math, physically impossible. Fix: added binary `is_charging[t] ∈ {0,1}` per slot with big-M linkage (`charge[t] ≤ M * is_charging[t]`, `dchg[t] ≤ M * (1 - is_charging[t])`). CBC solves the resulting MIP in <200 ms for a 48-slot horizon.

### Verified
- 7/7 new LP tests pass (charge-when-negative-and-soc-low, discharge-when-export-high, hold-when-flat, respects-min-soc, respects-max-soc, falls-back-on-empty-forecast, falls-back-when-soc-none).
- 602/602 full suite green on local Python 3.14.
- Live smoke on gateway 24170091 with `use_lp_optimizer=1`: `POST /api/smart_dispatch/eval/force` returned a real decision (HOLD / Max Price — Cease Charging) — no solver warnings.
- Fallback path: any pulp import failure, infeasible model, or timeout falls through to greedy via the outer `LPOptimizer.solve` wrapper with a WARNING log.

### Behavioural note
When the LP runs unconstrained across multiple slots with identical prices AND the battery has SoC headroom, the mathematically-optimal solution may include a discharge-then-charge cycle to net more revenue than pure charging. That's *correct* under pure price arbitrage; real Amber tariffs seldom have flat multi-slot windows so this rarely materialises in production. Documented in the module docstring.

---

## [0.5.0] - 2026-08-06

Phase 3.A of the SmartDispatch engine revamp — LP optimizer scaffold. Ships the dependency, the schema flag, and the routing dispatch ahead of the LP model body itself (v0.5.1) so the plumbing matures separately from the LP-formulation correctness debate. Rollback of Phase 3.B will be a one-file revert of `lp_optimizer.py` rather than a scheduler re-registration or config-flag rethink.

### Added
- **`pulp>=2.7.0` dependency** — pure-Python LP modeller + bundled CBC solver (~2 MB install). Only imported by `lp_optimizer.py` when a gateway opts in.
- **Schema v54 — `smart_dispatch_config.use_lp_optimizer`** INTEGER DEFAULT 0. Per-gateway opt-in for the LP dispatch planner. Default 0 preserves greedy behaviour for every gateway.
- **`smart_dispatch/lp_optimizer.py::LPOptimizer`** — conforms to the `PlanOptimizer` protocol (from v0.2.5). v0.5.0 body simply delegates to `engine.synthesize_and_optimize` (greedy) — same decision, just routed through the new class. Logs a per-gateway once-per-hour info notice so the intent is visible without spamming the ticker. Real LP body lands in v0.5.1.

### Changed
- **`MesoPlanner`** — wiring dispatch. When `cfg.get("use_lp_optimizer", 0)` is truthy, routes to `LPOptimizer.solve()`. Any exception in LP falls back to greedy and logs a WARNING — Meso must never block the engine.

### Verified
- v54 migration applied cleanly on the existing container database.
- `pulp` installed in-container (3.3.2 available).
- Live smoke: flipped `use_lp_optimizer=1` on gateway 24170091 → `POST /api/smart_dispatch/eval/force` returned a real decision (HOLD / Demand Charge Protection) with `LPOptimizer[24170091]: v0.5.0 scaffold — falling through to greedy (LP body ships in v0.5.1)` in the log.
- 72 SD tests pass; no behaviour change vs v0.4.15 for the fallback path.

### What v0.5.1 will bring (previewed here so users can plan)
- `pulp.LpProblem` with 30-min slots over 24 h horizon (48 slots)
- Decision vars per slot: `charge[t]`, `discharge[t]`, `soc[t]`
- Constraints: battery-balance recurrence, SoC min/max bounds, initial condition, charge/discharge power limits
- Objective: `minimize sum(charge[t]*import_price[t] - discharge[t]*export_price[t])`
- Returns the first-slot action + projected SoC (same tuple contract as greedy)
- Fixture-based comparison test: LP objective ≤ greedy objective in ≥90% of runs

---

## [0.4.15] - 2026-08-06

### Fixed
- **`test_stale_window_detector` locked in the pre-v0.4.14 strict behavior** — two tests (`test_partial_zero_not_dropped`, `test_non_standby_zero_not_dropped`) asserted that SoC=0 rows should slip through the old all-zeros signature when *any* runtime field was non-zero. My v0.4.14 broadening intentionally catches those via the new `soc-cliff` signature (soc 78.9% → 0% in a single 30 s tick is physically impossible). Updated both tests to assert the new intended behavior — that these cases ARE flagged via `soc-cliff` — matching v0.4.14's design intent.

### Verified
- Full suite: 595 pass / 1 skip / 0 fail on local Python 3.14.
- 12/12 stale-window tests pass.

---

## [0.4.14] - 2026-08-06

### Fixed
- **Batch I-3 — "SoC cliff-drop" stale-window signature**. The two existing signatures (`soc-threshold` and `mode-flip`) both required ALL four runtime fields zero before firing (`soc==0 AND battery_kw==0 AND grid_kw==0 AND run_status==Standby`). Any single non-zero field let the stale row through; `last_data` got overwritten with `soc=0`; the UI SoC widget blinked to 0% until the next real poll landed. v0.4.13 stopped the SD engine from firing notifications on this leak, but the UI symptom remained.

  Physical reality check that anchors the new signature: aPower's ~5 kW max discharge + 13.6 kWh capacity means a 5% drop (≈0.68 kWh) requires 8+ minutes of continuous max discharge. A poll interval is 30 s. Any transition from `_last_good_soc ≥ 5%` to `soc == 0` between two consecutive polls is physically impossible — it must be data corruption. The new signature `_by_soc_cliff` fires on that alone, independent of `battery_kw` / `grid_kw` / `run_status`.

  Fires as a third path alongside the existing two. Emitted log-sig now reports which triggered (`soc-threshold`, `mode-flip`, or `soc-cliff`) and includes the other zero-field readings so a diagnostic reader can tell why cliff-drop won when it did. The `_fhai_suspect_stale_window` payload marker was also loosened — previously said "soc==0 + all-zero" which is misleading once the cliff-drop signature catches non-zero rows.

### Verified
- 86 SD-relevant tests pass (dispatch strategy, dispatch revamp, gateway API, stale-action gate, optimizer + lockout).
- Live-verify path: on the next real stale-window drop where `battery_kw` or `grid_kw` isn't exactly zero, expect a `dropping poll [soc-cliff]` log line and no UI SoC blink to 0%.

---

## [0.4.13] - 2026-08-06

### Fixed
- **SD engine evaluated on `soc=0` sentinel from Batch I stale-window leaks**. Batch I's `_normalise_stats` zero-collapse detector uses a strict all-zeros signature (`soc==0 AND battery_kw==0 AND grid_kw==0 AND run_status==Standby`) — any one non-zero field lets the stale row through, `last_data` gets overwritten with `soc=0`, and SD then evaluated + fired notifications on garbage. Live audit evidence today: 2 of 4 SENT approvals arrived at the user with `SoC: 0%` in the payload while the battery was actually ~97%.

  Defence-in-depth fix at the SD engine choke point: `evaluate_and_log` now checks `soc_pct == 0.0` against the gateway's `_last_good_soc` (tracked by `gateway_service.context` — same field Batch I already uses for its own guard). If last known good ≥ 5%, the 0 is treated as a stale sentinel and the whole tick is skipped — no `amber_eval_log` write, no notification, no state churn. SoC does not drop 97 → 0 in a 30 s tick; if we see 0, it's data corruption.

  Doesn't tighten Batch I's upstream detector — that's still worth broadening separately, since a missed drop still means the UI SoC widget briefly shows 0%. But this stops the erroneous notifications from ever reaching the user.

### Verified
- 72 SD-relevant tests still pass.
- Live smoke: `POST /api/smart_dispatch/eval/force?gateway_id=24170091` returns a real decision (`HOLD`, `Demand Charge Protection`, `demand_window=True`) — the guard only triggers on the sentinel, not normal ticks.

---

## [0.4.12] - 2026-08-06

### Fixed
- **The 6 sprite symbols injected by v0.4.11 had the same nested `<svg>` bug that v0.4.6 fixed for the other 99.** When I extracted them from `lucide.svg` and wrapped them in a `<symbol>`, I preserved the original lucide `<svg>` wrapper — the same defect. The icons rendered anyway on the user's browser (probably because the specific parent-viewport dimensions happened to be compatible), but the sprite structure was still malformed. Fix: re-ran the v0.4.6 strip-nested-`<svg>` pass across the whole dictionary. Verified: 121 symbols total, 0 with nested `<svg>`.

---

## [0.4.11] - 2026-08-06

### Fixed
- **6 sidebar/table icons still invisible after v0.4.6+v0.4.7**. Sprite-structure repair and viewBox injection fixed 99% of icons, but 6 more (`sun-medium` = Solar Setup, `satellite-dish` = Home Automation, `shield-check`, `toggle-right`/`toggle-left` = Automations STATE toggle, `circle-check`) were **never in `svg_dictionary.html` in the first place** — they only existed in `lucide.svg`. The sidebar uses `<use href="#name"/>` (bare `#`) which resolves within the current document only, so any icon absent from the inline dict silently rendered nothing. Fix: extracted the missing 6 from `lucide.svg` and injected them into `svg_dictionary.html` with the same viewBox as their peers.

### Verified
- Served `/admin` HTML now contains all 6 previously-missing `<symbol>` blocks.
- Sidebar Solar Setup + Home Automation entries now show their icons.
- Automations tab STATE column toggle switch renders (green when enabled, muted grey when paused).

---

## [0.4.10] - 2026-08-06

### Fixed
- **`scheduler_liveness._restart_scheduler` didn't repoint `app_state["scheduler"]` at the newly-built engine**. Every route that reads user automations resolves via `state.get("scheduler")` — after a rebuild, that reference kept pointing at the dead engine (which had zero jobs), so `/api/scheduler/jobs` returned `[]` and the Automations tab rendered its empty state, even though the new engine was running fine and all 7 user jobs were still in `apscheduler_jobs`. This bit hard while v0.4.8's UTC-parse bug (fixed in v0.4.9) was making the liveness monitor thrash-restart every 2 min — the user's UI showed automations wiped after ~11 spurious restarts.

### Verified
- Container restart with v0.4.10: liveness `alive=true` after 90 s grace; no restart storm.
- `/api/scheduler/jobs` returns all 7 user automations intact.

---

## [0.4.9] - 2026-08-06

### Fixed
- **scheduler_liveness misread `amber_eval_log.ts` as local time** (regression in v0.4.8). The table stores naive UTC strings; `datetime.fromisoformat(iso_ts).timestamp()` interprets them as local, so on AEST (+10) the monitor thought every fresh eval was 10 h old and started scoring stalls. The two-consecutive-stall gate saved us from a spurious auto-restart. Fix: attach `timezone.utc` to the parsed value before computing age.

### Verified
- Post-fix `/api/health.scheduler_liveness.alive` transitions to `true` on the first check after the 90 s startup grace, and stays `true` under normal MicroTicker cadence.

---

## [0.4.8] - 2026-08-06

Scheduler durability — defence-in-depth against silent APScheduler death.

### Incident

At 2026-08-06 02:01:32 AEST, the AsyncIOScheduler's `wakeup` callback died with a `sqlite3.OperationalError("database is locked")` inside `SQLAlchemyJobStore.update_job`. Under uvloop, the wakeup exception silently kills the callback registration — the scheduler's `.running` flag stayed True and `.get_jobs()` still returned rows, but **no job fired for ~5 hours** until manual container restart. Zero external symptoms; the engine simply went silent.

### Added

- **`PRAGMA busy_timeout=5000` on the APScheduler SQLAlchemy engine** (`scheduler_core.py`). SQLite now waits up to 5 s for a competing writer to release the lock instead of raising immediately. This is the root fix — prevents the crash class.
- **`src/services/scheduler_liveness.py` — independent asyncio task on the uvicorn event loop.** Polls `MAX(ts) FROM amber_eval_log` every 60 s. If the last SmartDispatch evaluation is older than 120 s (2× MicroTicker interval + buffer) for two consecutive checks, stops + rebuilds + re-registers the AsyncIOScheduler in place. Requires two consecutive stall detections before firing to avoid false-positives on transient DB read hiccups. Never dies itself — every internal exception is caught and the monitor keeps looping.
- **`/api/health.scheduler_liveness`** — new field exposing `{alive, last_eval_at, last_check_at, last_restart_at, restart_count, consecutive_stalls}` so the outage is visible to external monitoring.

### Verified

- Container restarted with v0.4.8. Startup log confirms `scheduler_liveness: monitor started (check=60s, max_age=120s, grace=90s)`.
- MicroTicker fires every 30 s on gateway 24170091.
- `/api/health` returns the new `scheduler_liveness` block; populates from `null` to `alive: true` after the 90 s startup grace + first check.

---

## [0.4.7] - 2026-08-05

### Fixed
- **555 SVG icon containers were missing `viewBox` across 42 templates**. Repo-wide pattern: `<svg class="w-5 h-5" fill="none" stroke="currentColor" ...><use href="#name"/></svg>` — parent SVG lacked a `viewBox`, so the sprite's 24×24 native coordinate space wasn't mapped to the parent's 20-pixel pixel viewport. The referenced content rendered at native size, was clipped to the parent viewport, and appeared invisible. The one sidebar entry that DID render (Security) was the only one written with inline `<rect>`/`<path>` + a `viewBox`.

  Together with the v0.4.6 sprite structure repair, this restores every icon across the sidebar, topbar, tabs, dialogs, and inline UI markers. Fix was mechanical: for every `<svg ...><use .../></svg>` block missing a viewBox, inject `viewBox="0 0 24 24"`. 42 files touched, 555 SVG openings gained the attribute.

### Verified
- Served `/admin` HTML: 73 sprite-referencing SVGs, **0** still missing viewBox.
- No behaviour change on any non-UI subsystem.

---

## [0.4.6] - 2026-08-05

### Fixed
- **Sidebar (and many other UI) icons rendering invisible for months**. `src/templates/components/svg_dictionary.html` is inlined into every admin page as the local SVG sprite source. 99 of its 113 `<symbol>` blocks had raw lucide SVG files pasted inside them without stripping the outer `<svg>` wrapper — several had an unclosed `<svg>` opening tag with no `</svg>` close. Browser tolerated the malformed HTML but rendered each icon inside a nested 24×24 SVG viewport that couldn't scale to the parent `<use>` element in the sidebar (`class="w-4 h-4"` = 16 px). Net effect: every affected icon rendered but was clipped invisible.

  Fix: stripped both `<svg …>` opening and `</svg>` closing tags from inside every `<symbol>`. `<path>` children now sit directly inside `<symbol>` where SVG sprite semantics expect them. All 20 sidebar icons resolve to the freshly-repaired inline symbols on `<use href="#name"/>` lookup.

### Verified
- Served `/admin` HTML: 113 symbols, **0** still with nested `<svg>`.
- No behaviour change on any other subsystem — this was a template-only fix.

---

## [0.4.5] - 2026-08-05

### Fixed
- **Topbar "Runtime Mode" chip disappearing on every cloud poll flap**. The chip was gated on `poll_status ∈ {running, ok}` — so every time Batch I's stale-window detector dropped a cloud poll (~every 5 min), the chip vanished and the adjacent "Online" pill flipped to red "🔴 Comm Error" until the next successful poll landed. That flapping red dot was what the user was seeing in place of the operating mode. Fix: gate the chip on whether `activeMode` has data (i.e. we've ever seen a poll populate `currentGateway.runtime_mode`), not on live poll freshness. When the poll is stale the chip dims to 40 % opacity and the tooltip appends "(stale — poll not fresh)" so the reading being cached is visible without the chip disappearing outright.

### Verified
- Live smoke: post-restart, topbar renders "Self-Consumption" (or whichever cached mode was last received) with full opacity when polls are current; dims to 0.4 during transient stale-window drops instead of vanishing.

---

## [0.4.4] - 2026-08-05

Log noise cleanup exposed by the Phase 2.D MicroTicker cadence — pre-existing `StrategyMixer` conflict + shadow logs were 10× amplified by the switch from ~5-min ticks to 30-s ticks.

### Fixed
- **`StrategyMixer` rule-conflict WARNING re-emitted every 30 s** (`60 warnings / 10 min` observed on gateway 24170091). The conflict set is a function of the rulebook DB rows, not tick state — re-logging every fire was pure noise. Added a class-level dedup cache keyed by `(short_id, local_id, global_id, reason)` with a 1-hour re-emit interval. Class-level (not instance) because `SmartDispatchEngine` builds a fresh `StrategyMixer(...)` per evaluation call.
- **`StrategyMixer` shadow INFO log re-emitted every 30 s** (2 lines per fire on the same gateway). Same architectural cause; same fix — reuses the class-level dedup cache with a `("shadow", ...)` key prefix.

### Result
- 3 `Conflict detected` WARNINGs × 20/10 min → 3 × 1/hour = **99.7% log-volume reduction** for that channel.
- Shadow INFO lines similarly rate-limited.
- No engine behaviour change; only the log surface. The underlying rulebook conflicts still exist and still get resolved (gateway-specific wins).

### Verified
- 72 SD tests pass across `test_dispatch_strategy`, `test_dispatch_revamp`, `test_optimizer_and_lockout`, `test_stale_action_gate`.
- Post-restart live smoke: MicroTicker fires every 30 s on gateway 24170091, zero conflict warnings on subsequent ticks.

---

## [0.4.3] - 2026-08-05

Green-CI patch #3 — the last remaining CI failure was a hardcoded absolute path to the author's dev machine that had been silently making a system-security invariant no-op on any other host for months.

### Fixed
- **`check_and_trigger_session_reversion` returned False on every non-author host**. The function hardcoded `seed_path = Path("/Users/davidhona/dev/franklinwh-ha-integrator/db/seed/system_strategies_seed.json")` — if that exact path didn't exist (i.e. anywhere but the original author's laptop), the function returned False without reverting any modified system-immutable rules. On the running Docker container that path never existed either — meaning the "expired session → auto-revert modified safeguards" invariant was silently broken in production. Fix: resolve relative to `__file__` (same pattern used by `seed_system_strategies` at db.py:2803). This is the same class of bug that Batch S fixed in the lookahead layer.

### Verified
- 595 pass / 1 skip / 0 fail on Python 3.12 (CI target)
- 595 pass / 1 skip / 0 fail on Python 3.14 (local dev)
- `test_session_expiration_reversion` now passes in both isolation AND full-suite runs

---

## [0.4.2] - 2026-08-05

Green-CI patch #2 — chase every remaining test failure exposed after v0.4.1 unblocked the primary route-drop regression. **595 pass / 1 skip / 0 fail on both Python 3.12 (CI) and 3.14 (local).**

### Fixed
- **Test patch targets pointed at the moved `get_app_state`**. When `get_app_state` moved from `src.main` → `src.app_state` in v0.4.1, tests still patched `src.main.get_app_state`. Since consumers now import from `src.app_state` (module-level), or re-import lazily inside functions, the old patch path silently no-op'd. Updated ~10 patch sites across `test_auth.py`, `test_hold_notification_override.py`, `test_batch_l_lookahead_bridging.py`, `test_automation_weather_forecast.py`, `test_forecast_scheduled_soc.py`, `test_solar_location.py` to point at the correct lookup path (`src.app_state.get_app_state` for lazy consumers, `<consumer_module>.get_app_state` for module-level consumers).
- **Redundant lazy `from src.app_state import get_app_state` imports removed** across `api_automation.py`, `api_control.py`, `api_gateways.py`, `api_smart_dispatch.py`, `api_system.py`, `api_weather.py`, `api_solar.py`. The lazy re-imports bypassed test patches AND were dead code (module already had the import at the top). Standardised on a single module-level import per file.

### Verified
- 595/595 tests pass on Python 3.12 (CI target)
- 595/595 tests pass on Python 3.14 (local dev)
- Live container smoke: `/api/scheduler/jobs` 200, `/api/automation/rules` 200
- MicroTicker still fires every 30s on gateway 24170091 (no regression from Phase 2.D)

---

## [0.4.1] - 2026-08-05

Green-CI patch release. The GitHub Actions Release workflow had been failing on every push since v0.2.1 (2026-08-02) with `test_scaffold.py::test_admin_page` red — the root cause turned out to be TWO independent Python-3.12-only bugs that hid on my local 3.14 dev environment.

### Fixed
- **Circular import silently dropping ALL API routes on Python 3.12**. `src.main` batch-imports every route module at line 465; nine route modules did `from src.main import get_app_state` at module scope. On Python 3.12 with FastAPI 0.141 that trips `AttributeError: partially initialized module 'src.routes.api_X' has no attribute 'router'` mid-batch, so every subsequent `app.include_router()` runs against a broken module reference and drops routes silently. Only `/api/health` (defined via a top-level `@app.get` decorator after the include block) survived — hence `test_admin_page`'s `assert '/admin' in {'/api/health'}` failure. Fix: extract `app_state` + `get_app_state` into `src/app_state.py` (a leaf module with no dependencies), migrate all module-level `from src.main import get_app_state` to `from src.app_state import get_app_state`. Also updated the lazy-import sites for consistency (they weren't triggering the cycle but the mix was confusing).
- **`test_admin_page` route-enumeration incompatible with FastAPI ≥0.141**. FastAPI ≤0.135 flattens `include_router()` into `APIRoute` objects on `app.routes`; 0.141 wraps them as `_IncludedRouter` objects with the underlying routes reachable via `.original_router.routes`. The test filtered `isinstance(r, APIRoute)` and got 1 result on 3.12 (FastAPI 0.141) vs 302 on 3.14 (FastAPI 0.135). Fix: walk both shapes. `/admin` uses `include_in_schema=False` so OpenAPI can't be the source of truth.

### Verified
- Test suite on Python 3.12: 76/76 pass across `test_dispatch_strategy`, `test_dispatch_revamp`, `test_optimizer_and_lockout`, `test_stale_action_gate`, `test_scaffold`. `test_admin_page` now passes on both 3.12 and 3.14.
- No behaviour change on my running container (Python 3.12 in Docker) — routes still register, MicroTicker still fires every 30s on gateway 24170091, /api/scheduler/jobs still returns 200 with 7 user automations.

---

## [0.4.0] - 2026-08-05

Phase 2.D — MicroTicker (30s interval). Closes Phase 2 of the SmartDispatch engine revamp: all three temporal loops (Macro / Meso / Micro) are now first-class APScheduler jobs. Feature-flagged per gateway so rollout is instantly reversible; default OFF preserves prior behaviour for every gateway.

### Added
- **Schema v53 — `smart_dispatch_config.sd_use_micro_ticker`** (per-gateway INTEGER, DEFAULT 0). When 1: MicroTicker drives SD for that gateway on a 30s cadence; PricingService.tick skips SD invocation. Exactly one path fires per gateway.
- **`smart_dispatch.micro.MicroTicker`** class with `run_all()` + `_tick_for_gateway()`. Per-gateway 20s rate-limit backstop prevents overlapping fires if a single eval runs long. Reads the opt-in flag fresh each tick so a live toggle takes effect on the next fire — no restart.
- **APScheduler job `sd:micro:tick`** registered in `AutomationEngine.register_sd_jobs`. IntervalTrigger `seconds=30`. Coalesces, single-instance-at-a-time.

### Changed
- **`PricingService._fetch`** gained a per-gateway pre-check: if `sd_use_micro_ticker=1` for that gateway, skip the SD invocation with a `MicroTicker owns` log message.
- **Consistent short_id-keying across SD schedulers** — MicroTicker + MesoScheduler both iterate on `short_id` (the identifier used throughout `smart_dispatch_config`, the gateway registry, and `evaluate_and_log`). Fixed during 2.D live testing when the flag lookup with `full_serial` returned defaults.

### Verified
- **Baseline (flag=0 everywhere)**: MicroTicker fires every 30s but does no work — no gateway opted in, no per-gateway log noise. Original PricingService.tick path unaffected. `/api/scheduler/jobs` returns 200 with all 7 user automations.
- **After flag flip on gateway 24170091**: MicroTicker fires 20:17:04, 20:17:34, 20:18:04, 20:18:34, 20:19:04, 20:19:34 (every 30s exactly). Each fire runs full SD eval and produces `SmartDispatch [TICK] [24170091] action=HOLD rule='Demand Charge Protection' soc=69%`. Dedup gates prevent notification spam ("(same as prev — dedup)" on every tick).
- 83 SD-relevant tests pass (dispatch strategy, dispatch revamp, optimizer + lockout, stale-action gate, lookahead bridging).

### Phase 2 close
- ✅ 2.A — Macro Discovery daily @03:00 (v0.2.4)
- ✅ 2.B — MesoPlanner protocol + wrapper (v0.2.5)
- ✅ 2.C — Meso scheduler cron @ 6h boundaries (v0.3.0)
- ✅ 2.D — MicroTicker @30s, flag-gated per gateway (v0.4.0)

The three temporal loops are wired. Phase 3 (LP optimizer behind the seam) can drop into `PlanOptimizer.solve` without touching any of the scheduler wiring.

---

## [0.3.0] - 2026-08-05

Phase 2.C of the SmartDispatch engine revamp — Meso scheduler cron. Second-to-last stage of Phase 2. MINOR bump because scheduled Meso runs now happen independently of price ticks, which is a new engine behaviour (existing on-tick invocation preserved).

### Added
- **`smart_dispatch.meso.MesoScheduler`** class with `run_all()` + `_solve_for_gateway()` static methods. Enumerates registered gateways, per-gateway resolves pricing service → snapshot → SoC → cfg → `meso_planner.solve(engine, ..., force=True)`. Never raises — Meso must not block the scheduler even if one gateway is misconfigured.
- **APScheduler cron job `sd:meso:cron`** registered in `AutomationEngine.register_sd_jobs()`. Fires at 00:05 / 06:05 / 12:05 / 18:05 site-local. `force=True` bypasses the greedy heuristic's own 6h cadence controller so each scheduled fire always re-optimises. Coalesces missed fires, single-instance-at-a-time.

### Fixed
- **Regression from Phase 2.A**: `/api/scheduler/jobs` iterated ALL APScheduler jobs and validated each against `RuleResponse`. The new `sd:macro:daily` job (no user-automation kwargs) tripped `ResponseValidationError` → 500 → the Automations tab rendered its empty state even though 7 user rules were intact. Fix: skip `sd:*` namespaced jobs in `list_jobs` — they're system-owned, not user-editable.

### Verified
- 72 SD-relevant tests pass (dispatch strategy, dispatch revamp, optimizer + lockout, stale-action gate).
- Startup log shows both `sd:macro:daily` and `sd:meso:cron` added.
- `apscheduler_jobs` table: `sd:macro:daily` next_run = 2026-08-06 03:00 AEST; `sd:meso:cron` next_run = 2026-08-06 00:05 AEST.
- `/api/scheduler/jobs` returns 200 with all 7 user automations intact.
- Existing on-tick Meso invocation (via `evaluate_rules` → `meso_planner.solve`) continues to fire — `sd_forecast_history` gained fresh rows at 20:07:59, 20:08:02, 20:08:03 during the smoke test.

---

## [0.2.5] - 2026-08-05

Phase 2.B of the SmartDispatch engine revamp — MesoPlanner seam. Zero behaviour change; the 541-line greedy heuristic body stays put and gets adapted to a `PlanOptimizer` protocol so Phase 3's LP solver can drop in without touching `evaluate_rules`.

### Added
- **`smart_dispatch/meso.py`** — new module with:
  - `PlanOptimizer` (`typing.Protocol`) — the interface a Meso optimizer must satisfy: `async def solve(engine, gateway_id, snap, soc_pct, cfg, force) -> (action, target_soc)`. Matches the existing `synthesize_and_optimize` tuple return for a minimally-intrusive seam.
  - `GreedyOptimizer` — thin adapter that delegates to `engine.synthesize_and_optimize`. Kept as an adapter (not an extraction) to keep the diff small.
  - `MesoPlanner` — orchestrator that selects an optimizer. Reads `smart_dispatch_config.use_lp_optimizer` lazily so a Phase 3 live toggle takes effect without a restart.
  - `meso_planner` — module-level singleton.

### Changed
- **`SmartDispatchEngine.evaluate_rules` — routes optimization through `MesoPlanner.solve()`** instead of calling `self.synthesize_and_optimize` directly. The direct method stays intact for `tests/test_optimizer_and_lockout.py`, which calls it as the unit-under-test.

### Verified
- 106/106 SD tests pass, including the two `test_optimizer_*` integration tests that exercise `evaluate_rules` end-to-end (they route through the new planner path unchanged).
- Live `POST /api/smart_dispatch/eval/force?gateway_id=24170091` returns a real decision — Meso → Greedy → `synthesize_and_optimize` chain confirmed.

---

## [0.2.4] - 2026-08-05

Phase 2.A of the SmartDispatch engine revamp — the Macro loop lands as the first of the three temporal loops (Macro / Meso / Micro). Non-actionable; feeds the future Meso planner.

### Added
- **Schema v52 — `sd_macro_snapshot`** table with `(gateway_serial, generated_at, snapshot_json)` and a lookup index on `(gateway_serial, generated_at DESC)`.
- **`smart_dispatch/macro.py` — `MacroDiscovery`** class with `run(gateway_serial)` and `run_all()` static methods. Captures slow-changing per-gateway facts (DNA + utility service tariff + SD config thresholds) once per day so Meso/Micro don't re-fetch them every tick.
- **APScheduler cron job `sd:macro:daily`** registered in `AutomationEngine.register_sd_jobs()`, fires daily @03:00 site-local. Called from `main.py` after `automation_engine.start()`. Coalesces missed fires, single-instance-at-a-time.
- **DB helpers**: `db.save_macro_snapshot`, `db.get_latest_macro_snapshot`, `db.prune_macro_snapshots(keep_last_n=30)`.

### Verified
- v52 migration applies idempotently on the existing container database.
- `MacroDiscovery.run_all()` manually invoked writes a full snapshot for gateway `24170091` (DNA, utility service, sd_config all populated).
- APScheduler picks up the job at startup — log confirms `Added job "SmartDispatch Macro Discovery (daily)" to job store "default"`.

---

## [0.2.3] - 2026-08-05

Phase 1 of the SmartDispatch engine revamp — package split. Zero behaviour change; every rule decision produces the same output as v0.2.2 given the same input. The 4325-line `smart_dispatch.py` god file becomes a package with focused, testable modules.

### Changed
- **SmartDispatch package split** — `smart_dispatch/__init__.py` (was 4325 → now 2380 lines) plus five new focused modules:
  - `smart_dispatch/models.py` (71 lines) — `RuleResult`, `EvalDecision` dataclasses.
  - `smart_dispatch/loads.py` (473 lines) — `get_site_season`, `resolve_site_season_for_gateway`, `build_home_loads_context`, `is_schedule_active`, `is_heating_cooling_load`, `get_active_forecast_load_kw` and their private helpers.
  - `smart_dispatch/context.py` (282 lines) — `_build_context` (the 220-line god function) plus `_build_evaluated_params`.
  - `smart_dispatch/evaluator.py` (1005 lines) — condition DSL (`evaluate_condition`, `_extract_field`, `_eval_single`), all eight `_eval_*` category evaluators, `_call_ha_service`, `_calc_dispatch_payload`, and the `StrategyMixer` class.
  - `smart_dispatch/lookahead.py` (228 lines, shipped in v0.2.2 as the Phase 1 pilot) — T-30min lookahead scanner.
- All 15 external import sites (`from src.services.smart_dispatch import ...`) keep working unchanged via re-exports from `__init__.py`.

### Fixed
- **Latent `_build_context` NameError in `StrategyMixer.evaluate`** — the extraction surfaced this: the evaluator module lacked a `_build_context` import that the god-file version had via lexical scope. Test suite missed it (mixer tests don't exercise the full pipeline); live smoke against `POST /api/smart_dispatch/eval/force` caught it before commit.

### Verified
- 595 pass / 1 skip / 0 fail across the full pytest suite.
- Live `POST /api/smart_dispatch/eval/force?gateway_id=24170091` returns a real decision (`HOLD` / `matrix_7` / Max Price rule) — StrategyMixer + context + evaluator all wired correctly through the package boundary.

---

## [0.2.2] - 2026-08-02

Phase 0 of the SmartDispatch engine revamp — persistence + dead-code cleanup ahead of the structural refactor.

### Added
- **Batch S — persistent lookahead notification dedup** (schema v51): New `sd_lookahead_dedup(gateway_serial, kind, hour_bucket, sent_at, created_at)` table plus `db.get_lookahead_sent_at` / `mark_lookahead_sent` / `prune_lookahead_sent` helpers. The engine's `_lookahead_sent` and `_lookahead_last_check` in-memory dicts are gone; container restarts no longer replay lookahead pushes for windows the pre-restart process already sent.

### Fixed
- **"Cheap Charging" refire at container startup** — two `Cheap Price - Force Charge?` notifications used to fire ~1s after every restart for adjacent hour-buckets (12.95¢/13:00 UTC + 13.4¢/13:05 UTC), because dedup state lived only in memory. Verified fixed end-to-end: injecting pre-restart dedup rows blocks all post-restart refires.
- **UnboundLocalError in `evaluate_and_log`** — a nested `import asyncio` inside two smart_dispatch.py functions shadowed the module-level import, breaking any earlier `asyncio.ensure_future(...)` call in the same function. `POST /api/smart_dispatch/eval/force` was returning `"cannot access local variable 'asyncio'"` before this fix.
- **Duplicate `_request_approval` method on `SmartDispatchEngine`** — two definitions existed on the same class (the first with a `decision_hash` signature and an HA-unconfigured early-exit, the second with a `request_id` signature and the actual send). Python's later-def-wins meant the first was dead code. Removed; the surviving impl still surfaces HA-unconfigured via the `__approval_send_failed__` log path.

### Changed
- Schema advanced from v50 → v51 (`sd_lookahead_dedup` created idempotently).

---

## [0.2.1] - 2026-08-02

Major reliability, notification-quality, and observability release: Smart Dispatch grows a full pre-emit gate stack (event-stale, action-stale, lookahead, VPP ownership deference), the UI gets Batch R iPad-crash mitigations, the Terminal tab is unblocked via a subprocess shim, and 601 commits advance the schema from v45 to v50.

### Added (Batches A–Q, R.1, R.2, Terminal shim — 2026-08-02 highlights)
- **Batch A — Smart Dispatch Notifications defect cluster**: Audit Log now renders 50 rows (was blank), test-originated actionable callbacks short-circuit cleanly, and radio/checkbox clicks auto-save in Preferences.
- **Batch C — Notification cooldowns**: Cooldown-after-ignore dedup on pending TTL expiry, plus category-map gap closure and removal of a dead custom rule.
- **Batch E/F — Smart Dispatch tab UX**: Auto-refresh OFF by default with opt-in toggle (visibility-gated), state-aware countdown label, per-card visibility persistence, and premium collapsible cards across Overview / Config / Solar Setup / Forecast Loads with global expand-all controls.
- **Batch G — VPP notification suppression**: HEMS notifications now suppressed while VPP owns the gateway.
- **Batch H — runtime_mode hysteresis**: N-consecutive-poll hysteresis filter on `runtime_mode` to filter racy cloud API flips.
- **Batch I — Stale-window cloud poll drops**: Drop cloud stale-window polls (measured 16% garbage-rate at source).
- **Batch J — Stale-state action suppression**: Suppress notifications for stale-state recommendations the battery is already executing.
- **Batch K — Gateway network endpoint**: Dedicated `/gateways/{short_id}/network` read endpoint with future-write stub.
- **Batch L — Lookahead pending + callback diagnostics**: T-30min pre-emptive lookahead notifications with per-pending callback 404/410 diagnostics.
- **Batch M — Event-stale + threshold + tick collapse**: Event-stale gate at the gateway edge, tunable low-SoC threshold, and per-tick collapse to prevent duplicate emissions.
- **Batch N — Log quiet + brief audit details** (schema v49): Notification log quieted with concise, structured audit rows; N.2 adds an inline UI expander with full per-decision details.
- **Batch O — Event-stale broadening + CLI + Diagnostics UI**: Broader event-stale coverage, a `franklinwh-cli` audit tool, and a new Notifications Diagnostics UI tab.
- **Batch P — SD ownership deference** (schema v50): `VPP-SHADOW` shadow_reason capture, formal deference to whoever owns the gateway (Bridge / Modbus / third-party VPP).
- **Batch Q — Power History timeline (Reporting tab)**: Bridge-inspired Battery Timeline panel surfacing per-poll runtime_mode/SoC/flow rows for post-hoc audit.
- **Smart Dispatch — Automation Builder integration (Phases 1–4)**: `sd_signal` bridge (SD → DB → AB `extra_ctx`), pricing/dispatch metrics in the AB metric picker, RHS metric references and value/lookup pill control, Smart Dispatch Delegate Actions in the execution pipeline, unified precedence pipeline with custom-automation overrides, and a Shadowed Precedence Rules visualization.
- **Smart Dispatch — Rolling 24h forecast timeline**: Premium vertical timeline breakdown with LFP weather warning inputs header, reserve-SoC telemetry analysis, and scrollable card list.
- **Smart Dispatch — HEMS Phase A–C**: Offline tariffs schemas, decoupled load registry + CRUD REST API, physical grid topology modeling, off-grid dynamic MAC suppression, three-phase vector summation + SOC coordination, forecast loads Home Assistant entity integration (power/energy/switch/binary), SOC trajectory visualisations, safeguard lockout override UI + manual locking, and WAL transaction hardening.
- **Smart Dispatch — Home Loads catalog**: `home_loads.dispatch_categories` / `categories` distinct-list aggregates feeding the AB metric picker, slug column with backfill migration, `GET /api/automation/home-load-fields` catalog endpoint, HA-reported `unit_of_measurement` normalised to canonical kW/kWh, and per-entity live state + season payload cards on the Home Loads tab.
- **Smart Dispatch — Strategy Mixer**: Enhanced Strategy Mixer cards with collapsing depth, add-strategy workflow, grid topology mapping, gateway assignment dropdown, and paused-strategy audit trail.
- **Dynamic Pricing — Provider subsystem**: Full multi-tenant Utility Services model, generic Smart presets, one-click "Use for Gateway", Pricing Model rename, LocalVolts 24h synthetic emulation-mode forecast, AEMO NEM spot-price adapter, and side-by-side buy/sell rates with dual-column forecast list.
- **Weather — Provider integration**: Staggered forecasting, telemetry normalisation, parameter selects UI, RainViewer radar with dynamic map theme toggle, clouds layer toggle, barometric pressure fixes, and light/dark legend contrast fixes.
- **Battery — BMS telemetry hardening (Sprint A/B/C)**: Battery Charts sub-tab, 16-cell layout, cell dot glow animation, firmware-always-present, single-row BATT INFO, and Electrical Summary top-alignment.
- **Control tab — Groups 1+2 and Phase A–C**: Typography and shading pass, Live Button and Countdown Timer, Quick Access panel, Duration slider, Dispatch SOC inputs, Battery Status SOC limit editors with DB persistence, and Reserved SOC editor showing both Self and TOU sliders.
- **Schedule tab — BKL-SCHED-01 Phases 1–3**: Multi-season TOU data-loss fix, Season & Day-Type editor UI with bold tabs + NOW EDITING banner, Usage Type + Tariff Setup + CLI Table View, editable buy/sell rates in the pricing modal, dirty-state + status badge + dual timeline, preset gap-fill and unverified flag, and Validate / Save-Load-Delete Preset / Refresh modals.
- **UI Density Migration — Phases 1–5**: V2 route promotion, SVG icon extraction, design-system.css injection, dynamic Tailwind gray palette, Lucide icon replacement app-wide, colour token migration (Blocks 4–5), dialog button parity (Block 3), FA icon elimination (Block 2), and a permanent UI density migration ledger.
- **MQTT — FEM parity Sprints 3 / 4 / 4b**: Entity parity expansion 63 → 82 → 89 registry (77 → 84 published), 12 P1 MQTT entity parity defects fixed, `short_id` migration (Phase C), and `mqtt_published` reset on stop/disable/reconnect.
- **Home Automation — Live HA entity browser tab** with slug-based catalog and Alpine-reactive live values.
- **CloudFront PoP world map (API Metrics tab)**: Leaflet inline map with PoP transitions table, full-screen modal, frosted-glass overlay, world-wrap + memory optimisations, and dominant PoP badge.
- **SysAdmin System Controls tab**: Poll Interval + API Rate Limiter Guard, relocated under SysAdmin → System.
- **Off-grid / islanding**: State-aware islanding card and modal for on-grid/off-grid directions with safety guards.
- **First-run experience**: Welcome screen split-view, setup wizard customisation banners for headless + telemetry, gradient login backdrop.
- **Snapshot / restore / safe-upgrade pipelines** in `manage.sh` with hardened Dockerfile VOLUME.
- **Device Registry**: FranklinWH Device Registry with Tier-A static device data persisted once to `profile_json`.
- **Health tab — 4 new diagnostic sections** for FEM parity.
- **Terminal — Enphase DPEL / CLI options**: Left-aligned output and expanded CLI option set.

### Fixed (Batches A–Q, R.1, R.2, Terminal shim — 2026-08-02 highlights)
- **Terminal — ResolvedCapabilities shim** (2026-08-02, `6e49af0`): Inject `sitecustomize.py` via `PYTHONPATH` into the `franklinwh-cli` subprocess so the Terminal tab no longer crashes on ResolvedCapabilities import.
- **Batch R.1 — iPad WebKit crash (unmanaged timers)**: Captured 6 unmanaged `setInterval` timers and capped `_bmsNotifSeen` to prevent unbounded growth (iPad crash root cause #1).
- **Batch R.2 — iPad WebKit crash (tab wrappers)**: Lazy-mount all 23 tab wrappers via Alpine `x-if` — single biggest win against the iPad crash payload; combines with Phase 1+2 elimination of 812 KB dead-weight SVG, script deferral, and vendored Leaflet/Mermaid.
- **Smart Dispatch — HOLD tap-hint**: No longer says "Skip to cancel"; DISPATCH MODE pill divergence corrected by scoping UI reads/writes to gateway; rule pipeline unfrozen for `auto` / `user_approval` strategy modes; actionable buttons auto-enabled when `strategy_mode=user_approval`.
- **Smart Dispatch — noise reduction**: Enphase on-demand only, banner anti-flap, cloud poll-error coalesce; SD Config Save 422 fixed (accept `rampTime=99` sentinel + 8 missing fields); forecast endpoint 60s+ hang fixed via 10min cache + on-demand only; SD+solar 15s → 60s eval refresh, dead poll loop removed; SD Live Pricing widget shows real values.
- **PII leak — `franklinwh_cloud` logger** forced to DEBUG when app NOT in DEBUG mode; three dev-leftover INFO logs dropped from `_live_soc`.
- **Actionable notifications — webhook**: Exempted from auth; user-override callback clears pending approvals, sets a 120-min NONE intent lock, and dispatches `RESUME_NATIVE`.
- **Actionable notifications — HOLD flow**: Manual Hold/Override option added to diagnostics dropdown; `notification_mode` and `strategy_mode` synchronised via transparent auto-healing.
- **Weather — provider save + Alpine crash**: Resolved provider-saving defect and Alpine `TypeError`s; weather-evaluate fix; OWM API Key now persists.
- **Dynamic Pricing — Save Config / Test Connection stuck disabled**: Alpine `undefined → truthy` gotcha fixed; activate-provider was silently dropping `emulation_mode` + creds; JS cache bumped (v=7 → v=8); defensive guards for handler false-alarms.
- **Smart Dispatch v4/v5 rebuild**: Flat Alpine.js architecture (no root wrapper), self-contained sub-tab state, sticky save bar in Global Defaults scope, guarded `selectedGateway.slice(-4)` null crashes, all `currentGateway` null crashes patched, orphan `</div>` removed, sub-tabs restored, and cache bumped to v=15.
- **Automation Builder — condition/action cards** aligned with controls on the right, `expand_placeholders` now resolves arbitrarily-deep dotted paths, `battery.status` resolves to `run_status_desc` string not raw int, unnamed-gateway bug in evaluate logic fixed.
- **Storm Hedge 400 error** — mandatory `advanceTime` parameter now supplied.
- **Battery Reserve SOC / Backup blindspot** and battery dispatch card light-mode contrast (all 4 Battery tab defects).
- **Amber Engine** — SDK type guards + browser cache invalidation, engine mode toggle 404 + export penalty descriptor bug, verbose logging toggle + change-detection demoted to DEBUG, WCAG-AA contrast across all badges/chips.
- **Gateway — startup name guard**: Always persist cloud name when DB holds generic value; `get_grid_profile_info` interim context guard; grid compliance profile seed + TOU poll cadence reduction.
- **DEF-GRID-STATE-ENUM / DEF-GRID-STATUS-SEMANTIC / DEF-ELECTRICAL-METRICS-LAYER** — consume library fixes; `GridConnectionState` enum consumed from library.
- **Backup pruning** — replace blind prune scan with `TELEMETRY_TABLES` whitelist; skip startup backup if recent archive exists (fixes 4-minute startup block).
- **Sankey diagram** — exclude MQTT daemon publishes from API flow diagram.
- **CloudFront PoP map** — 400 tile errors, popup animation race, blank-tile / world-wrap, `fitBounds` / `setView` disabled to kill popup race, PoP-map 7d/30d rendering.
- **Setup Wizard — greenfield 500 error** on empty registry (welcome branch); PIN code popup default to false, sidebar reverted to Smart Dispatch.
- **`dispatch_action` handlers, PCS anti-snapback cache**, and emergency-backup flag injections retained from 0.1.18 series with additional fixes.
- **Docker seeding warnings** — resolved via `__file__`-relative strategies path and `db` volume mount in Dockerfile / docker-compose.yml.
- **HA Ingress compatibility** — relative paths restored; chart.js sourceMappingURL suppressed; dynamic tailwind warnings silenced.
- **Actionable notifications — `.dockerignore`** added to fix slow Docker builds.
- **Security** — actionable notifications webhook exempt from auth; password double-encryption on key regeneration prevented; deprecated rulebook seeder cleanly stubbed.
- **BL-010** — legacy config data migration fallback prevents `NOT NULL` constraint aborts.
- **Pricing tab** — resolved 3 Alpine crashes (`_x_dataStack` bare text node, malformed array negations, invalid `x-if` on div, null `editingService` on `demand_charge_kw`).
- **Dashboard — VPP mode detection**, per-card refresh, font normalisation, and gateway-card / TOU-strip layout unification.
- **Device Registry — Edit Model HW** silently dropped 13 electrical fields — restored.
- **API Models** — accessory Edit UX, sticky actions, dispatch status label; 6 SysAdmin/services UI defects.

### Changed (across the release)
- **Schema advanced from v45 to v50** across this range — includes Amber Smart Dispatch generalisation (v9), audit-log details (v49), and SD ownership deference / VPP-SHADOW (v50). All migrations idempotent and in-place.
- **Smart Dispatch — decoupled from direct hardware execution**: Removed all direct `stop` calls; SD now emits `sd_signal` and delegates through the automation-builder precedence pipeline. Restores `set_operating_mode` as the sole MQTT push trigger; SC→TOU mode cycle forced to trigger gateway schedule re-poll.
- **Amber Engine → Smart Dispatch** — full rename and schema v9 migration; Smart Dispatch label replaces provider-specific "Amber" in AB metric dropdowns and elsewhere.
- **Metrics tab dual-view revamp** — historical Cloud reporting; sankey diagram replaced with dual grid-method tables; persistent API-library metrics history; timeframe UX enhanced; `calls_by_python_method` integrated from `franklinwh-cloud` v0.5.
- **`franklinwh-cloud` bumped v0.4.6 → v0.4.7** and DEFAULT_CACHE adopted; `get_accessories_power_info` slow-polled every 10 ticks (~5 min); `franklinwh-cloud` v0.5 `calls_by_python_method` consumed.
- **HA Integration Hub** consolidated — token entered once in Settings, not per-tab; sidebar label `FranklinWH` → `FranklinWH HA` retained.
- **V2 UI route promoted** to default; legacy V1 preserved at `/admin/v1`.
- **Global toast auto-dismiss system** unified across all tabs; silence auto-toasts during local memory adjustments in SD mixer.
- **Icons app-wide** migrated from Material Symbols / FontAwesome to Lucide SVG.
- **Simulation event trace engine removed** (superseded by Automation Builder).
- **`Shadow Mode (Dry-Run)` renamed to `Dry-Run Mode (Simulation)`** to resolve naming clash.
- **Terminal / CLI** — left-aligned output; expanded CLI options.

### Added (earlier work in this release — original Unreleased content)
- **Enphase Cloud Token Fallback**: Programmatic fallback flow (`login.json` -> `session_id` -> `/tokens` -> JWT) for Enphase Standard Owner accounts to fetch JWT tokens directly from Enphase Cloud.
- **Diagnostics Logging**: Added step-by-step diagnostic logging for Enphase config token fetch and connection test endpoints, resolving visibility issues.
- **Docker Seeding Warn Fixes**: Cleaned up startup seeding by resolving the system strategies path relative to `__file__` and mapping the `db` directory in `Dockerfile` and `docker-compose.yml`.
- **Automations Tab Customization & Documentation**:
  - Relocated the "Control" action column to the immediate right of the "State" column.
  - Implemented dynamic column visibility dropdown, allowing Rule Name, Tags, Logic, and Action Payload columns to be shown or hidden reactively. Persists choices in `localStorage` under `fwh_rules_columns`.
  - Added new predefined cron schedule intervals (5, 10, 15, and 30 minutes) and custom cron options.
  - Implemented real-time client-side cron expression validation and translated expressions to human-friendly text.
  - Created [automation_builder.md](file:///Users/davidhona/dev/franklinwh-ha-integrator/docs/automation_builder.md) documenting variables, telemetry data, and actionable notification responses available inside the Automation Builder.
- **HEMS 24HR Forecast Premium Extended Panel**:
  - Built a premium **Inputs Acknowledged Header Panel** directly above the timeline chart, outlining Active Operating Modes, SOC boundaries, home load & solar forecasts, and weather-based low-temp Snowflake / high-temp Fire derating notices.
  - Revamped the timeline period card stack with a bounded scrollable list (`max-h-[380px] overflow-y-auto`) to keep layout space compact on smaller screens.
  - Implemented tier-two columns detailing Default TOU Dispatch modes, expected solar generation average kW, and weather / temp ranges.
  - Refactored visual card elements to high-contrast standard base styles (`bg-[var(--surface-2)] border-[var(--border)]`) with theme-reactive left-border colored accents for perfect legibility in both light and dark modes.
- **Permanent Design Documentation**:
  - Saved the detailed rolling timeline extended design as permanent documentation in `docs/smart_dispatch_rolling_timeline_extended_implementation_plan.md`.
- **HA Credentials Consolidation (Single Source of Truth)**: Established the `Home Automation` ➔ `Configuration` panel as the absolute single source of truth for core Home Assistant authentication. Completely removed duplicate Host/Token input fields from the `Smart Dispatch Notifications` connectivity sub-tab to prevent configuration drift.
- **Animated E2E Pipeline Diagram**: Rendered a premium, CSS-animated integration path in the Connectivity tab detailing how actionable signals travel out-of-band: `FHAI` ➔ `HA` ➔ `Companion App` ➔ `Event Callback Webhook` ➔ `FHAI`.
- **Unified Pipeline Diagnostic Console**: Added an interactive test action simulation console in the Connectivity tab, enabling users to choose a recommendation event type, trigger test push alerts, and monitor out-of-band callbacks in a monospaced terminal console.
- **Companion App Alignment Warning**: Integrated a prominent visual alert block in the Connectivity tab to warn users that their companion app must actively connect to the matching HA instance generating the notifications.
- **Compact Connection Preview Stubs**: Embedded compact status widgets in both `Dynamic Pricing` and `Smart Dispatch Notifications` tabs for real-time connection checks and direct tab navigation shortcuts.
- **Smart Dispatch HOLD Actionable approvals**: Allowed recommended `HOLD` decisions to request user approval and trigger HA actionable notifications when strategy is set to `user_approval`.

### Fixed
- **VPP Mode Automation Builder Fix**:
  - Overrode `run_status` to `9` and `run_status_desc` to `"VPP mode"` in `gateway_service.py` during VPP Mode execution.
  - Resolved `battery.run_status_int` and `battery.status` logic condition evaluation failures under VPP Mode dispatch.
  - Sourced comprehensive unit tests in `tests/test_vpp_run_status.py` verifying state mapping and normalization rules.
- **URL Pathing in Logs**: Resolved a toast notification logs failure by upgrading logs request pathing from simple string concatenation to robust relative `new URL()` constructors, correcting the missing `/` port-path separator.
- **Notification Settings API Write-isolation**: Refactored `/api/automation/notifications/settings` endpoint in `src/routes/api_automation.py` to stop writing `ha_host` and `ha_token` to `app_config` from the Notifications tab, maintaining single-source identity.
- **Configure Decisioning navigation shortcut**: Added a toolbar button next to "Add Strategy" in the Strategy Priority Matrix targeting the Decisioning tab inside Notifications.
- **[A1] `spike_status` per ForecastInterval**: Amber `SpikeStatus` enum value (`none` | `potential` | `spike`) now flows from SDK → `PricePeriod.spike_status` → `forecast[]` JSON → `priceStrip` → `priceWindows.spike_status`. Strip escalates: any spike interval makes the 30-min window `spike`.

### Fixed
- **Actionable Notification button override logic**: Implemented comprehensive user override webhook callback handling that clears pending approvals, sets a high-priority 0 `NONE` intent lock for 120 minutes, and dispatches `RESUME_NATIVE` via the scheduler bridge to return battery control to native mode.
- **Save Strategy Alpine.js callback**: Corrected button handler from `saveSettings()` to `savePreferences()` under the Preferences tab, resolving a JavaScript runtime exception.
- **[A2] `descriptor` per ForecastInterval**: Raw Amber `PriceDescriptor` value (`negative` | `extremelyLow` | `veryLow` | `low` | `neutral` | `high` | `spike`) preserved separately from `tariff_type` colour band. Existing key `descriptor` in `import_forecast`/`export_forecast` renamed to `tariff_band` to avoid collision.
- **[A3] `tariff_period` / `tariff_season` per ForecastInterval**: `TariffInformation.period` (`offPeak` | `shoulder` | `solarSponge` | `peak`) and `.season` (`default` | `summer` | `winter` | etc.) now extracted alongside the existing `demand_window` field.
- **`is_force_charge` on `priceWindows`**: Computed boolean — true when any 5-min interval has `descriptor` of `negative` or `extremelyLow`. Zero UI impact; pre-wires the B1 automation endpoint.
- **`windowColor(win)` / `periodLabel(win)` helpers** in `amber_tab_v2.js`: colour and badge driven by descriptor + spike_status + tariff_period hierarchy.
- **Strip colour dot**: 30-min header cell dot now uses `windowColor()` — richer semantic colouring (purple for force-charge, gold for solarSponge, etc.) falling back to existing `livePriceBgColor()`.
- **Period label badge**: Appears below demand stripe on 30-min cells when `periodLabel()` returns a signal (⚡ SPIKE, ⚠ SPIKE?, ☀ EXPORT, 💜 FREE, 🔴 PEAK, 🟠 SHOULDER).
- **5-min sub-interval badges**: Expanded accordion rows show spike badge and solarSponge export badge per interval.
- **`amber_tab_v2.js?v=5`** cache-bust; `amber.html` bumped to v2.3.

### Fixed
- **CloudFront PoP card label order**: "CLOUDFRONT POP" header now appears above the PoP code (was below)
- **CloudFront PoP Map icon**: Replaced invisible 9px faint icon with a large glowing blue badge (text-xl, ring glow, hover accent)
- **Map modal timeframe inheritance**: Opening the PoP map modal now syncs `popMapPeriod` to the parent page's active timeframe selection
- **Transitions overlay readability**: Solid `bg-gray-900` background (removed blur glass), merged From→To into one row with `→` arrow, font upgraded to `text-xs` (12px minimum), clear row dividers, wider panel (w-80)
- **API Metrics section nav bar**: Sticky anchor navigation between the 2-col grid and bottom zone — "API Flow" and "Raw Traces" quick-jump links
- **Trace filter naming mismatch (D2)**: Replaced single global `traceFilter` with independent `methodFilter` (snake_case) and `endpointFilter` (camelCase) — each table card has its own search input with a naming convention hint
- **Anchor router collision (D1)**: Section nav "API Flow" / "Raw Traces" buttons now use `scrollIntoView()` instead of `<a href="#">` — previous implementation caused the SPA hash router to set `activeTab='apimetrics-traces'`, rendering a blank page
- **CloudFront "All Time" silent truncation (D4/D5)**: `cloud_metrics_history` and `edge_pop_distribution` now use `limit=200000` for `since_days=0` queries — previous `limit=10000` silently dropped all but the most recent ~3.5 days of CloudFront history
- **`api_edge_metrics` never purged (D6)**: Added `purge_old_edge_metrics(days=90)` to `db.py`; hooked into `_store_metrics()` via a 2880-write cadence counter (~24h at 30s poll); fire-and-forget, never blocks poll loop

---

## [0.1.21] — 2026-03-31

### Added
- **Smart Circuits UI Tab**: Designed and shipped a brand-new observability dashboard embedded natively in the FHAI Web admin panel. It surfaces dynamic telemetry (power/energy) and grants direct interactive authority over Smart Circuit Mode logic (Schedule/Manual) and direct Power state relays (On/Off) using isolated Cloud API routing.

### Changed
- **MQTT Topology Flattening (FEM Parity)**: Aggressively destroyed the deeply nested `via_device` hierarchy (Site → Hub → aGate → aPower). All entities natively bind universally and exclusively to the parent `aGate` device node to mirror exactly the architecture exported from legacy franklinwh-energy-manager (FEM) deployments.
- **Setup Wizard Design**: Overhauled the first-run installation bootstrap wizard with a high-budget aesthetic utilizing the uploaded `setup-login.jpg` artifact image. 

### Fixed
- **Command Topic Resolution Error**: Realigned `command_topic_template` emissions on all Control variables to output utilizing the full 20-character Serial ID (instead of abbreviating to the `short_id`) to successfully navigate the incoming filter conditions of the `GatewayService` command listeners.
- **Battery Reserve SOC Blindspot**: Corrected a mapping issue preventing `backup_reserve_soc` and `self_reserve_soc` from appearing on UI sliders by rebuilding the `_normalise_stats` iteration loop over `get_all_mode_soc()` to strictly filter and assign boundaries derived directly from API work mode mappings.

---

## [0.1.20] — 2026-03-30

### Added
- **Greenfield Setup Wizard**: Natively intercepts the dashboard on a virgin install and forces the user to provide Cloud API credentials via a secure modal before activating any telemetry systems.
- **Headless Provisioning**: Exposes `CLOUD_EMAIL` and `CLOUD_PASSWORD` to natively bootstrap the system without User Interface interaction in Docker and Home Assistant Add-on. 
- **Immutable Tamper Snapshots**: Prints an `INITIAL_GREENFIELD_BOOT` cryptographic hash of the start-up parameters straight into the database to guarantee total transparency for deployment tracking.

### Changed
- **Service Suspension**: Strictly suspends execution of `GatewayRegistry`, `MQTTPublisher`, `CommandListener`, and APScheduler if `config.db` reports 0 gateways linked.
- **Hot-Start Booting**: Smoothly restarts the suspended threads as soon as the Wizard establishes a valid Gateway, entirely negating the need for Docker restarts.
- **MQTT Auto-Binding**: Refined MQTT `manager.py` to prevent incorrect port/token assignments outside of the Home Assistant `core-mosquitto` supervisor Add-on framework.

---

## [0.1.19] — 2026-03-30

### Fixed
- **Add-on Build Pipeline**: Injected missing `git` dependency into Alpine `apk` build manifest to resolve Supervisor source-build failures when pulling the `franklinwh-cloud` library natively from GitHub over pip.

---

## [0.1.18] — 2026-03-30

### Added
- **Native Async Cloud Dispatch Execution:** Ported FEM's 700-line `CloudDispatchService` seamlessly into FHAI as `AsyncCloudDispatchService`. Integrator now internally computes and injects dynamic Time-of-Use JSON schedules to execute `Charge` and `Discharge` requests automatically rather than dropping them.
- **Dispatch Action Handler:** Wired Home Assistant's `dispatch_action` entity to trigger execution. Select `Charge`, `Discharge`, or `Stop`. 
- **Transient Memory Payloads:** Setting inputs in HA for `dispatch_power`, `dispatch_target_soc`, `dispatch_duration`, and emergency backup offsets now appropriately store in a memory cache, acting as modifiers to the main manual `dispatch_action` trigger.
- **PCS Limits Anti-Snapback Cache:** Engineered a 120-second cache-lock for `_pcs_cache` on Grid Import/Export configuration limits to natively reject false state mutations returning from laggy Cloud API polls.
- **Emergency Backup Flag Injections:** Dynamic injection of "Self Consumption", "1 Day", etc. durations inside operating mode updates.

### Fixed
- **Cloud-Only Entity Enums:** Cleaned all HA Entity definitions in `src/models/entities.py`. Corrected legacy enum arrays, injected `Stop` payloads into actions, and successfully removed `Modbus` traces from `dispatch_method` because FHAI's design exclusively drives Cloud APIs.

---

## [0.1.17] — 2026-03-27

### Fixed
- **API Parity Stabilized:** Upgraded `franklinwh-cloud` dependency constraint to `>=0.4.2` allowing the backend to natively inherit the upstream `currendId=0` Java Spring Boot `@NotNull` exception bypasses. Mode-switching and Time of Use commands now natively intercept numerical parameters and legacy payload formats flawlessly.
- **Docker Log Chronology:** Inserted an `/etc/localtime` readonly mount directly into the Integrator's `docker-compose.yml`. This corrects an underlying defect where `fwhhai-app` diagnostic logs were bound to UTC (+00:00), appearing exactly 11 hours behind local AEDT (+11:00) during live verification.
- **Token Watchdog Purge:** Enforced a native localized fallback inside `GatewayService.set_operating_mode` to check the boolean extraction of upstream boolean Cloud errors immediately (mitigating False Positive UI toasts) and violently evicting the authentication `_client` token cache upon hitting an upstream Code 401 traceback.

---

## [0.1.16] — 2026-03-26

### Fixed
- **Auth Guardrails:** Replaced blind auth retry loop with an immediate suspension trigger (`auth_failed` state) on invalid credentials, preventing user accounts from being permanently locked out by the FranklinWH API.
- **Metrics Serialization:** Solved an uncaught `GridStatus is not JSON serializable` error during SQLite metrics writes by strictly resolving nested Enums inside the `_dataclass_to_dict` helper.

### Test Evidence
- Live container tests via browser UI successful against real aGate serials.
- Continuous 30s polling verified flawlessly in Docker with active mock payload updates.

---

## [0.1.15] — 2026-03-26

### Critical — Design v2 (Architecture Overhaul)
- **MQTT Namespace Isolation:** Migrated all topics to `{prefix}/{full_serial}/{category}/{metric}` to resolve collisions with FranklinWH Energy Manager (FEM). Removed legacy `{short_id}` usage in topics.
- **Credential Storage:** Deprecated `credentials_json` in the gateway table. Added `gateway_credentials` and `credential_audit_log` SQLite tables to strictly trace credential lifecycle events.
- **Credential Validation:** API endpoints now actively validate account credentials against the cloud API before persisting via `TokenFetcher`.
- **Poll Guard:** Implemented a non-retrying suspension loop (`no_credentials`) if a gateway is missing cloud credentials, halting API load and logging a clear warning.

### Added
- Auto-migration script `migrate_credentials_from_json()` to transition legacy DB rows to the new tables.
- FAH integration version published to `device/sw_version` retained MQTT payload.
- UI: Gateway list shows a `⚠ No Creds` badge, and Gateway Edit modal displays conditional green/red informational banners regarding credential status.

### Test Evidence
- 135 unit tests passing (`tests/results/phase19-credentials-mqtt-v3-*`).
- Docker test stack build passing 16/16.

---

## [0.1.6] — 2026-03-25

### Fixed — Critical Startup Bug
- `run.sh`: `export PYTHONPATH=/app` added before uvicorn launch — S6 overlay does not
  inherit Docker `ENV` vars, causing `ModuleNotFoundError: No module named 'src'` at runtime

### Added
- `config.yaml`: `cloud_email`, `cloud_password` (masked), `cloud_gateway` in HA Supervisor options
- `run.sh`: reads cloud credentials from bashio and writes to `options.json`
- `src/config/manager.py`: `cloud_email`, `cloud_password`, `cloud_gateway` fields with env var overrides
- `docker-compose.yml`: all settings now env-var-scriptable (`MQTT_HOST`, `CLOUD_EMAIL`, etc.)
  with `.env` file support for headless scripted installs
- `Makefile`: `ha-test-build/run/verify/logs/down/all` targets for local Phase 1 testing
- `scripts/verify_install.py`: now runs via `make verify` / `make ha-test-verify`

### Changed
- Sidebar label: `FranklinWH` → `FranklinWH HA`

### Test Evidence
- Phase 1 local Docker test: **18/18** `verify_install.py` checks passing
- Health endpoint: `{"status":"ok","mqtt_connected":true}`
- MQTT broker connected, `/data` writable

---

## [0.1.5] — 2026-03-25

### Fixed
- `requirements.txt`: `franklinwh-cloud==0.3.0` — package was published to PyPI on 2026-03-24;
  reverted from GitHub URL install and removed all vendoring (0.1.3/0.1.4)
- `Dockerfile`: removed `git` from apk deps and `vendor/` COPY step — pip handles everything
- `scripts/sync_addon.sh`: removed vendor sync step (no longer needed)
- Local Alpine 3.19 test: **16/16** `verify_install.py` checks passing before release

---

## [0.1.4] — 2026-03-25

### Fixed
- `requirements.txt`: added `jsonschema>=4.0.0` (transitive dep of vendored `franklinwh_cloud` — not auto-installed)
- Local Alpine build test: 16/16 verify_install.py checks passing before release

---

_Nothing pending._

---

## [0.1.3] — 2026-03-25

### Added — Build Integrity Gate (FEM Pattern)
- `scripts/verify_install.py`: FEM-style verification (10 dep checks, 6 app module checks, 2 runtime checks)
  — build fails if critical imports fail; also exposed via `manage.sh verify`
- `scripts/sync_addon.sh`: syncs `src/`, `scripts/`, requirements, and vendors `franklinwh_cloud`
  into `franklinwh_ha_integrator/` before release (mirrors FEM's `sync.sh` + `franklinwh-addon` pattern)
- `scripts/manage.sh verify`: runs `verify_install.py` full check inside the container

### Changed — COPY-based Dockerfile (no git clone)
- `franklinwh_ha_integrator/Dockerfile`: switched from `git clone` to `COPY` from build context
  — faster builds (~30s vs 60s+), no `git` dependency, no clone cache issues (see FEM `ha-addon-publish.md`)
- `franklinwh_cloud` is now **vendored** in `franklinwh_ha_integrator/vendor/franklinwh_cloud/`
  — removed GitHub URL from `requirements.txt` inside the addon build context

---

## [0.1.2] — 2026-03-25

### Fixed
- `requirements.txt`: `franklinwh-cloud>=0.9.0` replaced with GitHub install
  (`franklinwh-cloud-client @ git+https://github.com/david2069/franklinwh-cloud.git@v0.3.0`) —
  the package was never published to PyPI

---

## [0.1.1] — 2026-03-25

### Fixed — Home Assistant Add-on Build
- `Dockerfile`: `git clone` now tries `v{BUILD_VERSION}` first (HA Supervisor passes `0.1.0`, git tag is `v0.1.0`)
- `Dockerfile`: `pip3 install` now uses `--break-system-packages` (Alpine PEP 668 externally-managed environment)
- `Dockerfile`: `ENV PYTHONPATH=/app` added — required for `from src.*` imports to resolve inside Alpine container (from FEM build pattern)
- `Dockerfile`: Build-time import verification gate — verifies `fastapi`, `uvicorn`, `aiosqlite`, `paho.mqtt`, `src.main` after pip install; build is rejected if any import fails
- `repository.json`: Added to repo root (required by HA Supervisor for custom repository discovery)
- `franklinwh_ha_integrator/CHANGELOG.md`: Added so HA Supervisor can display changelog in the UI

---

## [0.1.0] — 2026-03-25

Initial release. Full 12-tab admin UI, multi-gateway polling, MQTT Discovery, and HA Add-on manifest.

### Added — Phase 10: Auth, CI/CD & HA Add-on Finalisation
- `src/middleware/auth.py`: `AdminAuthMiddleware` — HTTP Basic Auth, opt-in via
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars or HA Add-on options. `/api/health` and
  `/static/*` always exempt. Auth disabled when no credentials set (open access by default)
- `AppConfig`: `admin_username` + `admin_password` fields, loaded from `options.json` then env vars
- `config.yaml`: added optional `admin_username` / `admin_password` (password schema) options;
  added `url` field for HA custom-repository discovery
- `run.sh`: finalised bashio entrypoint — reads all Supervisor options, falls back to
  `core-mosquitto` if MQTT host not set, writes `options.json`, launches uvicorn on port 8099
- `.github/workflows/ci.yml`: test matrix on Python 3.11 + 3.12, pip cache, ruff lint;
  triggers on push to `main`/`develop` and PRs to `main`
- `.github/workflows/release.yml`: pre-release test gate → multi-arch Docker build
  (amd64/arm64/armv7) → GHCR publish on `v*` tags, with step summary
- `tests/test_auth.py`: 11 tests (open access, exempt routes, 401/200, credential checks)
- 135/135 tests passing

### Added — Phase 9: Remaining Tabs
- 5 remaining admin tabs implemented (zero placeholder tabs remain):
  - **Schedule**: gateway picker, current mode/storm guard/reserve/grid-charge cards,
    TOU period table (peak/off-peak type chips, rate column), link to Control for changes
  - **Battery**: gateway picker, aPower slot cards (rated kW/kWh + MQTT topic preview
    for all 5 accessory sensors), full serial detail table
  - **HA Entities**: searchable/filterable entity registry (19 aGate + 5 aPower accessory);
    type/group/controls-only filters; hw_requires chips; AP-2 policy footer
  - **Grid Profile**: hardware capability chips (solar/generator/smart circuits/V2L/aPower/
    multi-battery), dynamic KV grid of full profile
  - **Metrics**: gateway filter, power flow cards (SoC/battery/grid/load/solar/freq),
    collapsible raw JSON, 30-record recent telemetry table
- `src/routes/api_phase9.py`: `/api/entities`, `/api/gateways/{id}/batteries`,
  `/api/gateways/{id}/profile`, `/api/gateways/{id}/schedule`, `/api/metrics`
  (with `?short_id=` and `?limit=` params)
- `db.get_gateway_full` alias added
- 16 new Phase 9 tests; 126/126 passing

### Added — Phase 8: Diagnostics
- `src/routes/api_metrics.py`: `/api/health/detail` (system health: gateway status, MQTT,
  DB size), `/api/logs/startup` (paginated), `/api/metrics/api` (Cloud API latency tracking)
- DB helpers: `get_startup_logs()`, `get_api_performance()`, `get_recent_metrics()`
- 3 diagnostic admin tabs:
  - **Health**: app version/env/DB size banner, per-gateway status table, MQTT health cards
  - **Logs**: startup log from DB, phase+env filter dropdowns, expandable JSON details
  - **API Metrics**: per-gateway filter, 4 summary cards (avg/max/min latency, error count),
    CSS latency bar chart, raw call table
- 13 new Phase 8 tests; 110/110 passing

### Added — Phase 7: MQTT Admin
- `MQTTPublisher` runtime telemetry: `messages_published`, `reconnect_count`, `queue_depth`,
  `last_error` counters
- `src/routes/api_mqtt.py`: `GET /api/mqtt/status`, `/api/mqtt/topics`, `/api/mqtt/config`,
  `POST /api/mqtt/reconnect`
- `db.get_config_value` / `db.set_config_value` — JSON-serialising `app_config` table helpers
- MQTT Admin tab: broker status cards, topic map, persistent config overrides
- 12 new Phase 7 tests; 97/97 passing

### Added — Phase 6: Controls
- `GatewayService.dispatch_command(slug, value)` — COMMAND_DISPATCH table maps 5 slugs
  to Cloud API methods with type coercion (`str` / `int` / `_str_to_bool`)
- `GatewayRegistry.dispatch_command(short_id, slug, value)` — routing layer
- `CommandListener._handle_command()` wired to `registry.dispatch_command()`
  — MQTT command topics now reach the Cloud API end-to-end
- `src/routes/api_control.py`: `GET /api/gateways/{id}/control/commands` +
  `POST /api/gateways/{id}/control`
- Control tab UI: operating mode buttons (backup/normal/self-consumption/TOU),
  backup reserve slider (0–100%), grid charge / discharge / storm guard toggles,
  timestamped command log
- 20 new Phase 6 tests; 85/85 passing

### Added — Phase 5: Gateways Tab
- Gateway CRUD API extended: `PATCH /api/gateways/{id}`, `/start`, `/stop`,
  `/validate`, `/validate_credentials`
- Hot-registry: adding calls `registry.start_gateway()`; deleting calls
  `registry.stop_gateway()`; PATCH with new credentials calls `registry.restart_gateway()`
- Gateways tab UI: table with poll-status chips, start/stop/edit/validate/delete per row,
  slide-in add/edit drawer, "Test Credentials" Cloud API validation, delete confirm modal,
  toast notifications
- 11 new Phase 5 tests; 65/65 passing

### Added — Phase 4: Admin UI
- `design-system.css` — 450-line dark-first CSS system (tokens, glassmorphism cards,
  SOC ring gauge, power bars, sidebar, form controls, buttons)
- `app.js` — Alpine.js global store: gateway polling, tab navigation, MQTT status,
  auto-refresh every 15 s
- `admin.html` — 12-tab sidebar shell (Primary / Configuration / Diagnostics groups),
  topbar with gateway dropdown, env badge, MQTT connection dot
- `dashboard.html` — per-gateway cards (SOC ring + power bars), live detail panel (KV grid)
- Jinja2 `cache_size=0` fix for Python 3.14 LRUCache dict-key incompatibility

### Added — Phase 3: MQTT Publisher
- `EntityDef` + `AGATE_ENTITIES` (19 entities) + `BATTERY_ACCESSORY_ENTITIES` (5 entities);
  hardware-suppress flags (`solar`, `generator`, `smart_circuits`, `v2l`, `apbox`)
- `MQTTPublisher` — queue-based background task, HA Discovery fan-out,
  state publishing, availability (online/offline), AP-2 discovery-once policy
- `CommandListener` — MQTT subscriber for `franklinwh/+/control/+/set`;
  reconnect backoff; wired to registry
- 20 new Phase 3 tests; 54/54 passing

### Added — Phase 2: Multi-Gateway Service
- `GatewayService` — async poll loop, exponential backoff (30→300 s),
  `_poll_once()` via `franklinwh-cloud`, metric fan-out, live status tracking
- `GatewayRegistry` — `start_all()` / `stop_all()` lifecycle; hot-add / hot-remove;
  aggregate status
- Live status routes: `GET /api/gateways/{id}/status`, `GET /api/gateways/status/all`
- 10 new Phase 2 tests; 34/34 passing

### Added — Phase 1: Backend Core
- `aiosqlite` DB schema (WAL mode) — tables: `gateways`, `batteries`, `gateway_metrics`,
  `api_performance`, `app_config`, `startup_log`; schema version tracking
- `GatewayProfile` Pydantic model; `make_short_id()` convention (serial[-8:])
- `AppConfig` — options.json → env var loading with safe-dict redaction
- Staged startup lifespan: env → config → db → MQTT → registry
- Gateway CRUD routes: `GET/POST /api/gateways`, `GET/DELETE /api/gateways/{id}`
- 16 new Phase 1 tests; 24/24 passing

### Added — Phase 0: Project Scaffold
- Project structure: `src/`, `tests/`, `scripts/`, `.agents/workflows/`
- `AGENT.md` with policies AP-1 (Queue → Plan → Execute), AP-2 (discovery immutability)
- `Dockerfile` (Python 3.12-slim, multi-arch), `docker-compose.yml` + Mosquitto sidecar
- HA Add-on manifest (`config.yaml`, `run.sh`) — multi-arch (armhf/armv7/aarch64/amd64/i386)
- `requirements.txt`, `.gitignore`, `README.md`, `CHANGELOG.md`
- Automated workflow files (`/commit-after-phase`, `/docker-deploy`, `/end-of-session`)
- 8 initial tests passing

---

## Test Coverage Progression

| Phase | Tests | Commit |
|-------|-------|--------|
| 0 — Scaffold | 8 | `1e2251e` |
| 1 — Backend Core | 24 | `1703d1c` |
| 2 — Multi-Gateway | 34 | `ff0e0bd` |
| 3 — MQTT Publisher | 54 | `d01030f` |
| 4+5 — Admin UI + Gateways | 65 | `2d288a4` |
| 6 — Controls | 85 | `2bce234` |
| 7 — MQTT Admin | 97 | `3fc125c` |
| 8 — Diagnostics | 110 | `593d9bb` |
| 9 — Remaining Tabs | 126 | `91e58de` |
| 10 — Auth + CI/CD | 135 | `c8b7cc2` |
