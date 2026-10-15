# FranklinWH Add-ons for Home Assistant

Home Assistant add-ons for FranklinWH aGate battery systems.

## Install

1. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store**.
2. Open the **three-dot menu** (top right) -> **Repositories**.
3. Paste this URL and click **Add**:

   ```
   https://github.com/david2069/franklinwh-addon
   ```

4. Close the dialog. **FranklinWH HA Integrator** now appears in the store —
   reload the page if it does not.
5. Click it, then **Install**. The add-on ships as a prebuilt image, so this is
   a download rather than a build: seconds, not minutes.
6. Open **Configuration**, enter your FranklinWH cloud email and password, and
   **Save**.
7. **Start**, then open the UI from the sidebar.

Nothing else is required. MQTT defaults to the Home Assistant Mosquitto add-on,
gateways are discovered from your account, and the add-on runs behind Ingress —
no port to expose and no separate login.

## Add-ons in this repository

### FranklinWH HA Integrator

Bridges one or more FranklinWH aGate battery systems to Home Assistant over
MQTT Discovery: battery and per-aPower telemetry, mode control, manual charge
and discharge, TOU schedule editing with presets and push history, solar
forecasting, and cost tracking against your own tariff.

Full documentation is on the add-on's **Documentation** tab after installing,
or [here](franklinwh_ha_integrator/DOCS.md).

## Requirements

- Home Assistant **OS** or **Supervised** — add-ons are not available on
  Container or Core installs
- Architecture **aarch64** or **amd64**: HA Green, HA Yellow, Raspberry Pi 4/5,
  and any x86 machine or VM. 32-bit builds are not offered — several
  dependencies are Rust, and rustup does not support the musl armv7 target
  triple, so they cannot be built at all.
- A FranklinWH cloud account — the same credentials as the FranklinWH app
- An MQTT broker; the Mosquitto add-on is detected automatically

## Updating

Home Assistant offers an update when a new version is published. Release notes
are on the add-on's **Changelog** tab.

## Support

Issues and feature requests:
<https://github.com/david2069/franklinwh-ha-integrator/issues>

Unofficial. Not affiliated with, endorsed by, or supported by FranklinWH.
