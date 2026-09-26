"""
Entity definitions — the HA MQTT Discovery contract.

Each EntityDef maps one HA entity to:
  - its MQTT topic groups and slug
  - its HA entity type (sensor/select/switch/number)
  - its stat_path: dot-notation path into the normalised stats dict
  - optional hw_requires: feature flag that must be True on GatewayProfile

AP-2 POLICY: slugs, ha_type, unique_id format are IMMUTABLE once published.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EntityDef:
    slug: str               # Permanent identifier — never rename after first publish
    name: str               # Human-readable HA entity name
    ha_type: str            # sensor | select | switch | number | button
    state_group: str        # MQTT sub-path: battery | solar | grid | status | energy | ...
    stat_path: str = ""     # Dot-notation into normalised stats dict, e.g. "current.battery_use"

    # HA entity metadata
    unit: str = ""
    device_class: str = ""
    state_class: str = ""   # measurement | total_increasing
    icon: str = ""
    entity_category: str = "" # config | diagnostic | ""

    # Hardware suppression
    hw_requires: str = ""   # "" = always | "solar" | "generator" | "smart_circuits" | "v2l" | "apbox" | "ahub"
    
    # Device Hub Routing (Phase 108)
    device_type: str = ""   # "" = aGate | "smart_circuit_1", "generator", etc.

    # Control entity fields
    is_control: bool = False
    options: list = field(default_factory=list)   # for select
    min_val: Optional[float] = None               # for number
    max_val: Optional[float] = None               # for number
    step: Optional[float] = None                  # for number

    @property
    def unique_id_template(self) -> str:
        """HA unique_id format: franklinwh_{short_id}_{slug}"""
        return "franklinwh_{short_id}_" + self.slug

    @property
    def state_topic_template(self) -> str:
        return f"franklinwh/{{short_id}}/{self.state_group}/{self.slug}"

    @property
    def command_topic_template(self) -> Optional[str]:
        if self.is_control:
            return f"franklinwh/{{short_id}}/control/{self.slug}/set"
        return None

    @property
    def discovery_topic_template(self) -> str:
        return f"homeassistant/{self.ha_type}/franklinwh_{{short_id}}/{self.slug}/config"


# ---------------------------------------------------------------------------
# PARENT aGate ENTITY REGISTRY
# ---------------------------------------------------------------------------
# AP-2: This list is the source of truth. Any addition requires explicit approval.

AGATE_ENTITIES: list[EntityDef] = [

    # === BATTERY ===
    EntityDef(
        slug="battery_soc",
        name="State of Charge",
        ha_type="sensor",
        state_group="battery",
        stat_path="battery_soc",
        unit="%",
        device_class="battery",
        state_class="measurement",
        icon="mdi:battery",
    ),
    EntityDef(
        slug="battery_power_kw",
        name="Battery Power",
        ha_type="sensor",
        state_group="battery",
        stat_path="current.battery_use",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:battery-charging",
    ),
    EntityDef(
        slug="battery_charge_today_kwh",
        name="Daily Battery Charge",
        ha_type="sensor",
        state_group="energy",
        stat_path="totals.battery_charge",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:battery-arrow-up",
    ),
    EntityDef(
        slug="battery_discharge_today_kwh",
        name="Daily Battery Discharge",
        ha_type="sensor",
        state_group="energy",
        stat_path="totals.battery_discharge",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:battery-arrow-down",
    ),

    # === SOLAR ===
    EntityDef(
        slug="solar_power_kw",
        name="Solar Power",
        ha_type="sensor",
        state_group="solar",
        stat_path="current.solar_production",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power",
        hw_requires="solar",
    ),
    EntityDef(
        slug="solar_today_kwh",
        name="Daily Solar Energy",
        ha_type="sensor",
        state_group="energy",
        stat_path="totals.solar",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:solar-power-variant",
        hw_requires="solar",
    ),

    # === GRID ===
    EntityDef(
        slug="grid_power_kw",
        name="Grid Power",
        ha_type="sensor",
        state_group="power",
        stat_path="current.grid_use",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:transmission-tower",
    ),
    EntityDef(
        slug="grid_frequency_hz",
        name="Frequency",
        ha_type="sensor",
        state_group="status",
        stat_path="current.grid_frequency",
        unit="Hz",
        device_class="frequency",
        state_class="measurement",
        icon="mdi:sine-wave",
    ),
    EntityDef(
        slug="home_load_kw",
        name="Home Load",
        ha_type="sensor",
        state_group="power",
        stat_path="current.home_load",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-lightning-bolt",
    ),

    # === GENERATOR ===
    EntityDef(
        slug="generator_power_kw",
        name="Generator Power",
        ha_type="sensor",
        state_group="power",
        stat_path="current.generator_production",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:engine",
        hw_requires="generator",
        device_type="generator",
    ),

    # === CIRCUITS (Smart Circuits) ===
    # AP-2: slugs are immutable. 'name' is the HA display label — dynamically overridden
    # at publish_discovery() time with the cloud custom name (c_detail.name).
    # Default fallback: "Circuit N" (aligns with FranklinWH official app naming).
    EntityDef(
        slug="smart_circuit_1_kw",
        name="Circuit 1 Power",
        ha_type="sensor",
        state_group="smart_circuits",
        stat_path="smart_circuit_1_power",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:electric-switch",
        hw_requires="smart_circuits",
        device_type="smart_circuit_1",
    ),
    EntityDef(
        slug="smart_circuit_2_kw",
        name="Circuit 2 Power",
        ha_type="sensor",
        state_group="smart_circuits",
        stat_path="smart_circuit_2_power",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:ev-station",
        hw_requires="smart_circuit_2",
        device_type="smart_circuit_2",
    ),
    # Daily energy sensors — state_class=total_increasing required for HA Energy Dashboard
    EntityDef(
        slug="smart_circuit_1_energy_kwh",
        name="Circuit 1 Daily Energy",
        ha_type="sensor",
        state_group="smart_circuits",
        stat_path="smart_circuit_1_energy",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:ev-station",
        hw_requires="smart_circuits",
        device_type="smart_circuit_1",
    ),
    EntityDef(
        slug="smart_circuit_2_energy_kwh",
        name="Circuit 2 Daily Energy",
        ha_type="sensor",
        state_group="smart_circuits",
        stat_path="smart_circuit_2_energy",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:ev-station",
        hw_requires="smart_circuit_2",
        device_type="smart_circuit_2",
    ),

    # === STATUS (diagnostic sensors) ===
    # Safety interlock for the one command that physically opens the grid
    # relay. FHAI's own UI puts Go Off-Grid behind an acknowledgement modal;
    # Home Assistant got it as a bare dropdown option that fired immediately.
    EntityDef(
        slug="control_lock",
        name="Control Lock",
        ha_type="switch",
        state_group="control",
        icon="mdi:shield-lock-outline",
        entity_category="config",
        is_control=True,
    ),
    EntityDef(
        slug="lock_grid",
        name="Lock Grid Disconnect",
        ha_type="switch",
        state_group="control",
        icon="mdi:transmission-tower-off",
        entity_category="config",
        is_control=True,
    ),
    EntityDef(
        slug="lock_operating_mode",
        name="Lock Operating Mode",
        ha_type="switch",
        state_group="control",
        icon="mdi:cog-off-outline",
        entity_category="config",
        is_control=True,
    ),
    EntityDef(
        slug="lock_battery_dispatch",
        name="Lock Battery Dispatch",
        ha_type="switch",
        state_group="control",
        icon="mdi:battery-lock",
        entity_category="config",
        is_control=True,
    ),
    EntityDef(
        slug="operating_mode_sensor",
        name="Operating Mode",
        ha_type="sensor",
        state_group="status",
        stat_path="mode.work_mode_desc",
        icon="mdi:cog-outline",
        entity_category="diagnostic",
        # The same fact as the Operating Mode select, so it must be spelled the
        # same way. Without these the cloud's 'Time-Of-Use' was published here
        # while the select published 'Time-of-Use', and anything comparing the
        # two never matched.
        options=["Time-of-Use", "Self-Consumption", "Emergency Backup"],
    ),
    EntityDef(
        slug="runtime_mode",
        name="Runtime Mode",
        ha_type="sensor",
        state_group="status",
        stat_path="mode.runtime_mode",
        icon="mdi:state-machine",
        entity_category="diagnostic",
        device_type="site",
    ),
    EntityDef(
        # Batch I diagnostic: increments once per FranklinWH cloud stale-window
        # poll dropped by _normalise_stats (soc==0 while prior good SoC > 5%).
        # Session-scoped counter — resets on GatewayService restart. Track the
        # rate over time to correlate with any user-visible weirdness.
        slug="stale_polls_dropped",
        name="Cloud Stale-Window Drops",
        ha_type="sensor",
        state_group="status",
        stat_path="stale_polls_dropped",
        state_class="total_increasing",
        icon="mdi:cloud-alert",
        entity_category="diagnostic",
    ),

    # === CONTROLS ===
    EntityDef(
        slug="operating_mode",
        name="Operating Mode",
        ha_type="select",
        state_group="control",
        stat_path="mode.work_mode_desc",
        icon="mdi:cog",
        is_control=True,
        options=["Time-of-Use", "Self-Consumption", "Emergency Backup"],
    ),
    EntityDef(
        slug="off_grid_mode",
        name="Off-Grid Mode",
        ha_type="select",
        state_group="control",
        stat_path="control._off_grid_mode",
        icon="mdi:transmission-tower-off",
        is_control=True,
        options=["On-Grid", "Off-Grid"],
    ),
    EntityDef(
        slug="storm_hedge",
        name="Storm Hedge",
        ha_type="select",
        state_group="control",
        stat_path="control._storm_hedge_enabled",
        icon="mdi:weather-lightning",
        is_control=True,
        options=["Disabled", "Enabled"],
    ),
    EntityDef(
        slug="dispatch_action",
        name="Dispatch Action",
        ha_type="select",
        state_group="control",
        stat_path="dispatch.action",
        icon="mdi:lightning-bolt",
        is_control=True,
        options=["Idle", "Charge", "Discharge", "Stop"],
    ),
    EntityDef(
        slug="battery_backup_reserve_soc",
        name="Emergency Backup Reserved SOC",
        ha_type="number",
        state_group="control",
        stat_path="backup_reserve_soc",
        unit="%",
        icon="mdi:battery-lock",
        is_control=True,
        min_val=0,
        max_val=100,
        step=1,
    ),
    EntityDef(
        slug="battery_self_consumption_reserve_soc",
        name="Battery Self-Consumption SOC",
        ha_type="number",
        state_group="control",
        stat_path="self_reserve_soc",
        unit="%",
        icon="mdi:battery-charging-50",
        is_control=True,
        min_val=0,
        max_val=100,
        step=1,
    ),
    # Phase C (AP-2 approved): TOU reserve floor — maps to workMode:1 via update_soc()
    EntityDef(
        slug="battery_tou_reserved_soc",
        name="Battery TOU Reserved SOC",
        ha_type="number",
        state_group="control",
        stat_path="tou_reserve_soc",
        unit="%",
        icon="mdi:battery-clock",
        is_control=True,
        min_val=0,
        max_val=100,
        step=1,
    ),

    # === EXTENDED CLOUD DIAGNOSTICS & HARDWARE (Phase 16 Parity) ===
    # Relays
    EntityDef(slug="relay_grid1", name="Grid Relay 1", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.grid1", device_class="power", icon="mdi:electric-switch", entity_category="diagnostic"),
    EntityDef(slug="relay_grid2", name="Grid Relay 2", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.grid2", device_class="power", icon="mdi:electric-switch", entity_category="diagnostic"),
    EntityDef(slug="relay_generator", name="Generator Relay", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.generator", device_class="power", icon="mdi:engine", hw_requires="generator", device_type="generator", entity_category="diagnostic"),
    EntityDef(slug="relay_solar", name="Solar PV Relay 1", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.solar1", device_class="power", icon="mdi:solar-panel", entity_category="diagnostic"),
    EntityDef(slug="relay_black_start", name="Black Start Relay", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.blackStart", device_class="power", icon="mdi:power-plug", entity_category="diagnostic"),
    EntityDef(slug="relay_pv2", name="Solar PV Relay 2", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.pv2", device_class="power", icon="mdi:solar-panel-large", entity_category="diagnostic"),
    EntityDef(slug="relay_apbox", name="aPowerBox Relay", ha_type="binary_sensor", state_group="relays", stat_path="power.relays.apbox", device_class="power", icon="mdi:package-variant", hw_requires="apbox", device_type="apbox", entity_category="diagnostic"),

    # aGate Analytics & Connectivity
    EntityDef(slug="wifi_signal_dbm", name="WiFi Signal", ha_type="sensor", state_group="agate", stat_path="agate.wifi_signal", unit="dBm", device_class="signal_strength", icon="mdi:wifi", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="mobile_signal_dbm", name="Mobile Signal", ha_type="sensor", state_group="agate", stat_path="agate.mobile_signal", unit="dBm", device_class="signal_strength", icon="mdi:signal-cellular-3", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="network_connection", name="Network Connection", ha_type="sensor", state_group="agate", stat_path="agate.network_connection", icon="mdi:network", entity_category="diagnostic"),
    EntityDef(slug="ambient_temp_c", name="Ambient Temperature", ha_type="sensor", state_group="agate", stat_path="agate.ambient_temp", unit="°C", device_class="temperature", icon="mdi:thermometer", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="provider_mode", name="Data Provider", ha_type="sensor", state_group="integration", stat_path="integration.provider_mode", icon="mdi:cloud-sync", entity_category="diagnostic"),
    EntityDef(slug="update_available", name="Update Available", ha_type="sensor", state_group="integration", stat_path="integration.update_available", icon="mdi:cellphone-arrow-down", entity_category="diagnostic"),

    # Device Identity
    EntityDef(slug="device_model", name="Model", ha_type="sensor", state_group="device", stat_path="device.model", icon="mdi:information-outline", entity_category="diagnostic"),
    EntityDef(slug="device_firmware", name="Firmware Version", ha_type="sensor", state_group="device", stat_path="device.firmware_version", icon="mdi:chip", entity_category="diagnostic"),
    EntityDef(slug="device_cloud_software", name="Software Version (Cloud)", ha_type="sensor", state_group="device", stat_path="device.cloud_software_version", icon="mdi:cloud-check", entity_category="diagnostic"),
    EntityDef(slug="device_timezone", name="Site Timezone", ha_type="sensor", state_group="device", stat_path="device.timezone", icon="mdi:map-clock"),

    # Extended Energy Limits & Triggers
    EntityDef(slug="grid_export_unlimited", name="Grid Export Unlimited", ha_type="switch", state_group="control", stat_path="control._pcs_grid_export_unlimited", icon="mdi:transmission-tower-export", is_control=True),
    EntityDef(slug="grid_import_unlimited", name="Grid Import Unlimited", ha_type="switch", state_group="control", stat_path="control._pcs_grid_import_unlimited", icon="mdi:transmission-tower-import", is_control=True),
    EntityDef(slug="storm_backup_lead_time", name="Storm Backup Lead Time", ha_type="number", state_group="control", stat_path="control._storm_backup_lead_min", unit="min", icon="mdi:timer-outline", is_control=True, min_val=0, max_val=330, step=30),
    EntityDef(slug="storm_decision_strategy", name="Storm Decision Strategy", ha_type="select", state_group="control", stat_path="control._storm_decision_strategy", icon="mdi:shield-alert-outline", is_control=True, options=['Disabled', 'Auto-Active', 'Ask Each Time']),

    # Phase 106: Circuit switches (slugs immutable per AP-2; display name overridden dynamically at discovery)
    # hw_requires per slot: circuit 2 suppressed if SwMerge; circuit 3 AU-only suppression via smart_circuit_3 flag
    EntityDef(slug="smart_circuit_1", name="Circuit 1", ha_type="switch", state_group="control", stat_path="control.smart_circuit_1", icon="mdi:power-socket-us", is_control=True, hw_requires="smart_circuits", device_type="smart_circuit_1"),
    EntityDef(slug="smart_circuit_2", name="Circuit 2", ha_type="switch", state_group="control", stat_path="control.smart_circuit_2", icon="mdi:power-socket-us", is_control=True, hw_requires="smart_circuit_2", device_type="smart_circuit_2"),
    EntityDef(slug="smart_circuit_3", name="Circuit 3", ha_type="switch", state_group="control", stat_path="control.smart_circuit_3", icon="mdi:power-socket-us", is_control=True, hw_requires="smart_circuit_3", device_type="smart_circuit_3"),

    EntityDef(slug="grid_export_limit", name="Grid Export Limit", ha_type="number", state_group="control", stat_path="control.grid_export_limit", unit="kW", icon="mdi:transmission-tower-export", is_control=True, min_val=0, max_val=100, step=0.1),
    EntityDef(slug="grid_import_limit", name="Grid Import Limit", ha_type="number", state_group="control", stat_path="control.grid_import_limit", unit="kW", icon="mdi:transmission-tower-import", is_control=True, min_val=0, max_val=100, step=0.1),

    EntityDef(slug="emergency_backup_duration", name="Emergency Backup Duration", ha_type="number", state_group="control", stat_path="control.emergency_backup_duration", unit="min", icon="mdi:timer-sand", is_control=True, min_val=30, max_val=4320, step=30),
    EntityDef(slug="emergency_backup_duration_type", name="Emergency Backup Duration Type", ha_type="select", state_group="control", stat_path="control.emergency_backup_duration_type", icon="mdi:clock-alert", is_control=True, options=["Indefinite", "1 Day", "2 Days", "3 Days", "Customize"]),
    EntityDef(slug="emergency_backup_resume_mode", name="Emergency Backup Resume Mode", ha_type="select", state_group="control", stat_path="control.emergency_backup_resume_mode", icon="mdi:backup-restore", is_control=True, options=["Self-Consumption", "Time-of-Use"]),

    EntityDef(slug="dispatch_duration", name="Dispatch Duration", ha_type="number", state_group="control", stat_path="control.dispatch_duration", unit="min", icon="mdi:timer-outline", is_control=True, min_val=5, max_val=480, step=5),
    EntityDef(slug="dispatch_target_soc", name="Dispatch Target SOC", ha_type="number", state_group="control", stat_path="control.dispatch_target_soc", unit="%", icon="mdi:battery-arrow-up", is_control=True, min_val=20, max_val=100, step=5),
    EntityDef(slug="dispatch_power", name="Dispatch Power", ha_type="number", state_group="control", stat_path="control.dispatch_power", unit="kW", icon="mdi:flash", is_control=True, min_val=0.1, max_val=5.0, step=0.1),
    EntityDef(slug="dispatch_method", name="Dispatch Method", ha_type="select", state_group="control", stat_path="control.dispatch_method", icon="mdi:cloud-sync", is_control=True, options=["Cloud TOU"]),

    EntityDef(slug="tou_saved_dispatches", name="TOU Preset Dispatches", ha_type="select", state_group="control", stat_path="control.tou_saved_dispatches", icon="mdi:format-list-bulleted", is_control=True, options=["Stop / Restore"]),

    # Hardware Specs & Lifecycles
    EntityDef(slug="battery_count", name="Battery Count", ha_type="sensor", state_group="capacity", stat_path="capacity.battery_count", icon="mdi:battery-multiple", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="batteries_online", name="Batteries Online", ha_type="sensor", state_group="capacity", stat_path="capacity.batteries_online", icon="mdi:battery-heart", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="total_capacity_kwh", name="Total Capacity", ha_type="sensor", state_group="capacity", stat_path="capacity.total", unit="kWh", device_class="energy", icon="mdi:battery-high", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="available_capacity_kwh", name="Available Capacity", ha_type="sensor", state_group="capacity", stat_path="capacity.available", unit="kWh", device_class="energy", icon="mdi:battery-50", state_class="measurement", entity_category="diagnostic"),

    # Cross-Topology Power Flow Additions
    EntityDef(slug="grid_to_battery_kw", name="Grid Charging Battery", ha_type="sensor", state_group="power", stat_path="power.grid_to_battery", unit="kW", device_class="power", icon="mdi:transmission-tower", state_class="measurement"),
    EntityDef(slug="solar_to_grid_kw", name="Solar Export to Grid", ha_type="sensor", state_group="power", stat_path="power.solar_to_grid", unit="kW", device_class="power", icon="mdi:solar-power", state_class="measurement"),
    EntityDef(slug="battery_to_grid_kw", name="Battery Export to Grid", ha_type="sensor", state_group="power", stat_path="power.battery_to_grid", unit="kW", device_class="power", icon="mdi:battery-arrow-up", state_class="measurement"),

    # === SPRINT 4 — MQTT ENTITY PARITY EXPANSION ===

    # Phase A: Status & Dispatch Sensors (from existing root stats fields)
    EntityDef(slug="status_grid_status", name="Grid Status", ha_type="sensor", state_group="status", stat_path="status.grid_status", icon="mdi:transmission-tower", device_class="enum", options=["Connected", "Outage", "SimulatedOffGrid", "NotGridTied"]),
    EntityDef(slug="status_active_dispatch_name", name="Active Dispatch", ha_type="sensor", state_group="status", stat_path="status.active_dispatch_name", icon="mdi:calendar-clock"),
    EntityDef(slug="status_active_dispatch_remaining", name="Dispatch Remaining", ha_type="sensor", state_group="status", stat_path="status.active_dispatch_remaining", icon="mdi:timer-outline"),
    EntityDef(slug="status_active_dispatch_start", name="Dispatch Start", ha_type="sensor", state_group="status", stat_path="status.active_dispatch_start", icon="mdi:calendar-arrow-right"),
    EntityDef(slug="status_active_dispatch_end", name="Dispatch End", ha_type="sensor", state_group="status", stat_path="status.active_dispatch_end", icon="mdi:calendar-arrow-left"),
    EntityDef(slug="battery_heater_state", name="Battery Heater", ha_type="binary_sensor", state_group="battery", stat_path="battery.heater_state", device_class="heat", icon="mdi:heating-coil", entity_category="diagnostic"),
    EntityDef(slug="status_generator_enabled", name="Generator Enabled", ha_type="binary_sensor", state_group="status", stat_path="status.generator_enabled", icon="mdi:engine", entity_category="diagnostic"),

    # Phase B: Daily Energy Sensors (from existing totals in get_stats)
    EntityDef(slug="energy_daily_grid_export", name="Daily Grid Export", ha_type="sensor", state_group="energy", stat_path="grid_export_today", unit="kWh", device_class="energy", icon="mdi:transmission-tower-export", state_class="total_increasing"),
    EntityDef(slug="energy_daily_grid_import", name="Daily Grid Import", ha_type="sensor", state_group="energy", stat_path="grid_import_today", unit="kWh", device_class="energy", icon="mdi:transmission-tower-import", state_class="total_increasing"),
    EntityDef(slug="power_solar_to_battery", name="Solar Charging Battery", ha_type="sensor", state_group="power", stat_path="power.solar_to_battery", unit="kW", device_class="power", icon="mdi:solar-panel", state_class="measurement"),

    # Phase C: Battery Current + Capacity Limits (from BMS slow-poll)
    EntityDef(slug="battery_current_a", name="Battery Current", ha_type="sensor", state_group="battery", stat_path="battery.current", unit="A", device_class="current", icon="mdi:current-dc", state_class="measurement"),
    EntityDef(slug="capacity_max_charge_kw", name="Max Charge Power", ha_type="sensor", state_group="capacity", stat_path="capacity.max_charge_kw", unit="kW", device_class="power", icon="mdi:arrow-down-bold", state_class="measurement", entity_category="diagnostic"),
    EntityDef(slug="capacity_max_discharge_kw", name="Max Discharge Power", ha_type="sensor", state_group="capacity", stat_path="capacity.max_discharge_kw", unit="kW", device_class="power", icon="mdi:arrow-up-bold", state_class="measurement", entity_category="diagnostic"),

    # Phase D: Integration Timestamps, Device Serial, Remote Solar, Smart Circuit 3 Power
    EntityDef(slug="integration_last_restart", name="Last Restart", ha_type="sensor", state_group="integration", stat_path="integration.last_restart", device_class="timestamp", icon="mdi:restart", entity_category="diagnostic"),
    EntityDef(slug="integration_startup_time", name="Integration Startup Time", ha_type="sensor", state_group="integration", stat_path="integration.startup_time", device_class="timestamp", icon="mdi:clock-start", entity_category="diagnostic"),
    EntityDef(slug="device_serial_number", name="Serial Number", ha_type="sensor", state_group="device", stat_path="device.serial_number", icon="mdi:identifier", entity_category="diagnostic"),
    # Remote solar strings — disabled by default (hardware-conditional: requires aPowerBox)
    EntityDef(slug="solar_remote_pv1_w", name="Remote Solar PV1", ha_type="sensor", state_group="solar", stat_path="solar_hardware.remote_solar_pv1", unit="W", device_class="power", icon="mdi:solar-panel", state_class="measurement", hw_requires="apbox"),
    EntityDef(slug="solar_remote_pv2_w", name="Remote Solar PV2", ha_type="sensor", state_group="solar", stat_path="solar_hardware.remote_solar_pv2", unit="W", device_class="power", icon="mdi:solar-panel", state_class="measurement", hw_requires="apbox"),
    
    # MPPT Solar Auto-Detection (Feature P2)
    EntityDef(slug="mppt_active_power_w", name="MPPT Active Power", ha_type="sensor", state_group="solar", stat_path="solar_hardware.mppt_active_power", unit="W", device_class="power", icon="mdi:solar-power", state_class="measurement", hw_requires="solar"),
    EntityDef(slug="mppt_status", name="MPPT Status", ha_type="sensor", state_group="status", stat_path="solar_hardware.mppt_status", icon="mdi:solar-power", entity_category="diagnostic", hw_requires="solar"),

    # Circuit 3 Power — gated by smart_circuit_3 flag (suppressed on AU which has 2 circuits)
    EntityDef(slug="smart_circuit_3_power_kw", name="Circuit 3 Power", ha_type="sensor", state_group="smart_circuits", stat_path="smart_circuit_3_power", unit="kW", device_class="power", icon="mdi:ev-station", state_class="measurement", hw_requires="smart_circuit_3"),
    EntityDef(slug="smart_circuit_3_energy_kwh", name="Circuit 3 Daily Energy", ha_type="sensor", state_group="smart_circuits", stat_path="smart_circuit_3_energy", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:ev-station", hw_requires="smart_circuit_3"),
    # Voltage sensor — from grid/inverter data
    EntityDef(slug="inverter_voltage_v", name="Voltage", ha_type="sensor", state_group="status", stat_path="grid_voltage", unit="V", device_class="voltage", icon="mdi:lightning-bolt", state_class="measurement"),

    # === SPRINT 4b — REMAINING FEM STATUS PARITY (7 entities) ===

    # Battery run status (derived from power flow direction, FEM: battery.derived_state)
    EntityDef(slug="status_battery_status", name="Battery Run Status", ha_type="sensor", state_group="status", stat_path="status.battery_status", icon="mdi:battery-sync", device_class="enum", options=["Charging", "Discharging", "Standby"]),

    # Dispatch execution status — read-only sensor showing live dispatch state (FEM: dispatch_status)
    # Distinct from dispatch_action (control select). Derived from active dispatch state.
    EntityDef(slug="status_dispatch_status", name="Dispatch Status", ha_type="sensor", state_group="status", stat_path="status.dispatch_execution_status", icon="mdi:lightning-bolt-circle", device_class="enum", options=["Idle", "Charging", "Discharging", "Stopping", "Error"]),

    # Grid connection state — diagnostic read-only sensor matching FEM (Disconnected/Connected/Available)
    # Changed from select→sensor: HA requires command_topic for selects; this is display-only.
    EntityDef(slug="grid_connection_state", name="Grid Connection Status", ha_type="sensor", state_group="status", stat_path="status.grid_connection_state_label", icon="mdi:transmission-tower", entity_category="diagnostic", device_class="enum", options=["Disconnected", "Connected", "Available"]),

    # Control source — FHAI is always Cloud; field present for parity
    EntityDef(slug="status_control_source", name="Control Source", ha_type="sensor", state_group="status", stat_path="status.control_source", icon="mdi:swap-horizontal-variant", entity_category="diagnostic"),

    # Grid compliance profile name (from device_info.gridProfileName, cached)
    EntityDef(slug="status_grid_profile", name="Grid Compliance Profile", ha_type="sensor", state_group="status", stat_path="status.grid_profile", icon="mdi:transmission-tower", entity_category="diagnostic"),

    # TOU current dispatch name + remaining (FEM: status/current_dispatch, status/dispatch_remaining)
    EntityDef(slug="status_current_dispatch", name="TOU Dispatch Now", ha_type="sensor", state_group="status", stat_path="status.current_dispatch", icon="mdi:calendar-clock"),
    EntityDef(slug="status_dispatch_remaining", name="TOU Dispatch Remaining", ha_type="sensor", state_group="status", stat_path="status.dispatch_remaining", icon="mdi:timer-sand"),

    # TOU next dispatch block (name + start/end times)
    EntityDef(slug="status_next_dispatch_name", name="TOU Dispatch Next", ha_type="sensor", state_group="status", stat_path="status.next_dispatch_name", icon="mdi:calendar-arrow-right"),
    EntityDef(slug="status_next_dispatch_start", name="TOU Dispatch Next Start", ha_type="sensor", state_group="status", stat_path="status.next_dispatch_start", icon="mdi:calendar-arrow-right"),
    EntityDef(slug="status_next_dispatch_end", name="TOU Dispatch Next End", ha_type="sensor", state_group="status", stat_path="status.next_dispatch_end", icon="mdi:calendar-arrow-left"),

    # TOU tariff tier for current and next dispatch block (waveType enum label)
    # Values: Off-Peak | Mid-Peak | On-Peak | Super Off-Peak | "" (not in TOU mode)
    EntityDef(slug="status_dispatch_tariff_now", name="TOU Dispatch Tariff Now", ha_type="sensor", state_group="status", stat_path="status.dispatch_tariff_now", icon="mdi:tag-text-outline", device_class="enum", options=["Off-Peak", "Mid-Peak", "On-Peak", "Super Off-Peak"]),
    EntityDef(slug="status_dispatch_tariff_next", name="TOU Dispatch Tariff Next", ha_type="sensor", state_group="status", stat_path="status.dispatch_tariff_next", icon="mdi:tag-text-outline", device_class="enum", options=["Off-Peak", "Mid-Peak", "On-Peak", "Super Off-Peak"]),

    # Integration duration in seconds since startup (FEM: device_class=duration, unit=s)
    EntityDef(slug="integration_startup_duration_s", name="Startup Time", ha_type="sensor", state_group="integration", stat_path="integration.startup_time_s", unit="s", device_class="duration", icon="mdi:timer-outline", state_class="measurement", entity_category="diagnostic"),

    # MQTT publisher status — always "online" when app is running
    EntityDef(slug="status_mqtt", name="MQTT Status", ha_type="sensor", state_group="integration", stat_path="integration.mqtt_status", icon="mdi:check-network", entity_category="diagnostic"),
]

# ---------------------------------------------------------------------------
# BATTERY ACCESSORY ENTITY REGISTRY (per aPower unit, linked via via_device)
# ---------------------------------------------------------------------------

BATTERY_ACCESSORY_ENTITIES: list[EntityDef] = [
    EntityDef(
        slug="soc",
        name="State of Charge",
        ha_type="sensor",
        state_group="battery",
        stat_path="bms.soc",
        unit="%",
        device_class="battery",
        state_class="measurement",
        icon="mdi:battery",
    ),
    EntityDef(
        slug="soh",
        name="State of Health",
        ha_type="sensor",
        state_group="battery",
        stat_path="bms.soh",
        unit="%",
        state_class="measurement",
        icon="mdi:battery-heart",
    ),
    EntityDef(
        slug="voltage",
        name="Voltage",
        ha_type="sensor",
        state_group="battery",
        stat_path="bms.voltage",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        icon="mdi:lightning-bolt",
    ),
    EntityDef(
        slug="current_a",
        name="Current",
        ha_type="sensor",
        state_group="battery",
        stat_path="bms.current",
        unit="A",
        device_class="current",
        state_class="measurement",
        icon="mdi:current-dc",
    ),
    EntityDef(
        slug="temp_c",
        name="Temperature",
        ha_type="sensor",
        state_group="battery",
        stat_path="bms.temp",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        icon="mdi:thermometer",
    ),
    EntityDef(slug="battery_heater_running", name="Battery Heater", ha_type="binary_sensor", state_group="battery", stat_path="battery.heater_state", device_class="heat", icon="mdi:heating-coil"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The hardware gates that actually control entities, and the profile key each
#: reads. Exposed so the UI can show what is gated, what the profile says, and
#: what was decided — rather than entities simply not existing.
HW_GATES: dict[str, str] = {
    "solar": "has_solar",
    "generator": "has_generator",
    "smart_circuits": "has_smart_circuits",
    "apbox": "has_apbox",
}

#: What a user override may say. "auto" defers to the profile.
GATE_AUTO, GATE_SHOW, GATE_HIDE = "auto", "show", "hide"


def gate_decision(gate: str, profile_dict: dict, overrides: dict | None = None) -> dict:
    """What this gate resolves to, and why.

    Returned rather than merely applied, because a gate that silently removes
    entities is indistinguishable from a bug — a site generating 4.35 kW of
    solar had no Solar Power sensor and nothing said so anywhere.
    """
    override = (overrides or {}).get(gate, GATE_AUTO)
    key = HW_GATES.get(gate, "")
    reported = profile_dict.get(key) if key else None

    if override == GATE_SHOW:
        return {"gate": gate, "key": key, "reported": reported,
                "override": override, "published": True,
                "reason": "forced on by you"}
    if override == GATE_HIDE:
        return {"gate": gate, "key": key, "reported": reported,
                "override": override, "published": False,
                "reason": "forced off by you"}

    if reported is None:
        return {"gate": gate, "key": key, "reported": None,
                "override": GATE_AUTO, "published": True,
                "reason": "not reported by the gateway — shown, because unknown hides nothing"}
    return {"gate": gate, "key": key, "reported": bool(reported),
            "override": GATE_AUTO, "published": bool(reported),
            "reason": f"gateway reports {key}={bool(reported)}"}


def get_entities_for_profile(profile_dict: dict, overrides: dict | None = None) -> list[EntityDef]:
    """
    Return only the entities whose hw_requires flag is satisfied
    by the given GatewayProfile dict, subject to any user overrides.
    """
    _ov = overrides or {}

    def _gated(gate: str) -> bool:
        return gate_decision(gate, profile_dict, _ov)["published"]

    def _flag(key: str) -> bool:
        """Whether a hardware flag permits publishing.

        Missing means *unknown*, not absent, and unknown must hide nothing —
        only an explicit False hides. Defaulting these to False meant a profile
        that simply never carried `has_solar` published no solar entities at
        all, so a site generating 4 kW had no Solar Power sensor and nothing to
        put in the Energy Dashboard. No `has_*` key was present at all on the
        profile where this was found.

        Hiding a control the hardware lacks is a tidiness win; hiding a
        measurement the hardware is producing is data loss.
        """
        value = profile_dict.get(key)
        return True if value is None else bool(value)

    circuit_count = profile_dict.get("smart_circuit_count", 0)
    has_circuits = _gated("smart_circuits")
    sw_merge = profile_dict.get("sw_merge", False)  # US SwMerge: circuits 1+2 merged → suppress circuit 2
    hw_map = {
        "solar": _gated("solar"),
        "generator": _gated("generator"),
        "smart_circuits": has_circuits,
        # Per-slot circuit flags:
        #   smart_circuit_2: present if ≥2 circuits AND not SwMerge
        #   smart_circuit_3: present only if 3 circuits reported (US hardware)
        "smart_circuit_2": has_circuits and (circuit_count >= 2 or circuit_count == 0) and not sw_merge,
        "smart_circuit_3": has_circuits and circuit_count == 3,
        "v2l": _flag("has_v2l"),
        "apbox": _gated("apbox"),
        "multi_battery": _flag("has_multi_battery"),
    }
    return [e for e in AGATE_ENTITIES if not e.hw_requires or hw_map.get(e.hw_requires, False)]



def extract_stat(data: dict, path: str):
    """
    Extract a value from a nested dict using dot-notation path.
    Returns None if any key in the path is missing.
    """
    if path in data:
        return data[path]

    # Phase 108: Graceful fallback for Flattened `_normalise_stats` Matrix
    bare_key = path.split(".")[-1]
    if bare_key in data:
        return data[bare_key]

    parts = path.split(".")
    val = data
    for part in parts:
        if not isinstance(val, dict):
            return None
        val = val.get(part)
        if val is None:
            return None
    return val
