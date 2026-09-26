# FranklinWH HA Integrator

Bridges one or more FranklinWH aGate battery systems to Home Assistant over
MQTT Discovery — battery monitoring, mode control, TOU scheduling and power
management, for multiple gateways from one add-on.

## Installation

1. Add this repository to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Install **FranklinWH HA Integrator**.
3. Set the options below, then **Start**.
4. Open the UI from the sidebar (the add-on runs behind Ingress — no port to
   expose and no separate login).

The aim is three touches: install, enter your gateway serial and cloud
credentials, start. Everything else auto-detects.

## Options

| Option | Default | Notes |
|---|---|---|
| `mqtt_host` | `""` | Leave empty to use the Home Assistant Mosquitto add-on. |
| `mqtt_port` | `1883` | |
| `mqtt_username` / `mqtt_password` | `""` | Only if your broker requires them. |
| `topic_prefix` | `franklinwh` | Change only if it collides with something. |
| `ha_discovery_prefix` | `homeassistant` | Must match your HA discovery prefix. |
| `poll_interval` | `30` | Seconds. The cloud API rate-limits; below 30 is not advised. |
| `admin_username` / `admin_password` | `""` | Leave empty to disable the login. Behind Ingress you are already authenticated by Home Assistant. |
| `cloud_email` / `cloud_password` | `""` | Your FranklinWH app credentials. Can also be entered in the UI instead. |
| `cloud_gateway` | `""` | Gateway serial. Leave empty to discover every gateway on the account. |

Credentials entered in the UI are stored in the add-on's own database and
take precedence over the options above.

## Headless setup

Setting `cloud_email` and `cloud_password` provisions the add-on on its first
boot — it discovers every gateway on the account, registers them, and marks
setup complete so the wizard does not appear. Nothing else is required, and no
UI is involved.

`cloud_gateway` is an optional **filter**, not a picker. Leave it blank for
every gateway on the account, or give a comma-separated list of serial
suffixes:

```yaml
cloud_email: you@example.com
cloud_password: ...
cloud_gateway: "99900001,24170092"    # or blank for all
```

A suffix that matches nothing is logged with the serials that were available,
rather than provisioning nothing in silence. One gateway failing to register
does not abandon the others.

The same names work as environment variables for a plain Docker deployment.

## What you get

**Entities** — battery SoC, power flow (solar, home, grid, battery), per-aPower
telemetry and BMS detail, grid and generator relay states, smart circuits,
operating mode, and run status. All via MQTT Discovery, so they appear on their
own.

**Control** — operating mode, manual charge and discharge dispatch with a power
level and duration, SoC limits, and smart-circuit switching.

**Scheduling** — a full TOU schedule editor with seasons, day types and dispatch
blocks; named presets an automation can switch to; push history with restore;
and an optimiser that proposes a schedule from your tariff.

**Costs** — import, export, standing charges, demand charges and two-way export
tariffs, priced from your own plan. See the Pricing tab.

## Supported hardware

aGate X 1.0 / 1.1 / 1.3 / 1.3.1, with aPower X, aPower 2 and aPower S —
including mixed fleets, since an aGate X 1.3 can carry all three. Accessories
(generator module, smart circuits, split CT, aHub, aPbox) are detected from the
account.

Architectures: **aarch64** and **amd64**. 32-bit builds are not offered —
several dependencies are Rust and rustup does not support the musl armv7 target
triple, so they cannot be built at all rather than merely being slow.

## Troubleshooting

**No entities appear.** Check the broker settings, then Diagnostics → Health in
the UI. MQTT connection state is reported there.

**Gateway shows as unavailable.** The cloud API is the data source; if the
FranklinWH app cannot see the gateway, neither can this. Check the gateway's
own network connection first.

**Cost figures look wrong.** Rates are in **cents**, matching how retailers
quote them — a supply charge of 158.631 is c/day, not dollars. The Pricing tab
warns when a value looks like dollars typed into a cents field.

**Schedule blocks fire at the wrong time.** Set the plan timezone on the
utility service. Schedule blocks are local wall-clock, and the gateway's own
timezone is shown on the Gateways tab.

## Support

Issues and feature requests:
<https://github.com/david2069/franklinwh-ha-integrator/issues>

This is an unofficial integration and is not affiliated with FranklinWH.
