"""
SQLite database service — aiosqlite, short_id-keyed schema.

Tables:
  gateways            — registered aGate devices (short_id = serial[-8:])
  batteries           — aPower battery units linked to gateways
  gateway_metrics     — rolling Tier D telemetry (7-day TTL)
  api_performance     — Cloud API call latency tracking
  app_config          — key/value app configuration store
  startup_log         — boot lifecycle events
  device_models       — FranklinWH hardware model catalog (aGate + aPower)
  device_accessories  — FranklinWH accessory catalog (SC, Gen, aPBox, aHub, MAC-1, Split-CT)
"""
import json
from datetime import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Smart Dispatch mode for a gateway nobody has configured yet. "info" evaluates
# and reports but issues no hardware commands.
#
# This existed in two places that disagreed. The column was added by migration
# v10 with DEFAULT 'active', while get_smart_dispatch_config's base_config said
# "info", commented "safe default — signal only, no hardware commands". Which
# one applied depended on whether a row had been written: a gateway with no row
# read back "info", and the moment anything created one — saving an unrelated
# setting was enough — it silently became "active" and started commanding
# hardware. The more dangerous value won by default.
SAFE_DEFAULT_STRATEGY_MODE = "info"

SCHEMA_VERSION = 62

CREATE_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;


CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gateways (
    short_id              TEXT PRIMARY KEY,
    full_serial           TEXT UNIQUE,
    name                  TEXT,
    site_id               TEXT,
    -- The site's own name and address, as discovery reports them. Only the id
    -- was ever stored, so the dashboard could render nothing but "Site 3447"
    -- above "SITE ID: 3447" — the same number twice, and a number no user
    -- recognises. The template already preferred a name; nothing supplied one.
    site_name             TEXT,
    site_address          TEXT,
    model                 TEXT,
    profile_json          TEXT,
    credentials_json      TEXT,
    settings_json         TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    last_seen             TEXT,
    -- Ph-4: Grid topology fields (installer-configurable)
    service_amps          INTEGER DEFAULT NULL,   -- Installer derating (AU: ≤100A, US: ≤200A)
    grid_type             TEXT    DEFAULT NULL,   -- single_phase|split_phase_240|split_phase_208|three_phase_230|three_phase_415
    gateway_phase         TEXT    DEFAULT NULL,   -- L1|L2|L3|split (multi-aGate 3-phase)
    three_phase_group_id  TEXT    DEFAULT NULL,   -- Groups aGates on same 3-phase site
    created_at            TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batteries (
    short_id        TEXT PRIMARY KEY,           -- Last 8 chars of aPower serial
    full_serial     TEXT UNIQUE,
    agate_short_id  TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
    rated_kw        REAL,
    rated_kwh       REAL,
    slot_index      INTEGER,                    -- 1, 2, 3
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gateway_solar_sources (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    gateway_id    TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
    source_type   TEXT NOT NULL DEFAULT 'pv_port_1'
                  CHECK(source_type IN (
                      'pv_port','pv_port_1','pv_port_2',
                      'dc_coupled','mppt_1','mppt_2',
                      'remote_pv','remote_pv_1','remote_pv_2',
                      'ahub_pv_1','ahub_pv_2',
                      'apbox_pv_1','apbox_pv_2',
                      'split_ct'
                  )),
    port          INTEGER DEFAULT NULL,
    accessory_id  TEXT    DEFAULT NULL,
    kwp           REAL    NOT NULL DEFAULT 0.0,
    label         TEXT    DEFAULT NULL,
    source_name   TEXT    DEFAULT NULL,
    utility_service_id TEXT DEFAULT NULL,
    brand         TEXT    DEFAULT NULL,
    inverter_type TEXT    DEFAULT NULL,          -- string|micro|optimiser
    phase_count   INTEGER DEFAULT 1,             -- 1 or 3
    ac_voltage    INTEGER DEFAULT NULL,          -- 110|208|230|240
    ac_hz         INTEGER DEFAULT NULL,          -- 50|60
    pv_control    INTEGER DEFAULT 0,
    pv_control_type TEXT  DEFAULT NULL,          -- production_switch|curtailment|ha_entity
    pv_control_entity TEXT DEFAULT NULL,
    -- Ph-2: Amperage safety
    max_amps      INTEGER DEFAULT 63,            -- AU solar breaker limit (63A default)
    -- Ph-3: Three-phase metering topology
    solar_metering_mode TEXT DEFAULT 'single_phase_internal'
                  CHECK(solar_metering_mode IN (
                      'single_phase_internal','three_phase_ct_kit','split_ct_external','rs485_meter'
                  )),
    -- Ph-6: FranklinWH Installer App capability toggles
    off_grid_capable INTEGER DEFAULT 0,
    pv_data_api      INTEGER DEFAULT 0,
    detected_by   TEXT    NOT NULL DEFAULT 'manual'
                  CHECK(detected_by IN ('discover','manual')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_solar_sources_gw ON gateway_solar_sources(gateway_id);

CREATE TABLE IF NOT EXISTS gateway_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id    TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    data_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_short_id_ts ON gateway_metrics(short_id, timestamp);

CREATE TABLE IF NOT EXISTS api_performance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id    TEXT,
    endpoint    TEXT,
    latency_ms  INTEGER,
    status      TEXT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_api_perf_ts ON api_performance(timestamp);

CREATE TABLE IF NOT EXISTS api_edge_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id      TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
    timestamp     TEXT NOT NULL DEFAULT (datetime('now')),
    metrics_json  TEXT NOT NULL,
    edge_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_edge_metrics_ts ON api_edge_metrics(timestamp);

CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS startup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    boot_at      TEXT NOT NULL DEFAULT (datetime('now')),
    environment  TEXT,
    phase        INTEGER,
    details_json TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS gateway_credentials (
    serial        TEXT PRIMARY KEY,             -- full_serial of the gateway
    email         TEXT NOT NULL,
    password      TEXT NOT NULL,                -- plaintext (SQLite local store)
    validated_at  TEXT,                         -- ISO ts of last successful auth
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT,
    source        TEXT DEFAULT 'ui'             -- 'ui' | 'ini' | 'env'
);

CREATE TABLE IF NOT EXISTS credential_audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    serial  TEXT,
    event   TEXT,   -- 'created'|'updated'|'validated'|'failed'|'deleted'
    source  TEXT,   -- 'ui'|'ini'|'api'
    detail  TEXT,
    ts      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT,   -- e.g. 'backup_completed', 'task_restarted'
    source    TEXT,   -- 'system', 'ui', 'api'
    user      TEXT,   -- e.g. 'system'
    details   TEXT,   -- JSON or string details
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bms_sessions (
    session_id TEXT PRIMARY KEY,
    short_id TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
    battery_sn TEXT NOT NULL,
    session_name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         TEXT NOT NULL,
    rule_name       TEXT,
    gateway_serial  TEXT,
    action_type     TEXT,
    action_payload  TEXT,
    status          TEXT,
    detail          TEXT,
    source          TEXT DEFAULT 'edge',        -- 'amber'|'edge' for filtering
    timestamp       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_automation_history_ts ON automation_history(timestamp);

CREATE TABLE IF NOT EXISTS automation_notification_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    direction   TEXT,   -- 'SENT', 'RECEIVED', 'ERROR'
    event       TEXT,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_log_ts ON automation_notification_log(timestamp);

CREATE TABLE IF NOT EXISTS automation_state (
    rule_id         TEXT NOT NULL,
    gateway_serial  TEXT NOT NULL,
    last_true_ts    INTEGER,
    duration_secs   INTEGER,
    PRIMARY KEY (rule_id, gateway_serial)
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    request_id      TEXT PRIMARY KEY,
    gateway_serial  TEXT NOT NULL,
    rule_id         TEXT,
    rule_name       TEXT,
    action          TEXT,
    dispatch_summary TEXT,
    expires_at      INTEGER,
    no_reply_action TEXT DEFAULT 'skip',
    action_context  TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

-- ── Dynamic Pricing ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_config (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL DEFAULT 'flat',
    region       TEXT DEFAULT 'AU',
    enabled      INTEGER DEFAULT 0,
    credentials  TEXT DEFAULT '{}',   -- JSON: api_token, api_key, partner_id, nmi_id, site_id …
    settings     TEXT DEFAULT '{}',   -- JSON: poll_interval_secs, import_c_kwh, export_c_kwh …
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pricing_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    utility_service_id TEXT, -- BL-010: Link to utility_services.id
    provider       TEXT NOT NULL,
    timestamp      TEXT NOT NULL DEFAULT (datetime('now')),
    import_c_kwh   REAL,
    export_c_kwh   REAL,
    demand_window  INTEGER DEFAULT 0,
    solar_bonus    REAL,
    tariff_type    TEXT DEFAULT 'UNKNOWN',
    renewables_pct INTEGER,
    spike_status   TEXT DEFAULT 'NONE',
    interval_min   INTEGER DEFAULT 30,
    valid_until    TEXT,
    forecast_json  TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_pricing_ts ON pricing_snapshots(timestamp);

-- ── Utility Provider & Bill Reconciliation ───────────────────────
CREATE TABLE IF NOT EXISTS utility_config (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Utility / Retailer identity
    utility_name        TEXT,           -- e.g. "Origin Energy"
    account_number      TEXT,
    nmi_id              TEXT,           -- National Meter Identifier
    meter_serial        TEXT,
    meter_type          TEXT,           -- 'smart'|'interval'|'accumulation'
    -- Connection / Service details
    service_type        TEXT,           -- 'residential'|'commercial'|'industrial'
    ac_type             TEXT,           -- 'single_phase'|'three_phase'
    voltage_v           INTEGER,        -- e.g. 230, 415
    max_demand_kva      REAL,
    network_area        TEXT,           -- e.g. "Ausgrid", "Energex"
    -- Bill cycle
    bill_frequency      TEXT DEFAULT 'quarterly', -- 'monthly'|'quarterly'|'bimonthly'
    bill_start_day      INTEGER DEFAULT 1,         -- day of month billing starts
    bill_period_days    INTEGER,                   -- override if non-standard
    -- Fixed charges (per billing period)
    supply_charge_day   REAL DEFAULT 0,  -- c/day supply charge (all rate fields are cents)
    metering_fee        REAL DEFAULT 0,  -- $ per period metering fee
    network_fixed_fee   REAL DEFAULT 0,  -- $ per period network service fee
    -- Demand charges
    demand_charge_kw    REAL DEFAULT 0,  -- $/kW/month peak demand charge
    demand_window_start TEXT,            -- HH:MM local time
    demand_window_end   TEXT,            -- HH:MM local time
    demand_window_days  TEXT DEFAULT 'weekdays', -- 'all'|'weekdays'|'weekends'
    -- Solar / Feed-in
    fit_rate_c_kwh      REAL,            -- feed-in tariff rate ¢/kWh
    fit_provider        TEXT,            -- e.g. "Origin Solar Bonus Scheme"
    -- Notes
    notes               TEXT,
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- ── Decoupled Pricing Models ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS pricing_models (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    credentials  TEXT DEFAULT '{}',
    settings     TEXT DEFAULT '{}',
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- ── Multi-Tenant Site/Utility Services (BL-010) ───────────────────
CREATE TABLE IF NOT EXISTS utility_services (
    id                  TEXT PRIMARY KEY,
    name                TEXT,
    retailer_name       TEXT,
    account_number      TEXT,
    nmi                 TEXT,
    meter_serial        TEXT,
    meter_type          TEXT,
    service_amps        INTEGER,
    pricing_provider    TEXT NOT NULL DEFAULT 'flat',
    pricing_credentials TEXT DEFAULT '{}',
    pricing_settings    TEXT DEFAULT '{}',
    pricing_model_id    TEXT REFERENCES pricing_models(id) DEFAULT 'franklinwh_tou',
    bill_frequency      TEXT DEFAULT 'quarterly',
    bill_start_day      INTEGER DEFAULT 1,
    bill_period_days    INTEGER,
    supply_charge_day   REAL DEFAULT 0,
    metering_fee        REAL DEFAULT 0,
    network_fixed_fee   REAL DEFAULT 0,
    demand_charge_kw    REAL DEFAULT 0,
    demand_window_start TEXT,
    demand_window_end   TEXT,
    demand_window_days  TEXT DEFAULT 'weekdays',
    fit_rate_c_kwh      REAL,
    fit_scheme_name     TEXT,
    notes               TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agate_utility_links (
    gateway_short_id   TEXT PRIMARY KEY REFERENCES gateways(short_id) ON DELETE CASCADE,
    utility_service_id TEXT NOT NULL REFERENCES utility_services(id) ON DELETE CASCADE,
    updated_at         TEXT DEFAULT (datetime('now'))
);

-- ── FranklinWH Device Registry ────────────────────────────────────
-- hw_version_int is globally unique and API-native:
--   aGate:  sysHdVersionInt from getHomeGatewayList / getDeviceInfoV2
--   aPower: peHwVersion from getPowerCapConfigList
-- Ranges do not currently collide (aPower: 0-6, aGate: 100+).
CREATE TABLE IF NOT EXISTS device_models (
    hw_version_int     INTEGER PRIMARY KEY,
    device_class       TEXT NOT NULL,        -- 'agate' | 'apower'
    api_field_name     TEXT,                 -- 'sysHdVersionInt' | 'peHwVersion'
    real_hw_version    TEXT,                 -- e.g. 'FHP1.2' from realSysHdVersion
    name               TEXT NOT NULL,        -- 'aGate X'
    sku                TEXT,                 -- 'AGT-R1V1-AU'
    model              TEXT,                 -- 'aGate X-01-AU'
    country_id         INTEGER,              -- 1=CN 2=US 3=AU NULL=global
    generation         INTEGER,              -- 1 or 2 (aGate only)
    has_mppt           INTEGER DEFAULT 0,    -- 1 for aPower S
    type               TEXT,                 -- 'standard'|'high_capacity'|'stackable'
    notes              TEXT,
    is_deprecated      INTEGER DEFAULT 0,
    deprecated_since   TEXT,
    successor_hw       INTEGER,              -- hw_version_int of replacement model
    deprecated_note    TEXT,
    user_override_name TEXT,                 -- admin-editable display name override
    user_note          TEXT,
    source             TEXT DEFAULT 'seed',  -- 'seed' | 'user'
    updated_at         TEXT DEFAULT (datetime('now'))
);

-- accessory_id is an FHAI-internal convention (200/300-series).
-- The API returns only api_accessory_type (3=generator, 4=smart_circuits).
-- Accessories detected via feature flags have api_accessory_type=NULL.
CREATE TABLE IF NOT EXISTS device_accessories (
    accessory_id        INTEGER PRIMARY KEY,
    api_accessory_type  INTEGER,             -- API int: 3=generator 4=smart_circuits NULL=flag-detected
    name                TEXT NOT NULL,       -- 'Smart Circuits V1-AU'
    sku                 TEXT,
    accessory_type      TEXT NOT NULL,       -- 'smart_circuits'|'generator'|'apbox'|'ahub'|'mac1'|'split_ct'
    version             INTEGER,
    country_id          INTEGER,             -- 1=CN 2=US 3=AU NULL=global
    compatible_agates   TEXT,               -- JSON array '[100,101]' or literal 'ALL'
    compatible_apower   TEXT,               -- JSON array '[4,5]' or 'ALL'
    circuit_count       INTEGER,
    v2l_port            INTEGER DEFAULT 0,  -- has physical V2L port
    v2l_enables         INTEGER DEFAULT 0,  -- generator enables V2L on SC V1
    v2l_requires_gen    INTEGER DEFAULT 0,  -- SC V2L needs generator module
    notes               TEXT,
    is_deprecated       INTEGER DEFAULT 0,
    user_note           TEXT,
    source              TEXT DEFAULT 'seed',
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- ── TOU Schedule Snapshots (Audit Trail) ────────────────────────────────
-- Append-only log: each Save-to-Gateway push writes a row.
-- Enables "Push History" panel and one-click restore in the pricing modal.
CREATE TABLE IF NOT EXISTS tou_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id        TEXT NOT NULL,              -- aGate short_id
    label           TEXT NOT NULL,              -- human label, e.g. "Manual save 2026-04-17 06:01"
    strategy_json   TEXT NOT NULL,              -- full strategyList JSON blob
    season_count    INTEGER DEFAULT 1,          -- number of seasons in snapshot
    source          TEXT DEFAULT 'gateway_save',-- 'gateway_save' | 'restore' | 'manual'
    ts              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tou_snapshots_gw_ts ON tou_snapshots(short_id, ts);

-- ── Automation Rulebooks (B1 — Dynamic Tariff Runbooks) ──────────────────────
-- Named, provider-linked collections of dispatch rules.
-- One rulebook is active at a time per provider.
-- is_system=1 rows are shipped defaults: user-editable but not deletable.
CREATE TABLE IF NOT EXISTS automation_rulebooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rulebook_id     TEXT NOT NULL UNIQUE,       -- UUID slug, e.g. "amber-au-default"
    name            TEXT NOT NULL,              -- e.g. "Amber Electric — Default AU"
    provider        TEXT NOT NULL DEFAULT 'amber',
    description     TEXT,
    is_active       INTEGER DEFAULT 1,          -- 0 = globally disabled
    is_system       INTEGER DEFAULT 0,          -- 1 = shipped default, cannot be deleted
    engine_mode     TEXT DEFAULT 'signal_only', -- 'signal_only'|'active'|'paused'
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ── Automation Notification Settings ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_notification_settings (
    id              INTEGER PRIMARY KEY DEFAULT 1,  -- singleton row
    enabled         INTEGER DEFAULT 0,              -- global on/off
    ha_target       TEXT    DEFAULT '',             -- e.g. 'mobile_app_david_iphone'
    triggers        TEXT    DEFAULT '[]',           -- JSON: ["spike","force_charge",...]
    actionable      INTEGER DEFAULT 0,              -- Tier 3 actionable notifications
    actionable_ttl  INTEGER DEFAULT 1800,           -- seconds before actionable expires
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ── Automation Rules ─────────────────────────────────────────────────────────
-- One row per dispatch rule. Evaluated in ascending priority order.
-- Condition DSL stored as JSON. See smart_dispatch.py for evaluator.
CREATE TABLE IF NOT EXISTS automation_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rulebook_id     TEXT NOT NULL REFERENCES automation_rulebooks(rulebook_id) ON DELETE CASCADE,
    rule_id         TEXT NOT NULL UNIQUE,       -- UUID slug
    name            TEXT NOT NULL,
    description     TEXT,
    priority        INTEGER DEFAULT 100,        -- lower = evaluated first
    enabled         INTEGER DEFAULT 1,
    -- Trigger condition (JSON DSL; {} = always true / catch-all)
    condition_json  TEXT NOT NULL DEFAULT '{}',
    -- Action when condition is true
    action          TEXT NOT NULL,              -- GRID_CHARGE|GRID_EXPORT|HOLD|RESUME_TOU
    -- Action parameters (JSON)
    action_params   TEXT DEFAULT '{}',          -- soc_min, soc_max, duration_min, etc.
    -- Don't re-trigger within N minutes of last fire
    cooldown_min    INTEGER DEFAULT 30,
    -- NULL = all providers, 'amber' = only when provider matches
    provider_scope  TEXT,
    -- NULL = all gateways, else comma-separated short_ids
    gateway_scope   TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rules_rulebook_prio ON automation_rules(rulebook_id, priority);

-- ── Smart Dispatch Engine Configuration ───────────────────────────────────────────
-- One row per gateway (aGate short_id) — generic engine parameters and notification preferences.
CREATE TABLE IF NOT EXISTS smart_dispatch_config (
    gateway_id                TEXT PRIMARY KEY,
    min_soc                   REAL DEFAULT 20.0,
    max_soc                   REAL DEFAULT 90.0,
    max_charge_price          REAL DEFAULT 0.0,
    min_export_price          REAL DEFAULT 0.0,
    export_bonus_threshold    REAL DEFAULT 5.0,
    charge_power_mode         TEXT DEFAULT 'default',
    charge_power_value        REAL DEFAULT 0.0,
    discharge_power_mode      TEXT DEFAULT 'default',
    discharge_power_value     REAL DEFAULT 0.0,
    daily_earnings_target     REAL DEFAULT 0.0,
    monthly_earnings_target   REAL DEFAULT 0.0,
    solar_curtail_entity      TEXT DEFAULT NULL,
    enphase_enabled           INTEGER DEFAULT 0,
    enphase_host              TEXT DEFAULT NULL,
    enphase_user              TEXT DEFAULT 'installer',
    enphase_password          TEXT DEFAULT NULL,
    enphase_slew_rate         INTEGER DEFAULT 500,
    enphase_export_limit_w    INTEGER DEFAULT 0,
    allow_auto_offgrid        INTEGER DEFAULT 0,
    notification_mode         TEXT DEFAULT 'ask',
    notify_on_demand_charge   INTEGER DEFAULT 1,
    notify_on_negative_export INTEGER DEFAULT 1,
    notify_on_spike           INTEGER DEFAULT 1,
    notify_on_export_bonus    INTEGER DEFAULT 0,
    notify_on_earnings        INTEGER DEFAULT 0,
    notify_on_force_charge    INTEGER DEFAULT 1,
    info_notify_targets       TEXT DEFAULT '',
    -- Peak window SOC guard rails (used by AB condition Lookup values)
    min_peak_window_soc       REAL    DEFAULT 60.0,
    max_peak_window_soc       REAL    DEFAULT 90.0,
    weather_extreme_impact    INTEGER DEFAULT 0,
    site_has_high_loads       INTEGER DEFAULT 0,
    multi_utility_service     INTEGER DEFAULT 0,
    utility_export_limit_w    INTEGER DEFAULT 0,
    has_apbox_excess_solar    INTEGER DEFAULT 0,
    apower_s_mppt             INTEGER DEFAULT 0,
    strategy_priorities_json  TEXT    DEFAULT '["self_consumption", "peak_shaving", "export_exception", "battery_topup"]',
    last_full_generation_time TEXT    DEFAULT NULL,
    default_operating_mode    TEXT    DEFAULT 'gateway_default',
    baseline_tou_snapshot     TEXT    DEFAULT NULL,
    active_override_uuid      TEXT    DEFAULT NULL,
    active_override_expires_at TEXT   DEFAULT NULL,
    rampTime                  INTEGER DEFAULT 99,
    maxChargeSoc              INTEGER DEFAULT 100,
    minDischargeSoc           INTEGER DEFAULT 0,
    chargePower               INTEGER DEFAULT 5000,
    dischargePower            INTEGER DEFAULT 5000,
    updated_at                TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS smart_dispatch_schedules (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id                TEXT NOT NULL,
    period                    TEXT NOT NULL,
    start_time                TEXT NOT NULL,
    end_time                  TEXT NOT NULL,
    min_soc                   REAL NOT NULL,
    max_kw_percent            REAL DEFAULT NULL,
    action                    TEXT DEFAULT 'CHARGE',
    is_active                 INTEGER DEFAULT 1,
    created_at                TEXT DEFAULT (datetime('now')),
    updated_at                TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS engine_rules (
    gateway_id                TEXT,
    rule_id                   TEXT,
    rule_name                 TEXT,
    is_active                 INTEGER DEFAULT 1,
    priority                  INTEGER NOT NULL,
    config_json               TEXT,
    updated_at                TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (gateway_id, rule_id)
);

-- ── Amber Usage Cache (hourly earnings tracking) ────────────────────────────────
-- Populated hourly by earnings_tracker.py from AmberAdapter.get_usage().
-- cost column: negative value = credit/earning (feed-in), positive = charge.
-- Rolling 90-day window; older rows purged on each poll cycle.
CREATE TABLE IF NOT EXISTS amber_usage_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id  TEXT NOT NULL,               -- aGate short_id
    channel     TEXT NOT NULL,               -- 'general' | 'feed_in'
    start_time  TEXT NOT NULL,               -- ISO datetime
    end_time    TEXT NOT NULL,
    kwh         REAL NOT NULL DEFAULT 0.0,
    cost        REAL NOT NULL DEFAULT 0.0,   -- AUD; negative = feed-in credit (earnings)
    tariff_type TEXT,
    quality     TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(gateway_id, channel, start_time)
);
CREATE INDEX IF NOT EXISTS idx_amber_usage_gw_ts ON amber_usage_cache(gateway_id, start_time);

-- ── Amber Evaluation Log ────────────────────────────────────────────────────────────
-- Rolling log of engine decisions. Older rows purged when count exceeds 200 per gateway.
CREATE TABLE IF NOT EXISTS pricing_eval_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id        TEXT NOT NULL,
    trigger_category  TEXT NOT NULL,          -- demand_charge|negative_export|price_spike|
                                              --  export_bonus|earnings_target|force_charge|
                                              --  fallback|paused
    action            TEXT NOT NULL,          -- APPLY_PRESET|HOLD|RESUME_SC|PAUSED
    preset_name       TEXT,
    rule_name         TEXT,
    reason            TEXT,
    dispatch_summary  TEXT,                   -- human-readable plan shown in UI
    requires_approval INTEGER DEFAULT 0,      -- 1 = actionable notification was sent
    execution_status  TEXT DEFAULT 'signal',  -- signal|executed|approved|denied|auto
    import_c_kwh      REAL,
    export_c_kwh      REAL,
    soc_pct           REAL,
    spike_status      TEXT,
    demand_window     INTEGER DEFAULT 0,
    shadowed_rules_json TEXT DEFAULT '[]',
    ts                TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_amber_eval_gw_ts ON pricing_eval_log(gateway_id, ts);

-- ── Solar Forecast Configuration ──────────────────────────────────────────
-- Single row per install (site-wide, not gateway-scoped).
-- Solar forecast config applies globally; gateway_id kept for future multi-site.
CREATE TABLE IF NOT EXISTS solar_forecast_config (
    id                          INTEGER PRIMARY KEY DEFAULT 1,  -- singleton
    -- Source selection
    enabled                     INTEGER DEFAULT 0,
    source                      TEXT    DEFAULT 'auto',  -- auto|ha_entities|forecast_solar|solcast
    -- HA entity source
    ha_solar_actual_entity      TEXT    DEFAULT NULL,    -- e.g. sensor.franklinwh_solar_power
    ha_solar_forecast_entity    TEXT    DEFAULT NULL,    -- e.g. sensor.energy_production_today_remaining_2
    ha_solar_curtail_entity     TEXT    DEFAULT NULL,    -- e.g. switch.envoy_122204038186_production (moved from amber_engine_config.solar_ha_entity)
    

    
    -- Site location (for built-in providers)
    lat                         REAL    DEFAULT NULL,
    lng                         REAL    DEFAULT NULL,
    -- Panel installation specs
    azimuth                     REAL    DEFAULT 180.0,
    tilt                        REAL    DEFAULT 22.5,
    kwp                         REAL    DEFAULT 5.0,
    -- Forecast.Solar provider
    forecast_solar_api_key      TEXT    DEFAULT NULL,    -- NULL = free tier
    forecast_solar_rate_limit_mins INTEGER DEFAULT 30,
    -- Solcast provider
    solcast_api_key             TEXT    DEFAULT NULL,
    solcast_site_id             TEXT    DEFAULT NULL,    -- toolkit.solcast.com.au site ID
    -- SOC projection
    home_load_assumption_kw     REAL    DEFAULT 0.5,
    updated_at                  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS forecast_loads (
    id                TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    name              TEXT NOT NULL,
    slug              TEXT DEFAULT '',                  -- Stable per-gateway-unique key for AB namespace (v46)
    category          TEXT NOT NULL,
    ha_entity_id      TEXT,
    ha_energy_entity_id TEXT DEFAULT NULL,
    ha_switch_entity_id TEXT DEFAULT NULL,
    ha_binary_entity_id TEXT DEFAULT NULL,
    measurement_type  TEXT NOT NULL DEFAULT 'forecast', -- forecast | now
    peak_kw           REAL NOT NULL DEFAULT 0.0,
    avg_kw            REAL NOT NULL DEFAULT 0.0,
    schedule_json     TEXT NOT NULL DEFAULT '[]',       -- Array of TOU-like schedule periods
    enabled           INTEGER NOT NULL DEFAULT 1,
    gateway_id        TEXT DEFAULT 'global',
    dispatch_category TEXT DEFAULT '2-Essential Load',
    is_behind_meter   INTEGER DEFAULT 1,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

-- ── Smart Dispatch Strategy Matrix (v26) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS sd_strategy_matrix (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_order        INTEGER NOT NULL,
    gateway_id        TEXT NOT NULL DEFAULT 'all', -- short_id or 'all'
    strategy_name     TEXT,                        -- e.g. 'Amber Dynamic'
    enabled           INTEGER DEFAULT 1,
    system_immutable  INTEGER NOT NULL DEFAULT 0,  -- 1 = system safeguard rule, cannot be deleted
    trigger_category  TEXT DEFAULT 'custom',
    conditions_json   TEXT NOT NULL DEFAULT '{}',
    signals_json      TEXT NOT NULL DEFAULT '[]',
    forecast_weight   REAL DEFAULT 1.0,
    intent_duration_mins INTEGER DEFAULT NULL,
    updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sd_matrix_gw ON sd_strategy_matrix(gateway_id);

CREATE TABLE IF NOT EXISTS sd_actuator_map (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key        TEXT NOT NULL UNIQUE,        -- e.g. 'CURTAIL_SOLAR', 'LIMIT_PROD'
    actuator_type     TEXT NOT NULL,               -- 'ha_entity', 'ha_service', 'fwh_cloud'
    target            TEXT NOT NULL,               -- entity_id or service_name
    params_json       TEXT DEFAULT '{}',           -- static params
    updated_at        TEXT DEFAULT (datetime('now'))
);


CREATE TABLE IF NOT EXISTS utility_tariffs (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    retailer_name       TEXT,
    country             TEXT NOT NULL,
    model_compatibility INTEGER,
    flat_rate_import    REAL DEFAULT 0.0,
    flat_rate_export    REAL DEFAULT 0.0,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(model_compatibility) REFERENCES device_models(hw_version_int) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS utility_tariff_seasons (
    id                  TEXT PRIMARY KEY,
    tariff_id           TEXT NOT NULL REFERENCES utility_tariffs(id) ON DELETE CASCADE,
    season_name         TEXT NOT NULL,
    months              TEXT NOT NULL,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS utility_tariff_rates (
    id                  TEXT PRIMARY KEY,
    tariff_id           TEXT NOT NULL REFERENCES utility_tariffs(id) ON DELETE CASCADE,
    season_id           TEXT REFERENCES utility_tariff_seasons(id) ON DELETE CASCADE,
    rate_type           TEXT NOT NULL CHECK(rate_type IN ('import', 'export')),
    label               TEXT,
    start_time          TEXT NOT NULL,
    end_time            TEXT NOT NULL,
    day_type            TEXT DEFAULT 'all' CHECK(day_type IN ('all', 'weekdays', 'weekends')),
    rate_c_kwh          REAL NOT NULL,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sd_energy_devices (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,
    ha_power_entity     TEXT,
    ha_energy_entity    TEXT,
    ha_switch_entity    TEXT,
    peak_kw             REAL DEFAULT 0.0,
    avg_kw              REAL DEFAULT 0.0,
    schedule_json       TEXT DEFAULT '[]',
    enabled             INTEGER DEFAULT 1,
    gateway_id          TEXT DEFAULT 'global',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sd_security_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    unlocked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    session_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sd_forecast_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    plan_horizon_start TEXT NOT NULL,
    plan_horizon_end TEXT NOT NULL,
    plan_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT DEFAULT NULL,
    last_used_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS security_audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    event   TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT 'system',
    detail  TEXT,
    ts      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ha_instances (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    host TEXT NOT NULL,
    token TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    is_default INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_devices (
    id TEXT PRIMARY KEY,
    ha_instance_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    service_target TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    owner_username TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(ha_instance_id) REFERENCES ha_instances(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK(role IN ('admin', 'supervisor', 'control', 'user', 'guest', 'inkypi', 'inkpi', 'inkypi2')),
    dashboard      TEXT NOT NULL DEFAULT 'standard' CHECK(dashboard IN ('standard', 'inkypi', 'guest', 'inkypi2')),
    totp_secret    TEXT DEFAULT NULL,
    totp_enabled   INTEGER DEFAULT 0,
    must_change_pw INTEGER DEFAULT 0,
    email          TEXT DEFAULT NULL,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);
"""


# Module-level DB path (set during lifespan init)
_db_path: Optional[Path] = None


def set_db_path(path: Path) -> None:
    global _db_path
    _db_path = path


def get_db_path() -> Path:
    if _db_path is None:
        raise RuntimeError("DB path not initialised — call set_db_path() first")
    return _db_path


def retry_on_lock(max_retries: int = 5, base_delay: float = 0.05, max_delay: float = 1.0):
    """
    Decorator to retry async database functions if they fail due to SQLite lock contention.
    Uses exponential backoff with full jitter to prevent retry collisions.
    """
    import functools
    import random
    import sqlite3
    import asyncio

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    err_msg = str(exc).lower()
                    if "locked" in err_msg or "busy" in err_msg:
                        retries += 1
                        if retries > max_retries:
                            logger.error(f"Database lock retry limit reached ({max_retries}) for {func.__name__}: {exc}")
                            raise
                        # Exponential backoff with full jitter: delay = random(0, base_delay * 2^retries)
                        temp = min(max_delay, base_delay * (2 ** retries))
                        delay = random.uniform(0, temp)
                        logger.warning(
                            f"Database is locked/busy during {func.__name__}. "
                            f"Retrying ({retries}/{max_retries}) in {delay:.3f}s... Error: {exc}"
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Context manager: yield an open DB connection with WAL, synchronous NORMAL, and foreign keys."""
    async with aiosqlite.connect(get_db_path(), timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn


async def _migrate_v43_users_rebuild(conn) -> None:
    """Rebuild `users` with the widened role/dashboard CHECK constraints (v43).

    Extracted from init_db and made transactional. Two problems with the
    original inline version:

    1. It was unguarded, so it re-ran on every boot — confirmed live, 15
       rebuilds in one container's log.
    2. It was not in a transaction. A crash between `DROP TABLE users` and
       `ALTER TABLE users_new RENAME` leaves NO users table at all, i.e. every
       account gone. Combined with (1), that window was entered on every single
       startup.

    BEGIN IMMEDIATE takes the write lock up front, so the whole swap is atomic:
    a crash mid-way rolls back on next open and the original table survives.
    """
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users_new (
                username       TEXT PRIMARY KEY,
                password_hash  TEXT NOT NULL,
                role           TEXT NOT NULL CHECK(role IN ('admin', 'supervisor', 'control', 'user', 'guest', 'inkypi', 'inkpi', 'inkypi2')),
                dashboard      TEXT NOT NULL DEFAULT 'standard' CHECK(dashboard IN ('standard', 'inkypi', 'guest', 'inkypi2')),
                totp_secret    TEXT DEFAULT NULL,
                totp_enabled   INTEGER DEFAULT 0,
                must_change_pw INTEGER DEFAULT 0,
                email          TEXT DEFAULT NULL,
                created_at     TEXT DEFAULT (datetime('now')),
                updated_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        await conn.execute("""
            INSERT OR REPLACE INTO users_new (
                username, password_hash, role, dashboard, totp_secret,
                totp_enabled, must_change_pw, email, created_at, updated_at
            )
            SELECT
                username, password_hash, role, dashboard, totp_secret,
                totp_enabled, must_change_pw, email, created_at, updated_at
            FROM users
        """)

        # Do not swap in a table that lost rows. If the copy dropped accounts
        # (CHECK violation on a legacy value, say), abort and keep the original
        # — the old code would have committed the loss and logged a warning.
        async with conn.execute("SELECT COUNT(*) FROM users") as _c:
            _old_n = (await _c.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM users_new") as _c:
            _new_n = (await _c.fetchone())[0]
        if _new_n < _old_n:
            raise RuntimeError(
                f"refusing swap — copy lost rows ({_old_n} -> {_new_n}); original kept"
            )

        await conn.execute("DROP TABLE users")
        await conn.execute("ALTER TABLE users_new RENAME TO users")
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (43,)
        )
        await conn.commit()
        logger.info(
            f"DB migration v43: users table rebuilt ({_new_n} accounts preserved), "
            f"schema version 43 recorded"
        )
    except Exception as _e:
        try:
            await conn.rollback()
        except Exception:
            pass
        logger.warning(f"DB migration v43 failed (rolled back, users intact): {_e}")


# Legacy role -> enforced role. Mirrors authz.LEGACY_ROLE_MAP; kept separate so
# db.py has no import dependency on the middleware.
#
# `supervisor` -> admin is deliberate. It was labelled "Limited Admin" in the UI
# but never enforced anywhere, so those accounts have had full access for their
# whole existence. Demoting them the moment enforcement arrives would be a
# surprise, not a fix. `control` -> operator is the one role whose documented
# intent (drive the battery, touch nothing else) matches a tier exactly.
V56_ROLE_MAP: dict[str, str] = {
    "admin": "admin",
    "supervisor": "admin",
    "control": "operator",
    "operator": "operator",
    "user": "viewer",
    "guest": "viewer",
    "viewer": "viewer",
    "inkypi": "viewer",
    "inkpi": "viewer",
    "inkypi2": "viewer",
}


async def _migrate_v56_role_collapse(conn) -> None:
    """Collapse the 8 legacy role values to admin / operator / viewer (v56).

    The old set was aspirational: `supervisor`, `control`, `user` and `guest`
    were stored but never checked, so every account had full access. Stage 1
    added router-level tiers; this makes the stored values mean something.

    Also adds `api_tokens.role`. Every long-lived API token was hardcoded to
    admin in middleware/auth.py — the single worst privilege escalation in the
    codebase, since a token minted for an e-ink display could do anything.

    Same safety posture as _migrate_v43_users_rebuild: version-guarded by the
    caller, BEGIN IMMEDIATE so the DROP/RENAME swap is atomic, and a row-count
    check before the swap. Plus a lockout guard — a database with no admin
    after this runs is unusable, so that is treated as a hard failure to
    repair rather than a state to commit.
    """
    import json as _json

    try:
        await conn.execute("BEGIN IMMEDIATE")

        # ── api_tokens.role (additive — no rebuild needed) ──────────────────
        # NOTE: init_db opens its own aiosqlite connection WITHOUT row_factory
        # (unlike get_db()), so every row here is a plain tuple. Index access
        # only — r["col"] raises "tuple indices must be integers".
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(api_tokens)")).fetchall()]
        if "role" not in cols:
            await conn.execute("ALTER TABLE api_tokens ADD COLUMN role TEXT DEFAULT 'viewer'")
            # Existing tokens backfill to admin: they were minted when every
            # token WAS admin, and a live HA REST sensor or e-ink display would
            # break if silently demoted. New tokens default to viewer.
            await conn.execute("UPDATE api_tokens SET role = 'admin' WHERE role IS NULL OR role = 'viewer'")
            logger.info("DB migration v56: api_tokens.role added (existing tokens kept as admin)")

        # ── Rebuild users with the narrowed CHECK ───────────────────────────
        # Roles are mapped DURING the copy, never in place on the old table:
        # the old CHECK does not permit 'operator' or 'viewer', so an in-place
        # UPDATE fails the constraint and aborts the whole migration.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users_v56 (
                username       TEXT PRIMARY KEY,
                password_hash  TEXT NOT NULL,
                role           TEXT NOT NULL CHECK(role IN ('admin', 'operator', 'viewer')),
                dashboard      TEXT NOT NULL DEFAULT 'standard' CHECK(dashboard IN ('standard', 'inkypi', 'guest', 'inkypi2')),
                totp_secret    TEXT DEFAULT NULL,
                totp_enabled   INTEGER DEFAULT 0,
                must_change_pw INTEGER DEFAULT 0,
                email          TEXT DEFAULT NULL,
                created_at     TEXT DEFAULT (datetime('now')),
                updated_at     TEXT DEFAULT (datetime('now'))
            )
        """)

        rows = await (await conn.execute("""
            SELECT username, password_hash, role, dashboard, totp_secret,
                   totp_enabled, must_change_pw, email, created_at, updated_at
            FROM users
        """)).fetchall()

        before: dict[str, str] = {}
        report: dict[str, dict[str, str]] = {}
        mapped_rows = []
        for r in rows:
            username, old_role = r[0], r[2]
            before[username] = old_role
            new_role = V56_ROLE_MAP.get(str(old_role or "").strip().lower(), "viewer")
            if new_role != old_role:
                report[username] = {"from": old_role, "to": new_role}
            mapped_rows.append((r[0], r[1], new_role, *r[3:]))

        # ── Lockout guard ───────────────────────────────────────────────────
        # A database with accounts but no admin cannot be administered at all,
        # and the recovery path would be manual SQL. Repair rather than commit.
        if mapped_rows and not any(m[2] == "admin" for m in mapped_rows):
            cfg = await (await conn.execute(
                "SELECT value FROM app_config WHERE key = 'admin_username'")).fetchone()
            configured = cfg[0] if cfg else None
            idx = next((i for i, m in enumerate(mapped_rows) if m[0] == configured), 0)
            promoted = mapped_rows[idx][0]
            mapped_rows[idx] = (*mapped_rows[idx][:2], "admin", *mapped_rows[idx][3:])
            report.setdefault(promoted, {"from": before.get(promoted), "to": "admin"})["promoted"] = "lockout_guard"
            logger.error(
                f"DB migration v56: no admin would have remained — promoted {promoted!r} "
                f"to admin to prevent total lockout"
            )

        await conn.executemany("""
            INSERT OR REPLACE INTO users_v56 (
                username, password_hash, role, dashboard, totp_secret,
                totp_enabled, must_change_pw, email, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, mapped_rows)

        admin_n = sum(1 for m in mapped_rows if m[2] == "admin")

        old_n = (await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        new_n = (await (await conn.execute("SELECT COUNT(*) FROM users_v56")).fetchone())[0]
        if new_n < old_n:
            raise RuntimeError(f"refusing swap — copy lost rows ({old_n} -> {new_n})")

        await conn.execute("DROP TABLE users")
        await conn.execute("ALTER TABLE users_v56 RENAME TO users")

        if report:
            await conn.execute(
                "INSERT OR REPLACE INTO app_config (key, value) VALUES ('rbac_migration_report', ?)",
                (_json.dumps(report),),
            )
        await conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (56,))
        await conn.commit()

        changed = ", ".join(f"{u}: {c['from']}->{c['to']}" for u, c in report.items()) or "none"
        logger.info(
            f"DB migration v56: roles collapsed to admin/operator/viewer "
            f"({new_n} accounts, {admin_n if admin_n else 1} admin). Changed: {changed}"
        )
    except Exception as _e:
        try:
            await conn.rollback()
        except Exception:
            pass
        logger.warning(f"DB migration v56 failed (rolled back, users intact): {_e}")


async def init_db(db_path: Path) -> None:
    """Create all tables, apply additive migrations, and set schema version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    set_db_path(db_path)

    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.executescript(CREATE_SQL)

        # ── Applied schema versions ───────────────────────────────────────────
        # Read ONCE, here, so rebuild-class migrations can guard on it.
        #
        # Every migration below writes `INSERT OR IGNORE INTO schema_version`,
        # but until now nothing ever READ it back — so additive ALTERs (which
        # are self-guarding, they just fail harmlessly if the column exists)
        # were fine, while destructive rebuilds re-ran on every single boot.
        # Confirmed live: "DB migration v43: users table rebuilt" appeared 15
        # times in one container's log. v43 drops and recreates `users` each
        # start, so a crash between DROP and RENAME loses every account, and
        # that window was entered on every startup.
        #
        # Additive ALTERs deliberately stay unguarded — they are idempotent and
        # cheap. Only rebuild-class migrations need this.
        _applied_versions: set[int] = set()
        try:
            async with conn.execute("SELECT version FROM schema_version") as _cur:
                _applied_versions = {int(r[0]) for r in await _cur.fetchall()}
        except Exception as _e:
            logger.warning(f"Could not read schema_version (treating as none applied): {_e}")

        # ── Schema v4 additive migrations ─────────────────────────────────────
        # Safe ALTER TABLE: silently skip if column already exists.
        # This handles existing v3 databases that lack these columns.
        _v4_migrations = [
            (
                "automation_history",
                "source",
                "ALTER TABLE automation_history ADD COLUMN source TEXT DEFAULT 'edge'",
            ),
            (
                "automation_rulebooks",
                "engine_mode",
                "ALTER TABLE automation_rulebooks ADD COLUMN engine_mode TEXT DEFAULT 'signal_only'",
            ),
        ]
        for table, column, sql in _v4_migrations:
            try:
                await conn.execute(sql)
                logger.info(f"DB migration: added '{column}' to '{table}'")
            except Exception:
                # Column already exists — safe to ignore
                logger.debug(f"DB migration: '{table}.{column}' already present, skipping")
        # Create index for source column (safe IF NOT EXISTS)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_history_source ON automation_history(source)"
        )
        await conn.commit()

        # ── Schema v5 additive migrations ─────────────────────────────────────
        # amber_engine_config, amber_usage_cache, pricing_eval_log created via
        # CREATE TABLE IF NOT EXISTS in CREATE_SQL above — no ALTER needed for new installs.
        # This block handles column additions to existing tables in future revisions.
        _v5_migrations: list[tuple[str, str, str]] = [
            (
                "smart_dispatch_schedules",
                "action",
                "ALTER TABLE smart_dispatch_schedules ADD COLUMN action TEXT DEFAULT 'CHARGE'",
            ),
        ]
        for _tbl, _col, _sql in _v5_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v5: added '{_col}' to '{_tbl}'")
            except Exception:
                logger.debug(f"DB migration v5: '{_tbl}.{_col}' already present, skipping")
        await conn.commit()

        # ── Schema v6: solar_forecast_config (created via CREATE TABLE IF NOT EXISTS above)
        # No ALTER TABLE needed for new installs; this block handles column additions later.
        _v6_migrations: list[tuple[str, str, str]] = []
        for _tbl, _col, _sql in _v6_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v6: added '{_col}' to '{_tbl}'")
            except Exception:
                logger.debug(f"DB migration v6: '{_tbl}.{_col}' already present, skipping")
        await conn.commit()

        # ── Schema v7: utility_services and agate_utility_links (created via CREATE TABLE)
        # Migrate singletons to utility_services if it's empty
        try:
            async with conn.execute("SELECT COUNT(*) FROM utility_services") as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            
            if count == 0:
                import uuid
                new_id = str(uuid.uuid4())
                # Fetch existing utility config
                async with conn.execute("SELECT * FROM utility_config ORDER BY id DESC LIMIT 1") as cur:
                    util_row = await cur.fetchone()
                # Fetch existing pricing config
                async with conn.execute("SELECT * FROM pricing_config ORDER BY id DESC LIMIT 1") as cur:
                    price_row = await cur.fetchone()
                    
                if util_row or price_row:
                    logger.info(f"DB migration v7: Migrating legacy utility/pricing config to utility_services ({new_id})")
                    util = dict(util_row) if util_row else {}
                    price = dict(price_row) if price_row else {}
                    
                    await conn.execute(
                        """INSERT INTO utility_services (
                               id, name, retailer_name, account_number, nmi, meter_serial, meter_type,
                               pricing_provider, pricing_credentials, pricing_settings,
                               bill_frequency, bill_start_day, bill_period_days, supply_charge_day,
                               metering_fee, network_fixed_fee, demand_charge_kw, demand_window_start,
                               demand_window_end, demand_window_days, fit_rate_c_kwh, fit_scheme_name, notes
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id,
                            "Main Feed",
                            util.get("utility_name"),
                            util.get("account_number"),
                            util.get("nmi_id"),
                            util.get("meter_serial"),
                            util.get("meter_type"),
                            price.get("provider") or "flat",
                            price.get("credentials") or "{}",
                            price.get("settings") or "{}",
                            util.get("bill_frequency") or "quarterly",
                            util.get("bill_start_day") or 1,
                            util.get("bill_period_days"),
                            util.get("supply_charge_day") or 0.0,
                            util.get("metering_fee") or 0.0,
                            util.get("network_fixed_fee") or 0.0,
                            util.get("demand_charge_kw") or 0.0,
                            util.get("demand_window_start"),
                            util.get("demand_window_end"),
                            util.get("demand_window_days") or "weekdays",
                            util.get("fit_rate_c_kwh"),
                            util.get("fit_provider"),
                            util.get("notes")
                        )
                    )
                    
                    # Link all gateways to this service
                    async with conn.execute("SELECT short_id FROM gateways") as cur:
                        gw_rows = await cur.fetchall()
                    for gw in gw_rows:
                        await conn.execute(
                            "INSERT OR IGNORE INTO agate_utility_links (gateway_short_id, utility_service_id) VALUES (?, ?)",
                            (gw["short_id"], new_id)
                        )
                    await conn.commit()
        except Exception as exc:
            logger.warning(f"DB migration v7 failed: {exc}")

        # Add utility_service_id to pricing_snapshots
        try:
            await conn.execute("ALTER TABLE pricing_snapshots ADD COLUMN utility_service_id TEXT")
            logger.info("DB migration v7: added 'utility_service_id' to 'pricing_snapshots'")
        except Exception:
            logger.debug("DB migration v7: 'pricing_snapshots.utility_service_id' already present, skipping")
            
        await conn.commit()
        
        # ── Schema v8 additive migrations ─────────────────────────────────────
        try:
            await conn.execute("ALTER TABLE amber_engine_config ADD COLUMN amber_max_charge_price REAL DEFAULT 0.0")
            logger.info("DB migration v8: added 'amber_max_charge_price'")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE amber_engine_config ADD COLUMN amber_min_export_price REAL DEFAULT 0.0")
            logger.info("DB migration v8: added 'amber_min_export_price'")
        except Exception:
            pass
        await conn.commit()


        # ── Schema v9 additive migrations ─────────────────────────────────────
        try:
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN charge_power_mode TEXT DEFAULT 'default'")
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN charge_power_value REAL DEFAULT 0.0")
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN discharge_power_mode TEXT DEFAULT 'default'")
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN discharge_power_value REAL DEFAULT 0.0")
            logger.info("DB migration v9: added dynamic power parameters to smart_dispatch_config")
        except Exception:
            pass
        await conn.commit()
        # ── Schema v10 additive migrations ────────────────────────────────────
        try:
            # Only applies where the column does not yet exist, i.e. fresh
            # installs. Existing databases keep their column default and, more
            # importantly, their configured rows — untouched.
            await conn.execute(
                f"ALTER TABLE smart_dispatch_config ADD COLUMN strategy_mode TEXT "
                f"DEFAULT '{SAFE_DEFAULT_STRATEGY_MODE}'"
            )
            logger.info(
                f"DB migration v10: added strategy_mode to smart_dispatch_config "
                f"(default '{SAFE_DEFAULT_STRATEGY_MODE}')"
            )
        except Exception:
            pass
        await conn.commit()
        # ── Schema v11 additive migrations (Enphase DPEL) ─────────────────────
        _v11_migrations = [
            ("smart_dispatch_config", "enphase_enabled", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_enabled INTEGER DEFAULT 0"),
            ("smart_dispatch_config", "enphase_host", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_host TEXT DEFAULT NULL"),
            ("smart_dispatch_config", "enphase_user", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_user TEXT DEFAULT 'installer'"),
            ("smart_dispatch_config", "enphase_password", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_password TEXT DEFAULT NULL"),
            ("smart_dispatch_config", "enphase_slew_rate", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_slew_rate INTEGER DEFAULT 500"),
            ("smart_dispatch_config", "enphase_export_limit_w", "ALTER TABLE smart_dispatch_config ADD COLUMN enphase_export_limit_w INTEGER DEFAULT 0"),
        ]
        for _tbl, _col, _sql in _v11_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v11: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()
        # ── Schema v12 additive migrations (Enphase DPEL -> Solar) ────────────
        _v12_migrations = [
            ("solar_forecast_config", "enphase_enabled", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_enabled INTEGER DEFAULT 0"),
            ("solar_forecast_config", "enphase_host", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_host TEXT DEFAULT NULL"),
            ("solar_forecast_config", "enphase_user", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_user TEXT DEFAULT 'installer'"),
            ("solar_forecast_config", "enphase_password", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_password TEXT DEFAULT NULL"),
            ("solar_forecast_config", "enphase_slew_rate", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_slew_rate INTEGER DEFAULT 500"),
            ("solar_forecast_config", "enphase_export_limit_w", "ALTER TABLE solar_forecast_config ADD COLUMN enphase_export_limit_w INTEGER DEFAULT 0"),
        ]
        for _tbl, _col, _sql in _v12_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v12: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()

        # ── Schema v13 additive migrations ────────────────────────────────────
        try:
            await conn.execute("ALTER TABLE pricing_eval_log ADD COLUMN shadowed_rules_json TEXT DEFAULT '[]'")
            logger.info("DB migration v13: added shadowed_rules_json to pricing_eval_log")
        except Exception:
            pass
        await conn.commit()

        # ── Schema v14 — Smart Dispatch look-ahead window ─────────────────────
        _v14_migrations = [
            ("smart_dispatch_config", "lookahead_minutes",
             "ALTER TABLE smart_dispatch_config ADD COLUMN lookahead_minutes INTEGER DEFAULT 0"),
        ]
        for _tbl, _col, _sql in _v14_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v14: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # column already exists — safe to ignore
        await conn.commit()

        # ── Schema v15 — Solar Setup: Enphase control mode & JWT token ────────
        _v15_migrations = [
            ("solar_forecast_config", "enphase_mode",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_mode TEXT DEFAULT 'none'"),
            ("solar_forecast_config", "enphase_token",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_token TEXT DEFAULT NULL"),
            ("solar_forecast_config", "enphase_serial",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_serial TEXT DEFAULT NULL"),
            # JWT auto-fetch: Enlighten cloud credentials + token expiry
            ("solar_forecast_config", "enphase_enlighten_user",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_enlighten_user TEXT DEFAULT NULL"),
            ("solar_forecast_config", "enphase_enlighten_password",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_enlighten_password TEXT DEFAULT NULL"),
            ("solar_forecast_config", "enphase_token_expiry",
             "ALTER TABLE solar_forecast_config ADD COLUMN enphase_token_expiry TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v15_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v15: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # column already exists — safe to ignore
        await conn.commit()

        # ── Schema v16 — Smart Dispatch: Peak Window SOC guard rails ─────────
        _v16_migrations = [
            ("smart_dispatch_config", "min_peak_window_soc",
             "ALTER TABLE smart_dispatch_config ADD COLUMN min_peak_window_soc REAL DEFAULT 60.0"),
            ("smart_dispatch_config", "max_peak_window_soc",
             "ALTER TABLE smart_dispatch_config ADD COLUMN max_peak_window_soc REAL DEFAULT 90.0"),
        ]
        for _tbl, _col, _sql in _v16_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v16: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # column already exists — safe to ignore
        await conn.commit()
        # ── Schema v17 — gateway_solar_sources table ────────────────────────
        # Create table (idempotent — IF NOT EXISTS)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_solar_sources (
                id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
                gateway_id    TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
                source_type   TEXT NOT NULL DEFAULT 'pv_port'
                              CHECK(source_type IN ('pv_port','dc_coupled','remote_pv','split_ct')),
                port          INTEGER DEFAULT NULL,
                accessory_id  TEXT    DEFAULT NULL,
                kwp           REAL    NOT NULL DEFAULT 0.0,
                label         TEXT    DEFAULT NULL,
                detected_by   TEXT    NOT NULL DEFAULT 'manual'
                              CHECK(detected_by IN ('discover','manual')),
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_solar_sources_gw ON gateway_solar_sources(gateway_id)"
        )
        # Auto-seed: if no sources exist yet AND solar_forecast_config has a kwp AND
        # exactly one gateway is registered, create a pv_port source for it.
        try:
            async with conn.execute("SELECT COUNT(*) FROM gateway_solar_sources") as _cur:
                _src_count = (await _cur.fetchone())[0]
            if _src_count == 0:
                async with conn.execute("SELECT kwp FROM solar_forecast_config WHERE id=1") as _cur:
                    _row = await _cur.fetchone()
                    _kwp = float(_row[0]) if _row and _row[0] else 0.0
                if _kwp > 0:
                    async with conn.execute("SELECT short_id FROM gateways LIMIT 1") as _cur:
                        _gw = await _cur.fetchone()
                    if _gw:
                        await conn.execute(
                            """
                            INSERT OR IGNORE INTO gateway_solar_sources
                                (gateway_id, source_type, port, kwp, label, detected_by)
                            VALUES (?, 'pv_port', 1, ?, 'Migrated from Solar Setup', 'manual')
                            """,
                            (_gw[0], _kwp),
                        )
                        logger.info(
                            f"DB migration v17: seeded gateway_solar_sources from solar_forecast_config kwp={_kwp} for gateway {_gw[0]}"
                        )
        except Exception as _e:
            logger.warning(f"DB migration v17 seed skipped: {_e}")
        await conn.commit()

        # ── Schema v18 — Utility Service Pricing Enhancement ─────────────────
        # 1. Additive columns on utility_services
        _v18_util_cols = [
            ("utility_services", "fwh_site_id",
             "ALTER TABLE utility_services ADD COLUMN fwh_site_id TEXT DEFAULT NULL"),
            ("utility_services", "fwh_site_name",
             "ALTER TABLE utility_services ADD COLUMN fwh_site_name TEXT DEFAULT NULL"),
            ("utility_services", "account_name",
             "ALTER TABLE utility_services ADD COLUMN account_name TEXT DEFAULT NULL"),
            ("utility_services", "tariff_type",
             "ALTER TABLE utility_services ADD COLUMN tariff_type INTEGER DEFAULT NULL"),
            ("utility_services", "tariff_validated_at",
             "ALTER TABLE utility_services ADD COLUMN tariff_validated_at TEXT DEFAULT NULL"),
            ("utility_services", "tariff_company_id",
             "ALTER TABLE utility_services ADD COLUMN tariff_company_id INTEGER DEFAULT NULL"),
            ("utility_services", "tariff_company_name",
             "ALTER TABLE utility_services ADD COLUMN tariff_company_name TEXT DEFAULT NULL"),
            ("utility_services", "nem_type",
             "ALTER TABLE utility_services ADD COLUMN nem_type INTEGER DEFAULT NULL"),
            ("utility_services", "vpp_enrolled",
             "ALTER TABLE utility_services ADD COLUMN vpp_enrolled INTEGER DEFAULT 0"),
            ("utility_services", "vpp_provider",
             "ALTER TABLE utility_services ADD COLUMN vpp_provider TEXT DEFAULT NULL"),
            # DEF-PB-01: user-defined site grouping — for multi-meter installs and card display
            ("utility_services", "site_id",
             "ALTER TABLE utility_services ADD COLUMN site_id TEXT DEFAULT NULL"),
            ("utility_services", "site_name",
             "ALTER TABLE utility_services ADD COLUMN site_name TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v18_util_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v18: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # already exists
        await conn.commit()

        # 2. utility_service_windows — multi-period demand/export/discharge windows
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS utility_service_windows (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id  TEXT NOT NULL REFERENCES utility_services(id) ON DELETE CASCADE,
                window_type TEXT NOT NULL
                            CHECK(window_type IN ('demand','export','discharge')),
                label       TEXT,
                start_time  TEXT NOT NULL,              -- HH:MM
                end_time    TEXT NOT NULL,              -- HH:MM
                day_type    TEXT DEFAULT 'weekdays'
                            CHECK(day_type IN ('all','weekdays','weekends','everyday')),
                months      TEXT DEFAULT NULL,          -- comma CSV e.g. '10,11,12,1,2' or NULL=all
                rate        REAL DEFAULT NULL,          -- CENTS, always POSITIVE: c/kW (demand)
                                                        -- | c/kWh (export). Direction comes from
                                                        -- rate_kind, never from the sign — see v60.
                                                        -- Demand's time dimension comes from
                                                        -- utility_services.demand_charge_basis.
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usw_service ON utility_service_windows(service_id)"
        )

        # 3. utility_service_audit_log
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS utility_service_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id  TEXT NOT NULL,
                event       TEXT NOT NULL,   -- 'created'|'updated'|'deleted'|'validated'
                actor       TEXT DEFAULT 'ui',
                field       TEXT DEFAULT NULL,
                old_value   TEXT DEFAULT NULL,
                new_value   TEXT DEFAULT NULL,
                detail      TEXT DEFAULT NULL,
                ts          TEXT DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usal_service ON utility_service_audit_log(service_id)"
        )

        # 4. Auto-migrate existing demand_window_* data to utility_service_windows
        try:
            async with conn.execute(
                """SELECT id, demand_window_start, demand_window_end, demand_window_days,
                          demand_charge_kw
                   FROM utility_services
                   WHERE demand_window_start IS NOT NULL
                     AND demand_window_start != ''"""
            ) as _cur:
                _svcs = await _cur.fetchall()
            for _svc in _svcs:
                _sid, _start, _end, _days, _rate = _svc
                # Only migrate if no windows exist yet for this service
                async with conn.execute(
                    "SELECT COUNT(*) FROM utility_service_windows WHERE service_id=? AND window_type='demand'",
                    (_sid,)
                ) as _c:
                    if (await _c.fetchone())[0] == 0:
                        await conn.execute(
                            """INSERT INTO utility_service_windows
                               (service_id, window_type, label, start_time, end_time, day_type, rate)
                               VALUES (?, 'demand', 'Peak Demand', ?, ?, ?, ?)""",
                            (_sid, _start, _end, _days or 'weekdays', _rate or 0.0)
                        )
                        logger.info(f"DB migration v18: migrated demand window for service {_sid}")
        except Exception as _me:
            logger.warning(f"DB migration v18 demand window migration skipped: {_me}")
        await conn.commit()

        await conn.execute(
            "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )

        # ── Schema v19 — Solar Source naming + Utility Service link ──────────
        _v19_solar_cols = [
            ("gateway_solar_sources", "source_name",
             "ALTER TABLE gateway_solar_sources ADD COLUMN source_name TEXT DEFAULT NULL"),
            ("gateway_solar_sources", "utility_service_id",
             "ALTER TABLE gateway_solar_sources ADD COLUMN utility_service_id TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v19_solar_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v19: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # already exists
        await conn.commit()

        # ── Schema v20 — Solar Source inverter metadata ──────────────────────────────────
        _v20_meta_cols = [
            ("gateway_solar_sources", "brand",
             "ALTER TABLE gateway_solar_sources ADD COLUMN brand TEXT DEFAULT NULL"),
            ("gateway_solar_sources", "inverter_type",
             "ALTER TABLE gateway_solar_sources ADD COLUMN inverter_type TEXT DEFAULT NULL"),
            ("gateway_solar_sources", "phase_count",
             "ALTER TABLE gateway_solar_sources ADD COLUMN phase_count INTEGER DEFAULT 1"),
            ("gateway_solar_sources", "ac_voltage",
             "ALTER TABLE gateway_solar_sources ADD COLUMN ac_voltage INTEGER DEFAULT NULL"),
            ("gateway_solar_sources", "ac_hz",
             "ALTER TABLE gateway_solar_sources ADD COLUMN ac_hz INTEGER DEFAULT NULL"),
            ("gateway_solar_sources", "pv_control",
             "ALTER TABLE gateway_solar_sources ADD COLUMN pv_control INTEGER DEFAULT 0"),
            ("gateway_solar_sources", "pv_control_type",
             "ALTER TABLE gateway_solar_sources ADD COLUMN pv_control_type TEXT DEFAULT NULL"),
            ("gateway_solar_sources", "pv_control_entity",
             "ALTER TABLE gateway_solar_sources ADD COLUMN pv_control_entity TEXT DEFAULT NULL"),
            ("pending_approvals", "no_reply_action",
             "ALTER TABLE pending_approvals ADD COLUMN no_reply_action TEXT DEFAULT 'skip'"),
            ("pending_approvals", "action_context",
             "ALTER TABLE pending_approvals ADD COLUMN action_context TEXT DEFAULT '{}'"),
        ]
        for _tbl, _col, _sql in _v20_meta_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v20: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # already exists
        await conn.commit()

        # ── Schema v21 — Fix CHECK constraint + data migration + new columns ─────────────
        
        # ── Schema v22 — Forecast Loads Gateway ID ─────────────
        try:
            await conn.execute("ALTER TABLE forecast_loads ADD COLUMN gateway_id TEXT DEFAULT 'global'")
            logger.info("DB migration v22: added 'gateway_id' to 'forecast_loads'")
        except Exception:
            pass
        await conn.commit()

        # ── Schema v23 — Forecast Loads Dispatch Category ─────────────
        try:
            await conn.execute("ALTER TABLE forecast_loads ADD COLUMN dispatch_category TEXT DEFAULT '2-Essential Load'")
            logger.info("DB migration v23: added 'dispatch_category' to 'forecast_loads'")
        except Exception:
            pass
        await conn.commit()        # SQLite cannot ALTER a CHECK constraint. We must rebuild the table.
        # Strategy: rename old → create new (wider CHECK) → copy+remap → drop old.
        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gateway_solar_sources_v21_done'") as _chk:
            _v21_done = await _chk.fetchone()
            
        if not _v21_done:
            await conn.execute("PRAGMA foreign_keys=OFF")
            await conn.execute("DROP TABLE IF EXISTS _gss_old_v21")
            await conn.execute(
                "ALTER TABLE gateway_solar_sources RENAME TO _gss_old_v21"
            )
            await conn.execute("""
                CREATE TABLE gateway_solar_sources (
                    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
                    gateway_id    TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
                    source_type   TEXT NOT NULL DEFAULT 'pv_port_1'
                                  CHECK(source_type IN (
                                      'pv_port','pv_port_1','pv_port_2',
                                      'dc_coupled','mppt_1','mppt_2',
                                      'remote_pv','remote_pv_1','remote_pv_2',
                                      'split_ct'
                                  )),
                    port          INTEGER DEFAULT NULL,
                    accessory_id  TEXT    DEFAULT NULL,
                    kwp           REAL    NOT NULL DEFAULT 0.0,
                    label         TEXT    DEFAULT NULL,
                    source_name   TEXT    DEFAULT NULL,
                    utility_service_id TEXT DEFAULT NULL,
                    brand         TEXT    DEFAULT NULL,
                    inverter_type TEXT    DEFAULT NULL,
                    phase_count   INTEGER DEFAULT 1,
                    ac_voltage    INTEGER DEFAULT NULL,
                    ac_hz         INTEGER DEFAULT NULL,
                    pv_control    INTEGER DEFAULT 0,
                    pv_control_type TEXT  DEFAULT NULL,
                    pv_control_entity TEXT DEFAULT NULL,
                    max_amps      INTEGER DEFAULT 63,
                    solar_metering_mode TEXT DEFAULT 'single_phase_internal'
                                  CHECK(solar_metering_mode IN (
                                      'single_phase_internal','three_phase_ct_kit',
                                      'split_ct_external','rs485_meter'
                                  )),
                    off_grid_capable INTEGER DEFAULT 0,
                    pv_data_api      INTEGER DEFAULT 0,
                    detected_by   TEXT    NOT NULL DEFAULT 'manual'
                                  CHECK(detected_by IN ('discover','manual')),
                    enabled       INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT DEFAULT (datetime('now')),
                    updated_at    TEXT DEFAULT (datetime('now'))
                )
            """)
            # Copy rows, remapping legacy source_type values (Ph-1)
            await conn.execute("""
                INSERT INTO gateway_solar_sources
                    (id, gateway_id, source_type, port, accessory_id, kwp, label,
                     source_name, utility_service_id, brand, inverter_type, phase_count,
                     ac_voltage, ac_hz, pv_control, pv_control_type, pv_control_entity,
                     detected_by, enabled, created_at, updated_at)
                SELECT
                    id, gateway_id,
                    CASE
                        WHEN source_type = 'pv_port'    AND port = 2 THEN 'pv_port_2'
                        WHEN source_type = 'pv_port'                 THEN 'pv_port_1'
                        WHEN source_type = 'dc_coupled' AND port = 2 THEN 'mppt_2'
                        WHEN source_type = 'dc_coupled'              THEN 'mppt_1'
                        WHEN source_type = 'remote_pv'  AND port = 2 THEN 'remote_pv_2'
                        WHEN source_type = 'remote_pv'               THEN 'remote_pv_1'
                        WHEN source_type = 'three_phase'             THEN 'pv_port_1'
                        ELSE source_type
                    END,
                    port, accessory_id, kwp, label, source_name, utility_service_id,
                    brand, inverter_type, COALESCE(phase_count, 1),
                    ac_voltage, ac_hz, COALESCE(pv_control, 0),
                    pv_control_type, pv_control_entity,
                    detected_by, enabled, created_at, updated_at
                FROM _gss_old_v21
            """)
            _rmig = await conn.execute("SELECT changes()")
            _rmig_row = await _rmig.fetchone()
            _n = _rmig_row[0] if _rmig_row else 0
            await conn.execute("DROP TABLE _gss_old_v21")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_solar_sources_gw ON gateway_solar_sources(gateway_id)"
            )
            await conn.execute("CREATE TABLE gateway_solar_sources_v21_done (ts TEXT DEFAULT (datetime('now')))")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.commit()
            logger.info(f"DB migration v21: rebuilt gateway_solar_sources with expanded CHECK constraint, migrated {_n} rows")

        # Ph-4: gateways table grid topology columns
        _v21_gw_cols = [
            ("gateways", "service_amps",
             "ALTER TABLE gateways ADD COLUMN service_amps INTEGER DEFAULT NULL"),
            ("gateways", "grid_type",
             "ALTER TABLE gateways ADD COLUMN grid_type TEXT DEFAULT NULL"),
            ("gateways", "gateway_phase",
             "ALTER TABLE gateways ADD COLUMN gateway_phase TEXT DEFAULT NULL"),
            ("gateways", "three_phase_group_id",
             "ALTER TABLE gateways ADD COLUMN three_phase_group_id TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v21_gw_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v21: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # already exists
        await conn.commit()


        # ── Schema v22 — device_models + device_accessories hardware spec columns ─────────────
        # Adds physical spec data (power ratings, AC frequency, MPPT counts, service amps)
        # sourced from FranklinWH aPower Comparison Table and regional installation standards.
        _v22_cols = [
            # device_models: power + electrical specs per aPower Comparison Table
            ("device_models", "max_service_amps",
             "ALTER TABLE device_models ADD COLUMN max_service_amps INTEGER DEFAULT NULL"),
            ("device_models", "nominal_kw",
             "ALTER TABLE device_models ADD COLUMN nominal_kw REAL DEFAULT NULL"),
            ("device_models", "peak_kw",
             "ALTER TABLE device_models ADD COLUMN peak_kw REAL DEFAULT NULL"),
            ("device_models", "max_ac_amps",
             "ALTER TABLE device_models ADD COLUMN max_ac_amps REAL DEFAULT NULL"),
            ("device_models", "ac_hz",
             "ALTER TABLE device_models ADD COLUMN ac_hz INTEGER DEFAULT NULL"),
            ("device_models", "mppt_count",
             "ALTER TABLE device_models ADD COLUMN mppt_count INTEGER DEFAULT 0"),
            ("device_models", "mppt_isc_amps",
             "ALTER TABLE device_models ADD COLUMN mppt_isc_amps INTEGER DEFAULT NULL"),
            ("device_models", "mppt_imp_amps",
             "ALTER TABLE device_models ADD COLUMN mppt_imp_amps INTEGER DEFAULT NULL"),
            ("device_models", "mppt_max_kw",
             "ALTER TABLE device_models ADD COLUMN mppt_max_kw REAL DEFAULT NULL"),
            ("device_models", "ac_solar_max_kw",
             "ALTER TABLE device_models ADD COLUMN ac_solar_max_kw REAL DEFAULT NULL"),
            ("device_models", "rated_kwh",
             "ALTER TABLE device_models ADD COLUMN rated_kwh REAL DEFAULT NULL"),
            ("device_models", "sku_region",
             "ALTER TABLE device_models ADD COLUMN sku_region TEXT DEFAULT NULL"),
            # device_accessories: frequency + breaker rating
            ("device_accessories", "ac_hz",
             "ALTER TABLE device_accessories ADD COLUMN ac_hz INTEGER DEFAULT NULL"),
            ("device_accessories", "max_amps",
             "ALTER TABLE device_accessories ADD COLUMN max_amps INTEGER DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v22_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v22: added '{_col}' to '{_tbl}'")
            except Exception:
                pass  # already exists
        await conn.commit()

        # Back-fill spec data for all known FranklinWH models
        # (hw_version_int, max_service_amps, nominal_kw, peak_kw, max_ac_amps, ac_hz,
        #  mppt_count, mppt_isc_amps, mppt_imp_amps, mppt_max_kw, ac_solar_max_kw, rated_kwh, sku_region)
        _v22_specs = [
            # aPower X (US) hw=0,1,6 — 5kW/13.6kWh, 60Hz, 200A service, 20.8A AC
            (0,   200, 5.0,  10.0, 20.8, 60, 0, None, None, None, 5.0,  13.6, "US"),
            (1,   200, 5.0,  10.0, 20.8, 60, 0, None, None, None, 5.0,  13.6, "US"),
            (6,   200, 5.0,  10.0, 20.8, 60, 0, None, None, None, 5.0,  13.6, "US"),
            # aPower X (AU) hw=2 — 5kW/13.6kWh, 50Hz, 100A service, 21.7A @ 230V
            (2,   100, 5.0,  10.0, 21.7, 50, 0, None, None, None, 5.0,  13.6, "AU"),
            # aPower 2 (US) hw=3 — 10kW/15kWh, 60Hz, 200A service, 48A
            (3,   200, 10.0, 15.0, 48.0, 60, 0, None, None, None, 10.0, 15.0, "US"),
            # aPower S (US) hw=4,5 — 11.5kW nominal*, 15kW peak, 4 MPPTs, 48A AC
            #   mppt_isc_amps=20A, mppt_imp_amps=15A, mppt_max_kw=15kW total DC (5kW × 3)
            #   ac_solar_max_kw=5kW (AC-coupled via PV port), combined max=20kW
            #   *11.5kW continuous; battery-only discharge capped ~10kW
            (4,   200, 11.5, 15.0, 48.0, 60, 4, 20, 15, 15.0, 5.0, 15.0, "US"),
            (5,   200, 11.5, 15.0, 48.0, 60, 4, 20, 15, 15.0, 5.0, 15.0, "US"),
            # aGate X Gen1 (US) hw=100,101 — 200A service controller, 60Hz
            (100, 200, None, None, None, 60, 0, None, None, None, None, None, "US"),
            (101, 200, None, None, None, 60, 0, None, None, None, None, None, "US"),
            # aGate X-01 (AU) hw=102 — 100A service, 50Hz
            (102, 100, None, None, None, 50, 0, None, None, None, None, None, "AU"),
            # aGate X Gen2 (US) hw=103,104 — 200A service, 60Hz
            (103, 200, None, None, None, 60, 0, None, None, None, None, None, "US"),
            (104, 200, None, None, None, 60, 0, None, None, None, None, None, "US"),
        ]
        for (hw, svc_amps, nom_kw, pk_kw, ac_amps, hz,
             mppt_n, mppt_isc, mppt_imp, mppt_kw, ac_sol_kw, kwh, region) in _v22_specs:
            await conn.execute("""
                UPDATE device_models SET
                    max_service_amps = ?,
                    nominal_kw       = ?,
                    peak_kw          = ?,
                    max_ac_amps      = ?,
                    ac_hz            = ?,
                    mppt_count       = ?,
                    mppt_isc_amps    = ?,
                    mppt_imp_amps    = ?,
                    mppt_max_kw      = ?,
                    ac_solar_max_kw  = ?,
                    rated_kwh        = ?,
                    sku_region       = ?
                WHERE hw_version_int = ?
                  AND (max_service_amps IS NULL OR sku_region IS NULL)
            """, (svc_amps, nom_kw, pk_kw, ac_amps, hz,
                  mppt_n, mppt_isc, mppt_imp, mppt_kw, ac_sol_kw, kwh, region, hw))
        await conn.commit()
        logger.info("DB migration v22: back-filled hardware specs on device_models")

        # ── Schema v23 — Services Setup: grid wiring + composite utility link PK ────────────
        # 1. Adds grid wiring columns to utility_services
        # 2. Rebuilds agate_utility_links with composite PK (gateway can link to 2 services
        #    when CT Split—Grid is installed on the gateway).
        # 3. Adds gateway feature flag columns surfaced from FWH Cloud API
        _v23_util_cols = [
            ("utility_services", "ac_wiring_type",
             "ALTER TABLE utility_services ADD COLUMN ac_wiring_type INTEGER DEFAULT 1"),
             # 1=single_phase, 2=split_phase, 3=three_phase
            ("utility_services", "service_voltage_v",
             "ALTER TABLE utility_services ADD COLUMN service_voltage_v INTEGER DEFAULT NULL"),
             # 110, 120, 220, 230, 240
        ]
        for _tbl, _col, _sql in _v23_util_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v23: added '{_col}' to '{_tbl}'")
            except Exception:
                pass

        # Rebuild agate_utility_links with composite PK (gateway_short_id, utility_service_id)
        # Previous PK was gateway_short_id alone (1:1). CT Split—Grid gateways need 1:2.
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agate_utility_links_v23_done'"
        ) as _chk:
            _already_rebuilt = await _chk.fetchone()
        if not _already_rebuilt:
            try:
                # Create new table with composite PK + gateway_phase column
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS agate_utility_links_new (
                        gateway_short_id   TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
                        utility_service_id TEXT NOT NULL REFERENCES utility_services(id) ON DELETE CASCADE,
                        gateway_phase      TEXT DEFAULT NULL,
                        updated_at         TEXT DEFAULT (datetime('now')),
                        PRIMARY KEY (gateway_short_id, utility_service_id)
                    )
                """)
                # Migrate existing rows
                await conn.execute("""
                    INSERT OR IGNORE INTO agate_utility_links_new
                        (gateway_short_id, utility_service_id, updated_at)
                    SELECT gateway_short_id, utility_service_id, updated_at
                    FROM agate_utility_links
                """)
                await conn.execute("DROP TABLE agate_utility_links")
                await conn.execute("ALTER TABLE agate_utility_links_new RENAME TO agate_utility_links")
                # Sentinel so this block never re-runs
                await conn.execute(
                    "CREATE TABLE agate_utility_links_v23_done (ts TEXT DEFAULT (datetime('now')))"
                )
                logger.info("DB migration v23: rebuilt agate_utility_links with composite PK")
            except Exception as _e:
                logger.warning(f"DB migration v23: agate_utility_links rebuild skipped — {_e}")

        # Gateway feature flags from FWH Cloud API
        _v23_gw_cols = [
            ("gateways", "group_id",
             "ALTER TABLE gateways ADD COLUMN group_id TEXT DEFAULT NULL"),
            ("gateways", "group_name",
             "ALTER TABLE gateways ADD COLUMN group_name TEXT DEFAULT NULL"),
            ("gateways", "has_ct_split_grid",
             "ALTER TABLE gateways ADD COLUMN has_ct_split_grid INTEGER DEFAULT 0"),
            ("gateways", "has_ct_split_pv",
             "ALTER TABLE gateways ADD COLUMN has_ct_split_pv INTEGER DEFAULT 0"),
            ("gateways", "has_three_phase",
             "ALTER TABLE gateways ADD COLUMN has_three_phase INTEGER DEFAULT 0"),
            ("gateways", "has_v2l",
             "ALTER TABLE gateways ADD COLUMN has_v2l INTEGER DEFAULT 0"),
            ("gateways", "has_ahub",
             "ALTER TABLE gateways ADD COLUMN has_ahub INTEGER DEFAULT 0"),
            ("gateways", "has_apbox",
             "ALTER TABLE gateways ADD COLUMN has_apbox INTEGER DEFAULT 0"),
        ]
        for _tbl, _col, _sql in _v23_gw_cols:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v23: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()

        async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gateway_solar_sources_v24_done'") as _chk:
            _v24_done = await _chk.fetchone()
            
        if not _v24_done:
            # ── Schema v24 — expanded CHECK constraint on gateway_solar_sources ──
            try:
                # Check if _gss_old_v24 exists from a previously failed v24 run
                async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_gss_old_v24'") as _chk_old:
                    _has_old = await _chk_old.fetchone()
                
                if _has_old:
                    await conn.execute("DROP TABLE IF EXISTS gateway_solar_sources")
                else:
                    await conn.execute("ALTER TABLE gateway_solar_sources RENAME TO _gss_old_v24")
                
                await conn.execute("""
                CREATE TABLE gateway_solar_sources (
                    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
                    gateway_id    TEXT NOT NULL REFERENCES gateways(short_id) ON DELETE CASCADE,
                    source_type   TEXT NOT NULL DEFAULT 'pv_port_1'
                                  CHECK(source_type IN (
                                      'pv_port','pv_port_1','pv_port_2',
                                      'dc_coupled','mppt_1','mppt_2',
                                      'remote_pv','remote_pv_1','remote_pv_2',
                                      'ahub_pv_1','ahub_pv_2',
                                      'apbox_pv_1','apbox_pv_2',
                                      'split_ct'
                                  )),
                    port          INTEGER DEFAULT NULL,
                    accessory_id  TEXT    DEFAULT NULL,
                    kwp           REAL    NOT NULL DEFAULT 0.0,
                    label         TEXT    DEFAULT NULL,
                    source_name   TEXT    DEFAULT NULL,
                    utility_service_id TEXT DEFAULT NULL,
                    brand         TEXT    DEFAULT NULL,
                    inverter_type TEXT    DEFAULT NULL,
                    phase_count   INTEGER DEFAULT 1,
                    ac_voltage    INTEGER DEFAULT NULL,
                    ac_hz         INTEGER DEFAULT NULL,
                    pv_control    INTEGER DEFAULT 0,
                    pv_control_type TEXT  DEFAULT NULL,
                    pv_control_entity TEXT DEFAULT NULL,
                    max_amps      INTEGER DEFAULT 63,
                    solar_metering_mode TEXT DEFAULT 'single_phase_internal'
                                  CHECK(solar_metering_mode IN (
                                      'single_phase_internal','three_phase_ct_kit',
                                      'split_ct_external','rs485_meter'
                                  )),
                    off_grid_capable INTEGER DEFAULT 0,
                    pv_data_api      INTEGER DEFAULT 0,
                    detected_by   TEXT    NOT NULL DEFAULT 'manual'
                                  CHECK(detected_by IN ('discover','manual')),
                    enabled       INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT DEFAULT (datetime('now')),
                    updated_at    TEXT DEFAULT (datetime('now'))
                )
                """)
                
                # We specify columns explicitly to be safe during INSERT SELECT
                await conn.execute("""
                INSERT INTO gateway_solar_sources
                    (id, gateway_id, source_type, port, accessory_id, kwp, label,
                     source_name, utility_service_id, brand, inverter_type, phase_count,
                     ac_voltage, ac_hz, pv_control, pv_control_type, pv_control_entity,
                     max_amps, solar_metering_mode, off_grid_capable, pv_data_api,
                     detected_by, enabled, created_at, updated_at)
                SELECT
                    id, gateway_id, source_type, port, accessory_id, kwp, label,
                    source_name, utility_service_id, brand, inverter_type, phase_count,
                    ac_voltage, ac_hz, pv_control, pv_control_type, pv_control_entity,
                    max_amps, solar_metering_mode, off_grid_capable, pv_data_api,
                    detected_by, enabled, created_at, updated_at
                FROM _gss_old_v24
                """)
                
                # Map legacy remote_pv types to specific hardware variants based on gateway profile
                await conn.execute("""
                UPDATE gateway_solar_sources
                SET source_type = 'apbox_pv_1'
                WHERE source_type = 'remote_pv_1'
                AND gateway_id IN (
                    SELECT short_id FROM gateways 
                    WHERE profile_json LIKE '%"has_apbox": true%' OR profile_json LIKE '%"remote_solar": true%'
                )
                """)
                await conn.execute("""
                UPDATE gateway_solar_sources
                SET source_type = 'apbox_pv_2'
                WHERE source_type = 'remote_pv_2'
                AND gateway_id IN (
                    SELECT short_id FROM gateways 
                    WHERE profile_json LIKE '%"has_apbox": true%' OR profile_json LIKE '%"remote_solar": true%'
                )
                """)
                await conn.execute("""
                UPDATE gateway_solar_sources
                SET source_type = 'ahub_pv_1'
                WHERE source_type = 'remote_pv_1'
                AND gateway_id IN (
                    SELECT short_id FROM gateways 
                    WHERE profile_json LIKE '%"has_ahub": true%'
                )
                """)
                await conn.execute("""
                UPDATE gateway_solar_sources
                SET source_type = 'ahub_pv_2'
                WHERE source_type = 'remote_pv_2'
                AND gateway_id IN (
                    SELECT short_id FROM gateways 
                    WHERE profile_json LIKE '%"has_ahub": true%'
                )
                """)
                
                await conn.execute("DROP TABLE _gss_old_v24")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_solar_sources_gw ON gateway_solar_sources(gateway_id)")
                
                async with conn.execute("SELECT COUNT(*) FROM gateway_solar_sources") as _cur:
                    _n = (await _cur.fetchone())[0]
                    
                await conn.execute("CREATE TABLE gateway_solar_sources_v24_done (ts TEXT DEFAULT (datetime('now')))")
                logger.info(f"DB migration v24: rebuilt gateway_solar_sources with expanded CHECK constraint, migrated {_n} rows")
                await conn.commit()
            except Exception as _e:
                logger.error(f"DB migration v24: failed to rebuild gateway_solar_sources — {_e}")
                await conn.commit()

        # ── Schema v25 — Actionable Notification Reconciliation ──────────
        _v25_migrations = [
            ("automation_history", "request_id", "ALTER TABLE automation_history ADD COLUMN request_id TEXT DEFAULT NULL"),
            ("automation_history", "ha_user_id", "ALTER TABLE automation_history ADD COLUMN ha_user_id TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v25_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v25: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()

        # ── Schema v26 — Smart Dispatch Strategy Matrix & Site DNA Discovery ──────────
        _v26_migrations = [
            ("gateways", "electricity_type", "ALTER TABLE gateways ADD COLUMN electricity_type INTEGER DEFAULT NULL"),
            ("gateways", "grid_feed_max", "ALTER TABLE gateways ADD COLUMN grid_feed_max REAL DEFAULT NULL"),
            ("gateways", "grid_max", "ALTER TABLE gateways ADD COLUMN grid_max REAL DEFAULT NULL"),
            ("gateways", "not_control_export_solar", "ALTER TABLE gateways ADD COLUMN not_control_export_solar INTEGER DEFAULT NULL"),
            ("device_models", "ac_type", "ALTER TABLE device_models ADD COLUMN ac_type TEXT DEFAULT NULL"),
            ("device_accessories", "capability_mapping", "ALTER TABLE device_accessories ADD COLUMN capability_mapping TEXT DEFAULT '{}'"),
        ]
        for _tbl, _col, _sql in _v26_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v26: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        
        # Initialise default sd_actuator_map signals if empty
        async with conn.execute("SELECT COUNT(*) FROM sd_actuator_map") as _cur:
            _count = (await _cur.fetchone())[0]
        if _count == 0:
            logger.info("DB migration v26: seeding default Smart Dispatch actuator signals")
            _defaults = [
                ('CURTAIL_SOLAR', 'fwh_cloud', 'solar_relay'),
                ('FORCE_STANDBY', 'fwh_cloud', 'standby'),
                ('GRID_CHARGE',   'fwh_cloud', 'grid_charge'),
                ('GRID_EXPORT',   'fwh_cloud', 'grid_export'),
            ]
            for _key, _type, _target in _defaults:
                await conn.execute(
                    "INSERT OR IGNORE INTO sd_actuator_map (signal_key, actuator_type, target) VALUES (?, ?, ?)",
                    (_key, _type, _target)
                )
        await conn.commit()

        # ── Schema v27 — Strategy Matrix Weighting & Intent Durations ──────────
        _v27_migrations = [
            ("sd_strategy_matrix", "forecast_weight", "ALTER TABLE sd_strategy_matrix ADD COLUMN forecast_weight REAL DEFAULT 1.0"),
            ("sd_strategy_matrix", "intent_duration_mins", "ALTER TABLE sd_strategy_matrix ADD COLUMN intent_duration_mins INTEGER DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v27_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v27: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()

        # ── Schema v28 — Shadow Mode ──────────────────────────────────────────
        try:
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN shadow_mode INTEGER DEFAULT 0")
            logger.info("DB migration v28: added 'shadow_mode' to 'smart_dispatch_config'")
        except Exception:
            pass
        await conn.commit()

        # ── Schema v29 — Actionable Notification Response Storage ──────────
        _v29_migrations = [
            ("pending_approvals", "response", "ALTER TABLE pending_approvals ADD COLUMN response TEXT DEFAULT NULL"),
            ("pending_approvals", "responded_at", "ALTER TABLE pending_approvals ADD COLUMN responded_at TEXT DEFAULT NULL"),
        ]
        for _tbl, _col, _sql in _v29_migrations:
            try:
                await conn.execute(_sql)
                logger.info(f"DB migration v29: added '{_col}' to '{_tbl}'")
            except Exception:
                pass
        await conn.commit()

        # ── Schema v30 — Self-Healing Uppercase Normalization (Defect C) ──
        try:
            # We want to uppercase short_id and full_serial everywhere to enforce the uppercase standard.
            # SQLite does not support ON UPDATE CASCADE directly unless specified and enabled.
            # To be absolutely safe, we will temporarily disable foreign keys and update all tables.
            await conn.execute("PRAGMA foreign_keys=OFF")
            
            # 1. Update gateways table
            await conn.execute("UPDATE gateways SET short_id = UPPER(short_id), full_serial = UPPER(full_serial)")
            
            # 2. Update batteries table
            await conn.execute("UPDATE batteries SET short_id = UPPER(short_id), full_serial = UPPER(full_serial), agate_short_id = UPPER(agate_short_id)")
            
            # 3. Update gateway_solar_sources table
            await conn.execute("UPDATE gateway_solar_sources SET gateway_id = UPPER(gateway_id)")
            
            # 4. Update gateway_metrics table
            await conn.execute("UPDATE gateway_metrics SET short_id = UPPER(short_id)")
            
            # 5. Update api_edge_metrics table
            await conn.execute("UPDATE api_edge_metrics SET short_id = UPPER(short_id)")
            
            # 6. Update bms_sessions table
            await conn.execute("UPDATE bms_sessions SET short_id = UPPER(short_id)")
            
            # 7. Update agate_utility_links table
            await conn.execute("UPDATE agate_utility_links SET gateway_short_id = UPPER(gateway_short_id)")
            
            # 8. Update gateway_credentials table
            await conn.execute("UPDATE gateway_credentials SET serial = UPPER(serial)")
            
            # 9. Update credential_audit_log table
            await conn.execute("UPDATE credential_audit_log SET serial = UPPER(serial)")
            
            # 10. Update smart_dispatch_config table
            await conn.execute("UPDATE smart_dispatch_config SET gateway_id = UPPER(gateway_id) WHERE gateway_id != 'global'")
            
            # 11. Update sd_strategy_matrix table
            await conn.execute("UPDATE sd_strategy_matrix SET gateway_id = UPPER(gateway_id) WHERE gateway_id != 'all'")
            
            # 12. Update forecast_loads table
            await conn.execute("UPDATE forecast_loads SET gateway_id = UPPER(gateway_id) WHERE gateway_id != 'global'")
            
            # 13. Update pricing_eval_log table
            await conn.execute("UPDATE pricing_eval_log SET gateway_id = UPPER(gateway_id)")
            
            # 14. Update amber_usage_cache table
            await conn.execute("UPDATE amber_usage_cache SET gateway_id = UPPER(gateway_id)")

            # Record schema version 30 explicitly
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (30,),
            )
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.commit()
            logger.info("DB migration v30: successfully self-healed database casing to Normalized Uppercase Standard")
        except Exception as _e:
            logger.warning(f"DB migration v30 self-healing failed: {_e}")
            try:
                await conn.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
        # ── Schema v31: offline tariff profiles & local contract data ──────────
        try:
            # 1. Create utility_tariffs table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS utility_tariffs (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    retailer_name       TEXT,
                    country             TEXT NOT NULL,
                    model_compatibility INTEGER,
                    flat_rate_import    REAL DEFAULT 0.0,
                    flat_rate_export    REAL DEFAULT 0.0,
                    created_at          TEXT DEFAULT (datetime('now')),
                    updated_at          TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY(model_compatibility) REFERENCES device_models(hw_version_int) ON DELETE SET NULL
                )
            """)
            
            # 2. Create utility_tariff_seasons table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS utility_tariff_seasons (
                    id                  TEXT PRIMARY KEY,
                    tariff_id           TEXT NOT NULL REFERENCES utility_tariffs(id) ON DELETE CASCADE,
                    season_name         TEXT NOT NULL,
                    months              TEXT NOT NULL,
                    created_at          TEXT DEFAULT (datetime('now')),
                    updated_at          TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # 3. Create utility_tariff_rates table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS utility_tariff_rates (
                    id                  TEXT PRIMARY KEY,
                    tariff_id           TEXT NOT NULL REFERENCES utility_tariffs(id) ON DELETE CASCADE,
                    season_id           TEXT REFERENCES utility_tariff_seasons(id) ON DELETE CASCADE,
                    rate_type           TEXT NOT NULL CHECK(rate_type IN ('import', 'export')),
                    label               TEXT,
                    start_time          TEXT NOT NULL,
                    end_time            TEXT NOT NULL,
                    day_type            TEXT DEFAULT 'all' CHECK(day_type IN ('all', 'weekdays', 'weekends')),
                    rate_c_kwh          REAL NOT NULL,
                    created_at          TEXT DEFAULT (datetime('now')),
                    updated_at          TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # 4. Create sd_energy_devices table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sd_energy_devices (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    category            TEXT NOT NULL,
                    ha_power_entity     TEXT,
                    ha_energy_entity    TEXT,
                    ha_switch_entity    TEXT,
                    peak_kw             REAL DEFAULT 0.0,
                    avg_kw              REAL DEFAULT 0.0,
                    schedule_json       TEXT DEFAULT '[]',
                    enabled             INTEGER DEFAULT 1,
                    gateway_id          TEXT DEFAULT 'global',
                    created_at          TEXT DEFAULT (datetime('now')),
                    updated_at          TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # 5. Seed default offline tariffs if table is empty
            async with conn.execute("SELECT COUNT(*) FROM utility_tariffs") as cur:
                count_row = await cur.fetchone()
                if count_row and count_row[0] == 0:
                    import uuid
                    # Flat tariff
                    flat_id = str(uuid.uuid4())
                    await conn.execute("""
                        INSERT INTO utility_tariffs (id, name, retailer_name, country, flat_rate_import, flat_rate_export)
                        VALUES (?, 'Standard Flat Tariff', 'Generic Retailer', 'AU', 25.0, 5.0)
                    """, (flat_id,))
                    
                    # TOU tariff
                    tou_id = str(uuid.uuid4())
                    await conn.execute("""
                        INSERT INTO utility_tariffs (id, name, retailer_name, country, flat_rate_import, flat_rate_export)
                        VALUES (?, 'Standard TOU Tariff', 'Generic Retailer', 'AU', 0.0, 0.0)
                    """, (tou_id,))
                    
                    # Default season for TOU
                    season_id = str(uuid.uuid4())
                    await conn.execute("""
                        INSERT INTO utility_tariff_seasons (id, tariff_id, season_name, months)
                        VALUES (?, ?, 'All Year', '1,2,3,4,5,6,7,8,9,10,11,12')
                    """, (season_id, tou_id))
                    
                    # TOU rates
                    rates = [
                        (str(uuid.uuid4()), tou_id, season_id, 'import', 'Off-Peak', '22:00', '07:00', 'all', 12.0),
                        (str(uuid.uuid4()), tou_id, season_id, 'import', 'Shoulder', '07:00', '15:00', 'all', 20.0),
                        (str(uuid.uuid4()), tou_id, season_id, 'import', 'Peak', '15:00', '21:00', 'all', 35.0),
                        (str(uuid.uuid4()), tou_id, season_id, 'import', 'Shoulder', '21:00', '22:00', 'all', 20.0),
                        (str(uuid.uuid4()), tou_id, season_id, 'export', 'Flat Export', '00:00', '23:59', 'all', 5.0),
                    ]
                    for r in rates:
                        await conn.execute("""
                            INSERT INTO utility_tariff_rates (id, tariff_id, season_id, rate_type, label, start_time, end_time, day_type, rate_c_kwh)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, r)
                        
            # Record schema version 31 explicitly
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (31,),
            )
            await conn.commit()
            logger.info("DB migration v31: successfully created and seeded offline tariff tables")
        except Exception as _e:
            logger.warning(f"DB migration v31 failed: {_e}")

        # ── Schema v32 — Forecast Loads Multiple HA Entities ─────────────
        try:
            for col in ["ha_energy_entity_id", "ha_switch_entity_id", "ha_binary_entity_id"]:
                try:
                    await conn.execute(f"ALTER TABLE forecast_loads ADD COLUMN {col} TEXT DEFAULT NULL")
                    logger.info(f"DB migration v32: added '{col}' to 'forecast_loads'")
                except Exception:
                    pass
            
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (32,),
            )
            await conn.commit()
            logger.info("DB migration v32: successfully added new HA entities to forecast_loads")
        except Exception as _e:
            logger.warning(f"DB migration v32 failed: {_e}")

        # ── Schema v33 — Strategy Priority Matrix System Immutable Rules ─────
        try:
            await conn.execute("ALTER TABLE sd_strategy_matrix ADD COLUMN system_immutable INTEGER NOT NULL DEFAULT 0")
            logger.info("DB migration v33: added 'system_immutable' to 'sd_strategy_matrix'")
        except Exception:
            pass
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (33,),
            )
            await conn.commit()
        except Exception:
            pass

        # ── Schema v34 — Smart Dispatch Security Override & Forecast Plan History Store ─────
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sd_security_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gateway_id TEXT NOT NULL,
                    unlocked_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    session_token TEXT NOT NULL
                );
            """)
            logger.info("DB migration v34: created 'sd_security_sessions'")
        except Exception as _e:
            logger.warning(f"DB migration v34 (sd_security_sessions) failed: {_e}")

        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sd_forecast_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    plan_horizon_start TEXT NOT NULL,
                    plan_horizon_end TEXT NOT NULL,
                    plan_json TEXT NOT NULL
                );
            """)
            logger.info("DB migration v34: created 'sd_forecast_history'")
        except Exception as _e:
            logger.warning(f"DB migration v34 (sd_forecast_history) failed: {_e}")

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (34,),
            )
            await conn.commit()
            logger.info("DB migration v34: schema version updated to 34")
        except Exception as _e:
            logger.warning(f"DB migration v34 schema version update failed: {_e}")

        # ── Schema v35 — Security Core (api_tokens, security_audit_log, credential encryption) ──
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT NOT NULL,
                    token_hash   TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    expires_at   TEXT DEFAULT NULL,
                    last_used_at TEXT DEFAULT NULL
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS security_audit_log (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    event   TEXT NOT NULL,
                    source  TEXT NOT NULL DEFAULT 'system',
                    detail  TEXT,
                    ts      TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            logger.info("DB migration v35: created 'api_tokens' and 'security_audit_log'")
        except Exception as _e:
            logger.warning(f"DB migration v35 tables creation failed: {_e}")

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (35,),
            )
            await conn.commit()
            logger.info("DB migration v35: schema version updated to 35")
            
            # Encrypt any existing plaintext credentials
            await migrate_plaintext_credentials()
        except Exception as _e:
            logger.warning(f"DB migration v35 schema version update failed: {_e}")

        # ── Schema v36: ha_instances & notification_devices ─────────────────────
        try:
            # Check if ha_instances has any records
            async with conn.execute("SELECT COUNT(*) FROM ha_instances") as _cur:
                _ha_count = (await _cur.fetchone())[0]
            if _ha_count == 0:
                # Seed from legacy configs in app_config
                import json
                async with conn.execute("SELECT value FROM app_config WHERE key='ha_host'") as _cur:
                    _host_row = await _cur.fetchone()
                    try:
                        _host = json.loads(_host_row[0]) if _host_row else ""
                    except Exception:
                        _host = _host_row[0] if _host_row else ""
                async with conn.execute("SELECT value FROM app_config WHERE key='ha_token'") as _cur:
                    _token_row = await _cur.fetchone()
                    try:
                        _token = json.loads(_token_row[0]) if _token_row else ""
                    except Exception:
                        _token = _token_row[0] if _token_row else ""
                
                if _host:
                    logger.info("DB migration v36: Seeding 'default' HA instance from legacy app_config")
                    await conn.execute(
                        "INSERT OR IGNORE INTO ha_instances (id, alias, host, token, enabled, is_default) VALUES (?, ?, ?, ?, ?, ?)",
                        ("default", "Primary Home Assistant", _host, _token, 1, 1)
                    )
            
            # Check if notification_devices has any records
            async with conn.execute("SELECT COUNT(*) FROM notification_devices") as _cur:
                _dev_count = (await _cur.fetchone())[0]
            if _dev_count == 0:
                # Seed from legacy ha_target
                async with conn.execute("SELECT ha_target FROM automation_notification_settings WHERE id=1") as _cur:
                    _target_row = await _cur.fetchone()
                    _target = _target_row[0] if _target_row else ""
                
                if _target:
                    logger.info("DB migration v36: Seeding 'default_device' target device from legacy settings")
                    await conn.execute(
                        "INSERT OR IGNORE INTO notification_devices (id, ha_instance_id, alias, service_target, enabled) VALUES (?, ?, ?, ?, ?)",
                        ("default_device", "default", "Primary Device", _target, 1)
                    )
            await conn.commit()
        except Exception as _e:
            logger.warning(f"DB migration v36 tables migration failed: {_e}")

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (36,),
            )
            await conn.commit()
            logger.info("DB migration v36: schema version updated to 36")
        except Exception as _e:
            logger.warning(f"DB migration v36 schema version update failed: {_e}")

        # ── Schema v37: forecast_loads is_behind_meter ───────────────────────────
        try:
            await conn.execute("ALTER TABLE forecast_loads ADD COLUMN is_behind_meter INTEGER DEFAULT 1")
            logger.info("DB migration v37: added 'is_behind_meter' to 'forecast_loads'")
        except Exception:
            pass

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (37,),
            )
            await conn.commit()
            logger.info("DB migration v37: schema version updated to 37")
        except Exception as _e:
            logger.warning(f"DB migration v37 schema version update failed: {_e}")

        # ── Schema v38: smart_dispatch_config new Phase 1 config fields ──────────
        try:
            for col, dflt in [
                ("weather_extreme_impact", "INTEGER DEFAULT 0"),
                ("site_has_high_loads", "INTEGER DEFAULT 0"),
                ("multi_utility_service", "INTEGER DEFAULT 0"),
                ("utility_export_limit_w", "INTEGER DEFAULT 0"),
                ("has_apbox_excess_solar", "INTEGER DEFAULT 0"),
                ("apower_s_mppt", "INTEGER DEFAULT 0"),
                ("strategy_priorities_json", "TEXT DEFAULT '[\"self_consumption\", \"peak_shaving\", \"export_exception\", \"battery_topup\"]'"),
                ("last_full_generation_time", "TEXT DEFAULT NULL"),
            ]:
                try:
                    await conn.execute(f"ALTER TABLE smart_dispatch_config ADD COLUMN {col} {dflt}")
                    logger.info(f"DB migration v38: added column '{col}' to 'smart_dispatch_config'")
                except Exception:
                    pass
        except Exception as _e:
            logger.warning(f"DB migration v38 columns addition failed: {_e}")

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (38,),
            )
            await conn.commit()
            logger.info("DB migration v38: schema version updated to 38")
        except Exception as _e:
            logger.warning(f"DB migration v38 schema version update failed: {_e}")

        # ── Schema v39: default_operating_mode ──────────────────────────────────
        try:
            await conn.execute("ALTER TABLE smart_dispatch_config ADD COLUMN default_operating_mode TEXT DEFAULT 'gateway_default'")
            logger.info("DB migration v39: added 'default_operating_mode' to 'smart_dispatch_config'")
        except Exception:
            pass

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (39,),
            )
            await conn.commit()
            logger.info("DB migration v39: schema version updated to 39")
        except Exception as _e:
            logger.warning(f"DB migration v39 schema version update failed: {_e}")

        # ── Schema v40: baseline_tou_snapshot & overrides ───────────────────────
        _v40_cols = [
            ("baseline_tou_snapshot", "TEXT DEFAULT NULL"),
            ("active_override_uuid", "TEXT DEFAULT NULL"),
            ("active_override_expires_at", "TEXT DEFAULT NULL"),
            ("rampTime", "INTEGER DEFAULT 99"),
            ("maxChargeSoc", "INTEGER DEFAULT 100"),
            ("minDischargeSoc", "INTEGER DEFAULT 0"),
            ("chargePower", "INTEGER DEFAULT 5000"),
            ("dischargePower", "INTEGER DEFAULT 5000"),
        ]
        for col, dflt in _v40_cols:
            try:
                await conn.execute(f"ALTER TABLE smart_dispatch_config ADD COLUMN {col} {dflt}")
                logger.info(f"DB migration v40: added column '{col}' to 'smart_dispatch_config'")
            except Exception:
                pass

        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (40,),
            )
            await conn.commit()
            logger.info("DB migration v40: schema version updated to 40")
        except Exception as _e:
            logger.warning(f"DB migration v40 schema version update failed: {_e}")

        # ── Schema v41: users table migration and default admin setup ──────────
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (41,),
            )
            await conn.commit()
            logger.info("DB migration v41: schema version updated to 41")
            
            # Check if any users exist
            async with conn.execute("SELECT COUNT(*) FROM users") as cur:
                count_row = await cur.fetchone()
                user_count = count_row[0] if count_row else 0
            
            if user_count == 0:
                logger.info("Users table is empty. Checking for legacy admin credentials...")
                async with conn.execute("SELECT value FROM app_config WHERE key = 'admin_password_hash'") as cur:
                    legacy_pw_row = await cur.fetchone()
                    legacy_pw_hash = legacy_pw_row[0] if legacy_pw_row else None
                
                async with conn.execute("SELECT value FROM app_config WHERE key = 'admin_username'") as cur:
                    legacy_un_row = await cur.fetchone()
                    legacy_username = legacy_un_row[0] if legacy_un_row else "admin"

                if legacy_pw_hash:
                    logger.info(f"DB migration v41: Migrating legacy admin user '{legacy_username}' to users table")
                    await conn.execute(
                        "INSERT INTO users (username, password_hash, role, dashboard, must_change_pw) VALUES (?, ?, 'admin', 'standard', 0)",
                        (legacy_username, legacy_pw_hash),
                    )
                else:
                    import secrets
                    from src.services.crypto import hash_password
                    otp_suffix = secrets.token_hex(3)
                    otp = f"fhai_admin_{otp_suffix}"
                    hashed_otp = hash_password(otp)
                    
                    logger.info("****************************************************************")
                    logger.info("MANDATORY ADMIN STARTUP OTP GENERATION")
                    logger.info("USERNAME: admin")
                    logger.info(f"PASSWORD: {otp}")
                    logger.info("Please change this password immediately upon first login.")
                    logger.info("****************************************************************")
                    
                    print("****************************************************************", flush=True)
                    print("MANDATORY ADMIN STARTUP OTP GENERATION", flush=True)
                    print("USERNAME: admin", flush=True)
                    print(f"PASSWORD: {otp}", flush=True)
                    print("Please change this password immediately upon first login.", flush=True)
                    print("****************************************************************", flush=True)
                    
                    await conn.execute(
                        "INSERT INTO users (username, password_hash, role, dashboard, must_change_pw) VALUES ('admin', ?, 'admin', 'standard', 1)",
                        (hashed_otp,),
                    )
                await conn.commit()
        except Exception as _e:
            logger.warning(f"DB migration v41 failed: {_e}")

        # ── Schema v42: users email and notification_devices owner_username ────
        try:
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT NULL")
                logger.info("DB migration v42: added 'email' column to 'users'")
            except Exception:
                pass

            try:
                await conn.execute("ALTER TABLE notification_devices ADD COLUMN owner_username TEXT DEFAULT NULL")
                logger.info("DB migration v42: added 'owner_username' column to 'notification_devices'")
            except Exception:
                pass

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (42,),
            )
            await conn.commit()
            logger.info("DB migration v42: schema version updated to 42")
        except Exception as _e:
            logger.warning(f"DB migration v42 failed: {_e}")

        # ── Schema v43: users table rebuild for inkypi2 support ─────────────────
        # Guarded (GH: users/roles Stage 0) — this is a DESTRUCTIVE rebuild and
        # must run at most once. Unguarded it re-dropped `users` on every boot.
        if 43 in _applied_versions:
            logger.debug("DB migration v43: already applied — skipping users rebuild")
        else:
            # Close any implicit transaction left open by an earlier migration so
            # BEGIN IMMEDIATE cannot fail with "transaction within a transaction".
            try:
                await conn.commit()
            except Exception:
                pass
            await _migrate_v43_users_rebuild(conn)
            _applied_versions.add(43)

        # ── Schema v44: solar_forecast_config rate limiter column
        try:
            await conn.execute(
                "ALTER TABLE solar_forecast_config ADD COLUMN forecast_solar_rate_limit_mins INTEGER DEFAULT 30"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (44,),
            )
            await conn.commit()
            logger.info("DB migration v44: added forecast_solar_rate_limit_mins to solar_forecast_config and updated version to 44")
        except Exception as _e:
            logger.debug(f"DB migration v44 skipped (already applied or column exists): {_e}")

        # ── Schema v45: Decoupled Pricing Models ──────────────────────────────
        try:
            # 1. Create pricing_models table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pricing_models (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    credentials  TEXT DEFAULT '{}',
                    settings     TEXT DEFAULT '{}',
                    updated_at   TEXT DEFAULT (datetime('now'))
                )
            """)
            
            # 2. Seed default pricing models
            default_models = [
                ("flat", "Flat Rate"),
                ("amber", "Amber Electric"),
                ("localvolts", "LocalVolts"),
                ("aemo", "AEMO NEM"),
                ("franklinwh_tou", "FranklinWH TOU"),
                ("comed", "ComEd Hourly")
            ]
            for model_id, model_name in default_models:
                await conn.execute(
                    "INSERT OR IGNORE INTO pricing_models (id, name) VALUES (?, ?)",
                    (model_id, model_name)
                )

            # 3. Add pricing_model_id column to utility_services if not exists
            async with conn.execute("PRAGMA table_info(utility_services)") as cur:
                columns = [col[1] for col in await cur.fetchall()]
            if "pricing_model_id" not in columns:
                await conn.execute(
                    "ALTER TABLE utility_services ADD COLUMN pricing_model_id TEXT REFERENCES pricing_models(id) DEFAULT 'franklinwh_tou'"
                )
                logger.info("DB migration v45: added pricing_model_id column to utility_services")

            # 4. Migrate credentials/settings from utility_services to pricing_models
            async with conn.execute("SELECT id, pricing_provider, pricing_credentials, pricing_settings, pricing_model_id FROM utility_services") as cur:
                services = await cur.fetchall()

            for svc_id, provider, creds_str, settings_str, current_model_id in services:
                target_model_id = provider if provider in [m[0] for m in default_models] else "franklinwh_tou"
                
                if creds_str and creds_str != '{}':
                    await conn.execute(
                        "UPDATE pricing_models SET credentials = ? WHERE id = ? AND (credentials = '{}' OR credentials IS NULL)",
                        (creds_str, target_model_id)
                    )
                if settings_str and settings_str != '{}':
                    await conn.execute(
                        "UPDATE pricing_models SET settings = ? WHERE id = ? AND (settings = '{}' OR settings IS NULL)",
                        (settings_str, target_model_id)
                    )

                if current_model_id is None or current_model_id == 'franklinwh_tou' or current_model_id == '':
                    await conn.execute(
                        "UPDATE utility_services SET pricing_model_id = ? WHERE id = ?",
                        (target_model_id, svc_id)
                    )

            # 5. Insert schema version 45 record
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (45,),
            )
            await conn.commit()
            logger.info("DB migration v45: pricing_models schema migration applied successfully")
        except Exception as _e:
            logger.exception(f"DB migration v45 failed: {_e}")

        # ── Schema v46: forecast_loads slug column + backfill ──────────────
        # Stable per-gateway-unique key for the AB home_load.<gw>.<slug>.*
        # namespace. Slugs are derived from the load name on insert and never
        # auto-changed on rename, so AB rules survive load renames.
        try:
            try:
                await conn.execute("ALTER TABLE forecast_loads ADD COLUMN slug TEXT DEFAULT ''")
                logger.info("DB migration v46: added 'slug' to 'forecast_loads'")
            except Exception:
                logger.debug("DB migration v46: 'forecast_loads.slug' already present, skipping")

            # Backfill: for every row with empty slug, derive from name with
            # per-gateway dedupe. Done one gateway at a time so collisions
            # within a gateway get _2/_3 suffixes; collisions across gateways
            # are fine because the namespace nests under gateway_key.
            # init_db's connection does NOT set row_factory, so access rows
            # by positional index.
            import re as _re
            def _slugify(name: str, fallback: str) -> str:
                s = _re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
                return s or (fallback or "load")

            async with conn.execute(
                "SELECT DISTINCT COALESCE(gateway_id, 'global') FROM forecast_loads"
            ) as cur:
                gw_keys = [row[0] for row in await cur.fetchall()]

            for gw in gw_keys:
                async with conn.execute(
                    "SELECT id, name, slug FROM forecast_loads "
                    "WHERE COALESCE(gateway_id, 'global') = ? "
                    "ORDER BY created_at, id",
                    (gw,),
                ) as cur:
                    rows = await cur.fetchall()
                used: set[str] = {r[2] for r in rows if r[2]}
                for r in rows:
                    row_id, row_name, row_slug = r[0], r[1], r[2]
                    if row_slug:
                        continue
                    base = _slugify(row_name, row_id)
                    candidate = base
                    n = 2
                    while candidate in used:
                        candidate = f"{base}_{n}"
                        n += 1
                    used.add(candidate)
                    await conn.execute(
                        "UPDATE forecast_loads SET slug = ? WHERE id = ?",
                        (candidate, row_id),
                    )

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (46,),
            )
            await conn.commit()
            logger.info("DB migration v46: forecast_loads.slug backfilled per gateway")
        except Exception as _e:
            logger.warning(f"DB migration v46 failed: {_e}")

        # ── Schema v47: notification_cooldown_rules + notification_cooldown ─
        # Suppresses same-rule notification re-fires after a pending_approval
        # TTL expires without user response. Per backlog P2 dated 2026-07-09:
        # user complained about "duplicate" notifications where the same
        # matrix rule refired every 30-60min. This is not a duplicate — it's
        # a legitimate TTL-expiry re-emit — but the UX is spammy.
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_cooldown_rules (
                    rule_id           TEXT PRIMARY KEY,
                    label             TEXT NOT NULL,
                    cooldown_seconds  INTEGER NOT NULL DEFAULT 7200,
                    enabled           INTEGER NOT NULL DEFAULT 1,
                    updated_at        TEXT DEFAULT (datetime('now'))
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_cooldown (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    gateway_serial  TEXT NOT NULL,
                    rule_id         TEXT NOT NULL,
                    decision_hash   TEXT,
                    expires_at      INTEGER NOT NULL,
                    ignored_count   INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT DEFAULT (datetime('now'))
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_notification_cooldown_lookup
                ON notification_cooldown(gateway_serial, rule_id, expires_at)
            """)

            # Seed the 7 default cooldown rules — matches the _category_map
            # in src/services/smart_dispatch.py so trigger_category → ev_key
            # → cooldown row lookup works.
            default_rules = [
                ("spike",           "Grid Price Spike",       7200),
                ("force_charge",    "Force Charge Window",    7200),
                ("export_bonus",    "Export Bonus Window",    7200),
                ("demand_charge",   "Demand Charge Window",   7200),
                ("earnings_target", "Earnings Target Reached", 7200),
                ("negative_export", "Negative Export Window", 7200),
                ("force_export",    "Force Export Window",    7200),
            ]
            for rule_id, label, secs in default_rules:
                await conn.execute(
                    "INSERT OR IGNORE INTO notification_cooldown_rules "
                    "(rule_id, label, cooldown_seconds, enabled) VALUES (?, ?, ?, 1)",
                    (rule_id, label, secs),
                )

            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (47,),
            )
            await conn.commit()
            logger.info("DB migration v47: notification_cooldown_rules + notification_cooldown seeded")
        except Exception as _e:
            logger.warning(f"DB migration v47 failed: {_e}")

        # ── Schema v48: extend cooldown_rules with 3 missing categories ─────
        # Batch C (v47) seeded the 7 canonical rule categories mapped by
        # NOTIFICATION_CATEGORY_MAP. Audit of live sd_strategy_matrix showed
        # 3 in-use trigger_category values that had no cooldown seed row:
        # `time_schedule` (5 time-of-day system rules), `solar_optimization`
        # (solar-adjacent rules), and `custom` (any user-created rule with
        # trigger_category='custom'). Without a seed row, is_active() short-
        # circuits at rule_row lookup and returns None → no suppression → the
        # dedup mechanism silently degrades to noop for those categories.
        try:
            v48_extra_rules = [
                ("time_schedule",      "Time-of-Day Schedule",  7200),
                ("solar_optimization", "Solar Optimization",    7200),
                ("custom",             "Custom User Rule",      7200),
            ]
            for rule_id, label, secs in v48_extra_rules:
                await conn.execute(
                    "INSERT OR IGNORE INTO notification_cooldown_rules "
                    "(rule_id, label, cooldown_seconds, enabled) VALUES (?, ?, ?, 1)",
                    (rule_id, label, secs),
                )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (48,),
            )
            await conn.commit()
            logger.info(
                "DB migration v48: extended notification_cooldown_rules with "
                "time_schedule, solar_optimization, custom"
            )
        except Exception as _e:
            logger.warning(f"DB migration v48 failed: {_e}")

        # ── Schema v49: automation_notification_log.details_json ─────────────
        # Batch N (2026-07-25) audit UX: the `details` column was being
        # written as a 400+ char one-line dump of the full HA payload +
        # dispatch summary, making the SD Notification Audit ledger UI
        # unreadable. Splitting into:
        #   - details       : ≤ ~100 char human summary (existing column,
        #                     new writes are brief; historical rows keep
        #                     their long strings)
        #   - details_json  : nullable full JSON blob for opt-in expand-
        #                     on-demand rendering (new column here in v49)
        # UI update to render the expand affordance is deferred to Batch
        # N.2; for now the ledger just shows shorter details.
        try:
            # Idempotent ADD COLUMN (SQLite raises "duplicate column" on
            # re-run — catch + continue).
            try:
                await conn.execute(
                    "ALTER TABLE automation_notification_log "
                    "ADD COLUMN details_json TEXT"
                )
            except Exception as _alter_exc:
                if "duplicate column" not in str(_alter_exc).lower():
                    raise
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (49,),
            )
            await conn.commit()
            logger.info(
                "DB migration v49: added automation_notification_log.details_json"
            )
        except Exception as _e:
            logger.warning(f"DB migration v49 failed: {_e}")

        # ── Schema v50: pricing_eval_log.shadow_reason ─────────────────────────
        # Batch P (2026-07-30) SD ownership deference: when an external
        # controller (VPP, Modbus Bridge, Manual Dispatch) owns the gateway,
        # SD evaluates rules for audit but doesn't act. shadow_reason
        # records WHY SD deferred so consumers (Diagnostics tab / CLI /
        # HA sensor) can distinguish "SD would have wanted X" from "SD
        # actively decided X and shipped it". Nullable — populated only
        # on deferred rows; historic rows stay NULL.
        try:
            try:
                await conn.execute(
                    "ALTER TABLE pricing_eval_log ADD COLUMN shadow_reason TEXT"
                )
            except Exception as _alter_exc:
                if "duplicate column" not in str(_alter_exc).lower():
                    raise
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (50,),
            )
            await conn.commit()
            logger.info(
                "DB migration v50: added pricing_eval_log.shadow_reason"
            )
        except Exception as _e:
            logger.warning(f"DB migration v50 failed: {_e}")

        # ── Schema v51: sd_lookahead_dedup ───────────────────────────────────
        # Batch S (2026-08-02) — persistent lookahead notification dedup.
        # Previously the engine kept `_lookahead_sent[(gw, kind, hour_bucket)]`
        # and `_lookahead_last_check[gw]` as process-local dicts on the
        # SmartDispatchEngine instance. A container restart wiped both, so
        # the first tick after startup would re-send lookahead notifications
        # for any future hour-bucket still matching thresholds — even if the
        # pre-restart process had already sent them. Presenting symptom
        # (2026-08-02): duplicate "Cheap Price - Force Charge?" notifications
        # fired 1s after startup for two adjacent hour-buckets simultaneously.
        # Moving the dedup state into SQLite makes it survive restarts and
        # gives the future revamp phases a persistence pattern to reuse.
        #
        # Columns:
        #   gateway_serial  — full gateway serial the notification is for
        #   kind            — 'spike' | 'export_bonus' | 'force_charge'
        #   hour_bucket     — ISO-8601 date-plus-hour string ("2026-08-02T13")
        #                     of the target notification window
        #   sent_at         — unix timestamp of the send (float seconds)
        #   created_at      — ISO timestamp (audit / prune anchor)
        # Also a per-gateway scan cadence row keyed by kind='__scan__'
        # replaces `_lookahead_last_check`. hour_bucket for scan rows is
        # empty string. Prune drops rows older than 24 h on write.
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sd_lookahead_dedup (
                    gateway_serial TEXT    NOT NULL,
                    kind           TEXT    NOT NULL,
                    hour_bucket    TEXT    NOT NULL DEFAULT '',
                    sent_at        REAL    NOT NULL,
                    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (gateway_serial, kind, hour_bucket)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sd_lookahead_dedup_sent_at ON sd_lookahead_dedup(sent_at)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (51,),
            )
            await conn.commit()
            logger.info("DB migration v51: sd_lookahead_dedup persistent lookahead dedup table")
        except Exception as _e:
            logger.warning(f"DB migration v51 failed: {_e}")

        # ── Schema v52: sd_macro_snapshot ────────────────────────────────────
        # Phase 2.A (2026-08-05) — Macro Discovery loop persistence. Runs
        # daily @03:00 site-local per gateway to capture slow-changing
        # capability + tariff facts that Meso/Micro shouldn't re-fetch
        # every tick: gateway DNA (max grid feed, phases, service amps),
        # utility service tariff calendar, SD config thresholds. Non-
        # actionable — Meso reads the latest snapshot as one input to its
        # 24 h dispatch plan.
        #
        # Columns:
        #   gateway_serial  — full gateway serial the snapshot is for
        #   generated_at    — ISO timestamp (indexed for "get latest")
        #   snapshot_json   — JSON blob (dna + tariff + cfg + meter caps)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sd_macro_snapshot (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    gateway_serial TEXT    NOT NULL,
                    generated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    snapshot_json  TEXT    NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sd_macro_snapshot_lookup "
                "ON sd_macro_snapshot(gateway_serial, generated_at DESC)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (52,),
            )
            await conn.commit()
            logger.info("DB migration v52: sd_macro_snapshot table")
        except Exception as _e:
            logger.warning(f"DB migration v52 failed: {_e}")

        # ── Schema v53: smart_dispatch_config.sd_use_micro_ticker ───────────
        # Phase 2.D (2026-08-05) — per-gateway feature flag routing SD
        # invocation between the legacy on-tick path (PricingService.tick
        # rate-limited to 300 s) and the new APScheduler `sd:micro:tick`
        # job (30 s interval). Default 0 so no gateway switches unless
        # explicitly enabled. When 1: PricingService.tick skips SD for
        # that gateway; MicroTicker runs it instead. Exactly one path
        # fires per gateway based on the flag — no double-fire.
        try:
            try:
                await conn.execute(
                    "ALTER TABLE smart_dispatch_config "
                    "ADD COLUMN sd_use_micro_ticker INTEGER NOT NULL DEFAULT 0"
                )
            except Exception as _alter_exc:
                if "duplicate column" not in str(_alter_exc).lower():
                    raise
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (53,),
            )
            await conn.commit()
            logger.info(
                "DB migration v53: smart_dispatch_config.sd_use_micro_ticker "
                "(per-gateway MicroTicker opt-in)"
            )
        except Exception as _e:
            logger.warning(f"DB migration v53 failed: {_e}")

        # ── Schema v54: smart_dispatch_config.use_lp_optimizer ──────────────
        # Phase 3.A (2026-08-06) — per-gateway opt-in for the linear-
        # programming dispatch planner. Default 0 so no gateway switches
        # off the greedy heuristic unless explicitly enabled. When 1,
        # MesoPlanner.solve routes to LPOptimizer instead of GreedyOptimizer
        # (see src/services/smart_dispatch/lp_optimizer.py + meso.py).
        # v0.5.0 ships the flag + skeleton only; the real LP formulation
        # lands in v0.5.1 (Phase 3.B). Until then the LPOptimizer falls
        # through to greedy, so flipping the flag today has no effect —
        # by design, so the migration is safe to ship in advance of the
        # solver body.
        try:
            try:
                await conn.execute(
                    "ALTER TABLE smart_dispatch_config "
                    "ADD COLUMN use_lp_optimizer INTEGER NOT NULL DEFAULT 0"
                )
            except Exception as _alter_exc:
                if "duplicate column" not in str(_alter_exc).lower():
                    raise
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (54,),
            )
            await conn.commit()
            logger.info(
                "DB migration v54: smart_dispatch_config.use_lp_optimizer "
                "(per-gateway LP optimizer opt-in; body ships in v0.5.1)"
            )
        except Exception as _e:
            logger.warning(f"DB migration v54 failed: {_e}")

        # ── Schema v55: persona_detection_log ───────────────────────────────
        # GH #11 (BD-04) — audit trail for the persona matrix auto-detection.
        # Each row = one axis value at one detection run. Persisted via
        # persona_detector._log_axis(); read by /api/persona/log + the
        # wizard "What we found" step so users see why a persona flag
        # was set (source + confidence). Retention: no auto-prune in v55;
        # add a scheduler-driven prune once row growth becomes an issue.
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS persona_detection_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at     TEXT NOT NULL,
                    gateway_serial  TEXT,
                    axis            TEXT NOT NULL,
                    value           TEXT,
                    source          TEXT NOT NULL,
                    confidence      TEXT NOT NULL,
                    previous_value  TEXT
                )"""
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_persona_log_detected_at "
                "ON persona_detection_log(detected_at DESC)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (55,),
            )
            await conn.commit()
            logger.info("DB migration v55: persona_detection_log")
        except Exception as _e:
            logger.warning(f"DB migration v55 failed: {_e}")

        # ── Schema v56: collapse roles to admin/operator/viewer ─────────────
        # Rebuild-class and destructive — guarded like v43.
        if 56 in _applied_versions:
            logger.debug("DB migration v56: already applied — skipping role collapse")
        else:
            try:
                await conn.commit()
            except Exception:
                pass
            await _migrate_v56_role_collapse(conn)
            _applied_versions.add(56)

        # ── Schema v57: local usage telemetry (collection only, no transmit) ──
        # Counters accumulate per period; a scheduled rollup materialises one
        # outbox row holding the exact payload that would be transmitted, so the
        # UI renders what was built rather than re-deriving it. Nothing leaves
        # the host in this phase. See GH #40.
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_counters (
                    period      TEXT NOT NULL,
                    metric      TEXT NOT NULL,
                    count       INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (period, metric)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_outbox (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    period        TEXT NOT NULL UNIQUE,
                    payload_json  TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'collected',
                    error         TEXT,
                    created_at    TEXT DEFAULT (datetime('now')),
                    sent_at       TEXT
                )
            """)
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (57,)
            )
            await conn.commit()
            logger.info("DB migration v57: telemetry_counters + telemetry_outbox")
        except Exception as _e:
            logger.warning(f"DB migration v57 failed: {_e}")

        # ── Schema v58 — full tariff model, ported from the Modbus Bridge ────
        #
        # The Bridge's tariff editor models a plan the FWHAI one could not.
        #
        # Most of the gap was already covered: utility_service_windows has held
        # demand, export and discharge windows — with times, day type, a month
        # CSV and a rate — as a *list*, which is strictly more capable than a
        # fixed column per window type. The first version of this migration
        # duplicated it with bonus_window_* and export_charge_* columns that
        # allowed exactly one window of each kind. Those are gone; what remains
        # is only what has nowhere else to live.
        #
        # NOTE: this is deliberately a second copy of the same plan. The Bridge
        # holds one too, and nothing reconciles them — whichever is edited last
        # wins, silently, and only for the app it was edited in. That is the
        # same duplicated-source-of-truth shape behind several defects in this
        # project. Ported on request; recorded here so it is visible to whoever
        # meets the drift.
        #
        # Units are CENTS throughout, matching tariff_costing and the rest of
        # FWHAI, even though the Bridge's form is in dollars. A figure read off
        # a bill is typed in as it appears; conversion happens in one place.
        _v58_cols = [
            # Plan identity and rules
            ("plan_timezone",            "TEXT DEFAULT NULL"),
            ("plan_type",                "TEXT DEFAULT NULL"),
            ("export_limit_kw",          "REAL DEFAULT 0"),
            ("export_allowed",           "INTEGER DEFAULT 1"),
            ("battery_charge_permitted", "INTEGER DEFAULT 1"),
            ("battery_discharge_permitted", "INTEGER DEFAULT 1"),
            ("min_monthly_bill_c",       "REAL DEFAULT 0"),
            # How a demand peak is *measured*. The window itself — times, days,
            # months and rate — already lives in utility_service_windows; these
            # are plan-level and have nowhere else to go.
            ("demand_interval_min",      "INTEGER DEFAULT 30"),
            ("demand_interval_count",    "INTEGER DEFAULT 2"),
            ("demand_charge_basis",      "TEXT DEFAULT 'kw_rate_days'"),
            # A plan-level allowance, not a property of any one window.
            ("export_free_kwh_day",      "REAL DEFAULT 0"),
        ]
        for _col, _dflt in _v58_cols:
            try:
                await conn.execute(
                    f"ALTER TABLE utility_services ADD COLUMN {_col} {_dflt}")
            except Exception:
                pass   # already present

        # Standing charges were three fixed columns — supply_charge_day,
        # metering_fee, network_fixed_fee — so a plan with a membership fee or
        # a connection fee had nowhere to put it. A list has no such ceiling.
        # The three columns stay and are still summed by tariff_costing; this
        # table is additive.
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS utility_standing_charges (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id  TEXT NOT NULL,
                    label       TEXT NOT NULL,
                    amount_c    REAL NOT NULL DEFAULT 0,
                    basis       TEXT NOT NULL DEFAULT 'per_day',
                    months      TEXT,
                    created_at  TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (service_id) REFERENCES utility_services(id) ON DELETE CASCADE
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_standing_charges_service "
                "ON utility_standing_charges(service_id)")
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (58,))
            await conn.commit()
            logger.info("DB migration v58: full tariff model + standing charge list")
        except Exception as _e:
            logger.warning(f"DB migration v58 failed: {_e}")

        # ── Schema v59 — demand window rates in cents ───────────────────────
        #
        # utility_service_windows.rate carried two different units in one
        # column: "$/kW/month (demand) | c/kWh (export)". Everything else in
        # FWHAI is cents, and tariff_costing's standing rule is that a figure
        # read off a bill is typed in as it appears, so a dollars-per-month
        # demand rate silently understated every demand charge by 100x while
        # the export rate beside it was correct.
        #
        # The rate is now CENTS per kW for every window type. Its time
        # dimension comes from demand_charge_basis, which is what that field is
        # for: kw_rate_days multiplies by days in the period, kw_rate applies it
        # once. A rate that was $/kW/month becomes c/kW with basis kw_rate, so
        # the conversion is x100 and the basis moves with it.
        try:
            async with conn.execute(
                "SELECT COUNT(*) FROM utility_service_windows WHERE window_type = 'demand'"
            ) as _cur:
                _demand_rows = (await _cur.fetchone())[0]
            if _demand_rows:
                await conn.execute(
                    "UPDATE utility_service_windows SET rate = rate * 100.0 "
                    "WHERE window_type = 'demand' AND rate IS NOT NULL"
                )
                # Those rates were monthly, so they apply once per period.
                await conn.execute(
                    "UPDATE utility_services SET demand_charge_basis = 'kw_rate' "
                    "WHERE id IN (SELECT DISTINCT service_id FROM utility_service_windows "
                    "             WHERE window_type = 'demand')"
                )
                logger.info(
                    f"DB migration v59: converted {_demand_rows} demand window "
                    "rate(s) from $/kW/month to c/kW, basis kw_rate"
                )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (59,))
            await conn.commit()
        except Exception as _e:
            logger.warning(f"DB migration v59 failed: {_e}")

        # ── Schema v60 — window rate direction is a field, not a sign ───────
        #
        # A two-way export charge was to be entered as an export window with a
        # NEGATIVE rate. That collides with dynamic pricing, where a negative
        # price is real data: Amber's PriceDescriptor has a `negative` member,
        # and a negative import price means the market is paying you to
        # consume. One field would have carried two different meanings — "this
        # is a charge rather than a credit" for static plans and "the price
        # went below zero" for dynamic ones — which is precisely the ambiguity
        # that produces a bill nobody can reconcile.
        #
        # So: rates a human types are always POSITIVE, exactly as they appear
        # on the bill, and rate_kind says which way the money goes. Sign stays
        # meaningful only where a pricing API produces it, which is the
        # adapters in src/services/pricing and never this table.
        #
        # Any negative rate already stored was entered under the old reading,
        # so it is converted rather than left to mean something else tomorrow.
        try:
            await conn.execute(
                "ALTER TABLE utility_service_windows ADD COLUMN rate_kind "
                "TEXT NOT NULL DEFAULT 'credit'")
        except Exception:
            pass   # already present
        try:
            await conn.execute(
                "UPDATE utility_service_windows SET rate_kind = 'charge', rate = -rate "
                "WHERE rate IS NOT NULL AND rate < 0")
            # A demand window is always a charge; it has no credit reading.
            await conn.execute(
                "UPDATE utility_service_windows SET rate_kind = 'charge' "
                "WHERE window_type = 'demand'")
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (60,))
            await conn.commit()
            logger.info("DB migration v60: window rate_kind (credit|charge)")
        except Exception as _e:
            logger.warning(f"DB migration v60 failed: {_e}")

        # ── Schema v61 — the site has a name, not just an id ────────────────
        #
        # Discovery returns `site` ("Home") and `address` for every gateway, and
        # both were discarded at registration. The dashboard header fell back to
        # the id, printing "Site 3447" directly above "SITE ID: 3447".
        #
        # Existing rows are left NULL rather than backfilled here: the names
        # live in the cloud, a migration has no credentials, and inventing a
        # placeholder would be indistinguishable from a real name later.
        # api_gateways backfills on the next successful discovery.
        for _col in ("site_name", "site_address"):
            try:
                await conn.execute(f"ALTER TABLE gateways ADD COLUMN {_col} TEXT")
            except Exception:
                pass   # already present
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (61,))
            await conn.commit()
            logger.info("DB migration v61: gateways.site_name + site_address")
        except Exception as _e:
            logger.warning(f"DB migration v61 failed: {_e}")

        # ── Schema v62 — the evaluation log is not Amber's ──────────────────
        #
        # `amber_eval_log` records every dynamic-pricing evaluation, whichever
        # provider produced the prices — Amber, Localvolts, ComEd, AEMO or a
        # flat tariff. Naming it after one of them made the scheduler report
        # "source=amber_eval_log" on a site with no Amber account, which reads
        # as a fault rather than a filename.
        #
        # Amber remains a real provider (src/services/pricing/amber.py) and
        # keeps its name there. Only the generic engine concepts are renamed.
        #
        # ALTER TABLE ... RENAME TO carries the rows, so history survives.
        # A plain RENAME is not enough: the schema block above has already run
        # CREATE TABLE IF NOT EXISTS pricing_eval_log, so the destination always
        # exists by the time this executes and the rename would silently skip,
        # leaving every historical row stranded in the old table. Copy, then
        # drop.
        try:
            _cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='amber_eval_log'")
            if await _cur.fetchone():
                _cols = await conn.execute("PRAGMA table_info(amber_eval_log)")
                _names = [r[1] for r in await _cols.fetchall()]
                _cur2 = await conn.execute("PRAGMA table_info(pricing_eval_log)")
                _dest = {r[1] for r in await _cur2.fetchall()}
                _shared = [c for c in _names if c in _dest]
                _list = ", ".join(f'"{c}"' for c in _shared)
                await conn.execute(
                    f"INSERT OR IGNORE INTO pricing_eval_log ({_list}) "
                    f"SELECT {_list} FROM amber_eval_log")
                _moved = await conn.execute("SELECT COUNT(*) FROM amber_eval_log")
                _n = (await _moved.fetchone())[0]
                await conn.execute("DROP TABLE amber_eval_log")
                logger.info(
                    f"DB migration v62: amber_eval_log -> pricing_eval_log ({_n} row(s) carried over)")
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (62,))
            await conn.commit()
        except Exception as _e:
            logger.warning(f"DB migration v62 failed: {_e}")

        # Idempotently seed system immutable strategies
        try:
            await seed_system_strategies(conn)
        except Exception as _e:
            logger.warning(f"Failed to seed system strategies: {_e}")

        await conn.commit()

    logger.info(f"Database initialised at {db_path} (schema v{SCHEMA_VERSION})")


async def seed_system_strategies(conn: aiosqlite.Connection) -> None:
    """Idempotently seed default system protective rules from JSON catalog."""
    import json as _json
    seed_path = Path(__file__).parent.parent.parent / "db" / "seed" / "system_strategies_seed.json"
    if not seed_path.exists():
        logger.warning(f"seed_system_strategies: seed file not found at {seed_path}")
        return

    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            rules = _json.load(f)
    except Exception as e:
        logger.error(f"seed_system_strategies: failed to load seed JSON — {e}")
        return

    for rule in rules:
        name = rule["strategy_name"]
        # Check if this system immutable rule already exists
        async with conn.execute(
            "SELECT id, enabled FROM sd_strategy_matrix WHERE strategy_name = ? AND system_immutable = 1",
            (name,)
        ) as cur:
            row = await cur.fetchone()

        if row:
            # Rule exists — update its static parameters (conditions, signals, order, category)
            # but leave the 'enabled' toggle and 'gateway_id' untouched!
            row_id = row[0]
            query = """
                UPDATE sd_strategy_matrix
                SET trigger_category=?, conditions_json=?, signals_json=?, eval_order=?
                WHERE id=?
            """
            await conn.execute(query, (
                rule["trigger_category"],
                rule["conditions_json"],
                rule["signals_json"],
                rule["eval_order"],
                row_id
            ))
            logger.debug(f"seed_system_strategies: updated existing system rule '{name}' (id={row_id})")
        else:
            # Rule does not exist — insert it fresh!
            query = """
                INSERT INTO sd_strategy_matrix
                (gateway_id, strategy_name, trigger_category, conditions_json, signals_json, eval_order, enabled, system_immutable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            await conn.execute(query, (
                "all",
                name,
                rule["trigger_category"],
                rule["conditions_json"],
                rule["signals_json"],
                rule["eval_order"],
                1,  # default enabled
                1   # system_immutable = 1
            ))
            logger.info(f"seed_system_strategies: inserted new system rule '{name}'")
            
    await conn.commit()


# ---------------------------------------------------------------------------
# Gateway CRUD
# ---------------------------------------------------------------------------

async def get_all_gateways() -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT short_id, full_serial, name, model, site_id, site_name, site_address, group_id, group_name, profile_json, enabled, last_seen, service_amps, grid_type, gateway_phase, three_phase_group_id FROM gateways ORDER BY created_at"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_gateway_by_full_serial(full_serial: str) -> Optional[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM gateways WHERE full_serial = ?", (full_serial,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_gateway(short_id: str) -> Optional[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM gateways WHERE short_id = ?", (short_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# Alias — get_gateway already returns all columns
get_gateway_full = get_gateway


# ---------------------------------------------------------------------------
# Credential CRUD (gateway_credentials + credential_audit_log)
# ---------------------------------------------------------------------------

async def get_credentials(full_serial: str) -> Optional[dict]:
    """Return stored credentials for a gateway, decrypted, or None."""
    from src.services.crypto import decrypt_secret
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM gateway_credentials WHERE serial = ?", (full_serial,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    pw = d.get("password", "")
    if pw:
        try:
            d["password"] = decrypt_secret(pw)
        except Exception as e:
            logger.error(f"Failed to decrypt password for {full_serial[:8]}: {e}")
            d["password"] = ""  # Prevent returning corrupted ciphertext
    return d


async def get_all_credentials() -> list[dict]:
    """Return all stored gateway credentials, decrypted."""
    from src.services.crypto import decrypt_secret
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM gateway_credentials") as cur:
            rows = await cur.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        pw = d.get("password", "")
        if pw:
            try:
                d["password"] = decrypt_secret(pw)
            except Exception as e:
                logger.error(f"Failed to decrypt password for {d['serial'][:8]}: {e}")
                d["password"] = ""
        res.append(d)
    return res


async def has_credentials(full_serial: str) -> bool:
    creds = await get_credentials(full_serial)
    return creds is not None and bool(creds.get("email")) and bool(creds.get("password"))


async def upsert_credentials(
    full_serial: str,
    email: str,
    password: str,
    source: str = "ui",
    validated_at: Optional[str] = None,
) -> None:
    """Save credentials, automatically encrypting the password using AES-256-GCM."""
    from src.services.crypto import encrypt_secret
    encrypted_password = encrypt_secret(password)
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO gateway_credentials (serial, email, password, validated_at, updated_at, source)
               VALUES (?, ?, ?, ?, datetime('now'), ?)
               ON CONFLICT(serial) DO UPDATE SET
                 email=excluded.email,
                 password=excluded.password,
                 validated_at=excluded.validated_at,
                 updated_at=excluded.updated_at,
                 source=excluded.source
            """,
            (full_serial, email, encrypted_password, validated_at, source),
        )
        await conn.commit()
    await audit_credential(full_serial, "created" if not validated_at else "updated", source)


async def delete_credentials(full_serial: str) -> None:
    async with get_db() as conn:
        await conn.execute(
            "DELETE FROM gateway_credentials WHERE serial = ?", (full_serial,)
        )
        await conn.commit()
    await audit_credential(full_serial, "deleted", "ui")


async def migrate_plaintext_credentials() -> None:
    """Idempotently scan gateway_credentials and encrypt any plaintext passwords."""
    from src.services.crypto import encrypt_secret, decrypt_secret
    import base64
    async with get_db() as conn:
        async with conn.execute("SELECT serial, password FROM gateway_credentials") as cur:
            rows = await cur.fetchall()
        for serial, pw in rows:
            if not pw:
                continue
            # Try to decrypt the password. If it fails, it's plaintext!
            try:
                decrypt_secret(pw)
            except Exception as e:
                # Check if it looks like a valid base64 ciphertext
                is_encrypted = False
                try:
                    decoded = base64.b64decode(pw.encode("utf-8"), validate=True)
                    if len(decoded) >= 28:
                        is_encrypted = True
                except Exception:
                    pass

                if is_encrypted:
                    logger.critical(
                        f"⚠️ Detected encrypted credential for {serial[:8]} that cannot be decrypted. "
                        f"The machine security.key may have changed. Skipping migration to prevent double-encryption. "
                        f"Error: {e}"
                    )
                    continue

                logger.info(f"🔒 Migrating and encrypting plaintext credentials for {serial[:8]}...")
                encrypted_pw = encrypt_secret(pw)
                await conn.execute(
                    "UPDATE gateway_credentials SET password = ?, updated_at = datetime('now') WHERE serial = ?",
                    (encrypted_pw, serial)
                )
        await conn.commit()


# ---------------------------------------------------------------------------
# API Tokens & Security Event Audit Helpers
# ---------------------------------------------------------------------------

async def insert_api_token(name: str, token_hash: str, expires_at: Optional[str] = None) -> int:
    """Save a hashed API token to the database. Returns the generated ID."""
    async with get_db() as conn:
        async with conn.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
            (name, token_hash, expires_at)
        ) as cur:
            token_id = cur.lastrowid
        await conn.commit()
    return token_id


async def verify_api_token(token_hash: str) -> Optional[dict]:
    """Verify an incoming token hash against database. Updates last_used_at on success."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = dict(row)
            await conn.execute(
                "UPDATE api_tokens SET last_used_at = datetime('now') WHERE id = ?",
                (d["id"],)
            )
            await conn.commit()
            return d
    return None


async def revoke_api_token(token_id: int) -> None:
    """Delete a generated API token by its ID."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        await conn.commit()


async def get_active_api_tokens() -> list[dict]:
    """Return all active API tokens ordered by creation date."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM api_tokens ORDER BY id DESC") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Multi-user Security Helpers ─────────────────────────────────────
async def get_user(username: str) -> Optional[dict]:
    """Retrieve user dictionary by username."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def insert_user(
    username: str,
    password_hash: str,
    role: str,
    dashboard: str = "standard",
    totp_secret: Optional[str] = None,
    totp_enabled: int = 0,
    must_change_pw: int = 0,
    email: Optional[str] = None,
) -> None:
    """Insert a new user profile into config.db."""
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO users (username, password_hash, role, dashboard, totp_secret, totp_enabled, must_change_pw, email)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (username, password_hash, role, dashboard, totp_secret, totp_enabled, must_change_pw, email),
        )
        await conn.commit()


async def delete_user(username: str) -> None:
    """Delete a user profile."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM users WHERE username = ?", (username,))
        await conn.commit()


async def update_user_profile(
    username: str,
    role: str,
    dashboard: str,
    totp_secret: Optional[str],
    totp_enabled: int,
    must_change_pw: int = 0,
    password_hash: Optional[str] = None,
    email: Optional[str] = None,
) -> None:
    """Update a user's details. If password_hash is provided, it is updated too."""
    async with get_db() as conn:
        if password_hash:
            await conn.execute(
                """UPDATE users
                   SET role = ?, dashboard = ?, totp_secret = ?, totp_enabled = ?, must_change_pw = ?, password_hash = ?, email = ?, updated_at = datetime('now')
                   WHERE username = ?""",
                (role, dashboard, totp_secret, totp_enabled, must_change_pw, password_hash, email, username),
            )
        else:
            await conn.execute(
                """UPDATE users
                   SET role = ?, dashboard = ?, totp_secret = ?, totp_enabled = ?, must_change_pw = ?, email = ?, updated_at = datetime('now')
                   WHERE username = ?""",
                (role, dashboard, totp_secret, totp_enabled, must_change_pw, email, username),
            )
        await conn.commit()


async def list_users() -> list[dict]:
    """Retrieve list of all users."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM users ORDER BY username ASC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_user_count() -> int:
    """Return the total number of users."""
    async with get_db() as conn:
        async with conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0



async def log_security_event(event: str, detail: str = "", source: str = "system") -> None:
    """Append an audit trail entry to the security_audit_log table."""
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO security_audit_log (event, source, detail) VALUES (?, ?, ?)",
            (event, source, detail)
        )
        await conn.commit()


async def audit_credential(
    full_serial: str, event: str, source: str, detail: str = ""
) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO credential_audit_log (serial, event, source, detail) VALUES (?, ?, ?, ?)",
            (full_serial, event, source, detail),
        )
        await conn.commit()


async def get_credential_audit(full_serial: str = None, limit: int = 50) -> list[dict]:
    async with get_db() as conn:
        if full_serial:
            async with conn.execute(
                "SELECT * FROM credential_audit_log WHERE serial = ? ORDER BY id DESC LIMIT ?",
                (full_serial, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM credential_audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def migrate_credentials_from_json() -> int:
    """One-time migration: move credentials_json from gateways table to gateway_credentials."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT full_serial, credentials_json FROM gateways WHERE credentials_json IS NOT NULL AND credentials_json != '{}'"
        ) as cur:
            rows = await cur.fetchall()
    migrated = 0
    for row in rows:
        full_serial = row["full_serial"]
        try:
            creds = json.loads(row["credentials_json"])
            email = creds.get("email", "")
            password = creds.get("password", "")
            if email and password:
                existing = await get_credentials(full_serial)
                if not existing:
                    await upsert_credentials(full_serial, email, password, source="migration")
                    migrated += 1
        except Exception:
            pass
    return migrated


async def upsert_gateway(
    short_id: str,
    full_serial: str,
    name: str = "",
    site_id: str = "",
    site_name: str = "",
    site_address: str = "",
    model: str = "",
    profile: Optional[dict] = None,
    credentials: Optional[dict] = None,
    settings: Optional[dict] = None,
    enabled: bool = True,
    electricity_type: Optional[int] = None,
    grid_feed_max: Optional[float] = None,
    grid_max: Optional[float] = None,
    not_control_export_solar: Optional[int] = None,
) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO gateways
               (short_id, full_serial, name, site_id, site_name, site_address,
                model, profile_json,
                credentials_json, settings_json, enabled, last_seen,
                electricity_type, grid_feed_max, grid_max, not_control_export_solar)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
               ON CONFLICT(short_id) DO UPDATE SET
                 full_serial=excluded.full_serial,
                 name=excluded.name,
                 site_id=excluded.site_id,
                 -- A known name is never replaced by a blank: a later caller
                 -- that does not carry site details must not erase them.
                 site_name=COALESCE(NULLIF(excluded.site_name, ''), gateways.site_name),
                 site_address=COALESCE(NULLIF(excluded.site_address, ''), gateways.site_address),
                 model=excluded.model,
                 profile_json=excluded.profile_json,
                 credentials_json=excluded.credentials_json,
                 settings_json=excluded.settings_json,
                 enabled=excluded.enabled,
                 last_seen=excluded.last_seen,
                 electricity_type=COALESCE(excluded.electricity_type, gateways.electricity_type),
                 grid_feed_max=COALESCE(excluded.grid_feed_max, gateways.grid_feed_max),
                 grid_max=COALESCE(excluded.grid_max, gateways.grid_max),
                 not_control_export_solar=COALESCE(excluded.not_control_export_solar, gateways.not_control_export_solar)
            """,
            (
                short_id,
                full_serial,
                name,
                site_id,
                site_name,
                site_address,
                model,
                json.dumps(profile or {}),
                json.dumps(credentials or {}),
                json.dumps(settings or {}),
                1 if enabled else 0,
                electricity_type,
                grid_feed_max,
                grid_max,
                not_control_export_solar,
            ),
        )
        await conn.commit()


async def delete_gateway(short_id: str) -> None:
    async with get_db() as conn:
        await conn.execute("DELETE FROM gateways WHERE short_id = ?", (short_id,))
        await conn.commit()


async def touch_gateway(short_id: str) -> None:
    """Update last_seen timestamp."""
    async with get_db() as conn:
        await conn.execute(
            "UPDATE gateways SET last_seen = datetime('now') WHERE short_id = ?",
            (short_id,),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Battery CRUD
# ---------------------------------------------------------------------------

async def upsert_battery(
    short_id: str,
    full_serial: str,
    agate_short_id: str,
    rated_kw: float = 0.0,
    rated_kwh: float = 0.0,
    slot_index: int = 1,
) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO batteries
               (short_id, full_serial, agate_short_id, rated_kw, rated_kwh, slot_index)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(short_id) DO UPDATE SET
                 full_serial=excluded.full_serial,
                 rated_kw=excluded.rated_kw,
                 rated_kwh=excluded.rated_kwh,
                 slot_index=excluded.slot_index
            """,
            (short_id, full_serial, agate_short_id, rated_kw, rated_kwh, slot_index),
        )
        await conn.commit()


async def get_batteries_for_gateway(agate_short_id: str) -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM batteries WHERE agate_short_id = ? ORDER BY slot_index",
            (agate_short_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

async def insert_metric(short_id: str, data: dict) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO gateway_metrics (short_id, data_json) VALUES (?, ?)",
            (short_id, json.dumps(data)),
        )
        await conn.commit()


async def purge_old_metrics(days: int = 7) -> int:
    """Delete metrics older than N days. Returns rows deleted."""
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM gateway_metrics WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        await conn.commit()
        return cur.rowcount


async def purge_old_edge_metrics(days: int = 90) -> int:
    """Delete api_edge_metrics rows older than N days.

    Called periodically by GatewayService._store_metrics() to bound table growth.
    Default 90 days: at 30s poll interval that caps ~259 200 rows per gateway,
    which is ~35 MB of raw JSON blobs — well within Docker volume limits.
    Returns the number of rows deleted.
    """
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM api_edge_metrics WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        await conn.commit()
        return cur.rowcount


async def record_api_call(short_id: str, endpoint: str, latency_ms: int, status: str) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO api_performance (short_id, endpoint, latency_ms, status) VALUES (?, ?, ?, ?)",
            (short_id, endpoint, latency_ms, status),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# App config key/value store
# ---------------------------------------------------------------------------

async def get_config_value(key: str, default: Any = None) -> Any:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT value FROM app_config WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


@retry_on_lock()
async def set_config_value(key: str, value: Any) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value)),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Smart Dispatch signal bridge
# ---------------------------------------------------------------------------

async def set_sd_signal(
    short_id: str,
    signal: str,
    trigger_category: str = "",
    payload: dict | None = None,
) -> None:
    """Write the current Smart Dispatch decision signal for a gateway.

    Called every SD tick unconditionally so that Automation Builder conditions
    can read `pricing.sd_signal` and always see the current engine decision —
    even when the action has not changed from the previous cycle.

    `payload` carries SD-calculated action parameters:
        power_kw      — net available inverter kW after deducting home load / solar
        duration_mins — how many minutes the favourable price window lasts
        target_soc    — min SOC (export) or max SOC (charge) from SD config
        calc_basis    — human-readable derivation string for transparency

    Key: sd_signal_{short_id}  e.g. sd_signal_99900001
    """
    record: dict = {
        "signal": signal,
        "trigger_category": trigger_category,
    }
    if payload:
        record.update(payload)
    await set_config_value(f"sd_signal_{short_id}", record)


async def get_sd_signal(short_id: str) -> str:
    """Return the last emitted sd_signal string for a gateway.

    Returns an empty string if the engine has never ticked for this gateway,
    so AB condition `pricing.sd_signal == \"GRID_CHARGE\"` evaluates to False
    rather than raising an error.
    """
    val = await get_config_value(f"sd_signal_{short_id}", default={"signal": ""})
    return val.get("signal", "") if isinstance(val, dict) else ""


async def get_sd_signal_payload(short_id: str) -> dict:
    """Return the full SD signal payload dict for a gateway.

    Includes signal, trigger_category, and SD-calculated parameters:
    power_kw, duration_mins, target_soc, calc_basis.
    Returns a minimal dict with empty signal if no signal has been written.
    """
    val = await get_config_value(f"sd_signal_{short_id}", default={"signal": ""})
    return val if isinstance(val, dict) else {"signal": ""}


# ---------------------------------------------------------------------------
# Startup log
# ---------------------------------------------------------------------------

async def log_startup(environment: str, phase: int, details: dict = None, error: str = None) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO startup_log (environment, phase, details_json, error) VALUES (?, ?, ?, ?)",
            (environment, phase, json.dumps(details or {}), error),
        )
        await conn.commit()


async def get_startup_logs(limit: int = 50) -> list[dict]:
    """Return most recent startup log entries, newest first."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM startup_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_api_performance(short_id: str = None, limit: int = 100) -> list[dict]:
    """Return recent api_performance rows, optionally filtered by short_id."""
    async with get_db() as conn:
        if short_id:
            async with conn.execute(
                "SELECT * FROM api_performance WHERE short_id = ? ORDER BY id DESC LIMIT ?",
                (short_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM api_performance ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_recent_metrics(short_id: str = None, limit: int = 20) -> list[dict]:
    """Return recent gateway_metrics rows."""
    async with get_db() as conn:
        if short_id:
            async with conn.execute(
                "SELECT * FROM gateway_metrics WHERE short_id = ? ORDER BY id DESC LIMIT ?",
                (short_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM gateway_metrics ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]

@retry_on_lock()
async def log_admin_audit(event: str, source: str, user: str = "system", details: str = "") -> None:
    """Record a system-level administrative action."""
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO admin_audit_log (event, source, user, details) VALUES (?, ?, ?, ?)",
            (event, source, user, details),
        )
        await conn.commit()

def _security_audit_where(search: str | None) -> tuple[str, list]:
    """Shared WHERE clause so the page and its count cannot disagree.

    Written once because a count built from a different predicate than the rows
    is how "page 4 of 9" ends up empty.
    """
    if not search:
        return "", []
    like = f"%{search}%"
    return " WHERE event LIKE ? OR detail LIKE ? OR source LIKE ?", [like, like, like]


async def count_security_audit_logs(search: str | None = None) -> int:
    where, params = _security_audit_where(search)
    async with get_db() as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM security_audit_log{where}", params) as cur:
            return (await cur.fetchone())[0]


async def get_security_audit_logs(limit: int = 200, offset: int = 0,
                                  search: str | None = None) -> list[dict]:
    """Read the security audit trail.

    This table had no reader. Every security event since the feature shipped —
    logins, password changes, MFA enrolment, TLS and mTLS changes — was written
    and then displayed nowhere, while the "Audit Trail" tab showed a different
    table entirely. A trail nobody can read is not a trail; it is a log file
    with extra steps.

    Kept separate from admin_audit_log rather than merged: that table is 96%
    routine scheduler activity, and burying security events in it is how they
    became unfindable in the first place.
    """
    where, params = _security_audit_where(search)
    sql = f"SELECT id, event, source, detail, ts FROM security_audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params = [*params, limit, offset]

    async with get_db() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _admin_audit_where(category: str | None, search: str | None) -> tuple[str, list]:
    """Shared predicate for the page and its count — see _security_audit_where."""
    clauses, params = [], []
    if category:
        clauses.append("event = ?")
        params.append(category)
    if search:
        like = f"%{search}%"
        clauses.append("(event LIKE ? OR details LIKE ? OR source LIKE ? OR user LIKE ?)")
        params += [like, like, like, like]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


async def count_admin_audit_logs(category: str | None = None, search: str | None = None) -> int:
    where, params = _admin_audit_where(category, search)
    async with get_db() as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM admin_audit_log{where}", params) as cur:
            return (await cur.fetchone())[0]


async def get_gateway_metric_rows(short_id: str, limit: int = 300_000) -> list[tuple]:
    """Raw (timestamp, data_json) history for a gateway.

    Returned unparsed so the caller decides what to extract — the site profiler
    wants two fields out of a payload with hundreds, and materialising the rest
    for a quarter of a million rows would cost far more than it is worth.
    """
    try:
        async with get_db() as conn:
            async with conn.execute(
                "SELECT timestamp, data_json FROM gateway_metrics "
                "WHERE short_id = ? ORDER BY id DESC LIMIT ?",
                (short_id, limit),
            ) as cur:
                return [(r[0], r[1]) for r in await cur.fetchall()]
    except Exception:
        logger.debug("get_gateway_metric_rows: query failed", exc_info=True)
        return []


async def get_latest_tou_strategy(short_id: str | None = None) -> list:
    """The most recent TOU schedule captured for a gateway, as a strategyList.

    tou_snapshots already persists every schedule the app has seen, so the
    pricing provider can derive a forecast without a second call to the cloud.
    Returns [] rather than raising: no schedule is a normal state on a fresh
    install, and a pricing snapshot with an empty forecast is better than one
    that fails to build at all.
    """
    sql = "SELECT strategy_json FROM tou_snapshots"
    params: list = []
    if short_id:
        sql += " WHERE short_id = ?"
        params.append(short_id)
    sql += " ORDER BY id DESC LIMIT 1"

    try:
        async with get_db() as conn:
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
    except Exception:
        logger.debug("get_latest_tou_strategy: query failed", exc_info=True)
        return []

    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError):
        logger.debug("get_latest_tou_strategy: strategy_json is not valid JSON")
        return []
    return parsed if isinstance(parsed, list) else []


async def get_admin_audit_logs(
    limit: int = 100, offset: int = 0, category: str = None, search: str | None = None
) -> list[dict]:
    """Retrieve the most recent admin audit logs.

    `search` filters in SQL rather than in the caller. That distinction is the
    whole point here: this table is 96% "Automation Trigger" from a rule firing
    every five minutes, so the newest N rows are that and nothing else for any
    N a UI would ask for. Fetching a window and filtering it afterwards finds
    nothing — verified: a post-fetch filter over the newest 2000 rows returned
    zero of the 84 Config Change events.
    """
    where, params = _admin_audit_where(category, search)
    sql = f"SELECT * FROM admin_audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params = [*params, limit, offset]

    async with get_db() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_backup_history(short_id: str, limit: int = 20) -> list[dict]:
    """Return backup/reserve events for a gateway from admin_audit_log.

    Matches rows where 'event' contains 'backup' or 'reserve', or 'mode_set'
    events that mention the given short_id in the details column.
    """
    async with get_db() as conn:
        async with conn.execute(
            """SELECT id, event, source, user, details, timestamp
               FROM admin_audit_log
               WHERE (event LIKE '%backup%' OR event LIKE '%reserve%' OR event LIKE '%mode_set%')
                 AND (details LIKE ? OR ? = '')
               ORDER BY id DESC LIMIT ?""",
            (f"%{short_id}%", short_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def insert_api_edge_metrics(short_id: str, metrics: dict, edge: dict) -> None:
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO api_edge_metrics (short_id, metrics_json, edge_json) VALUES (?, ?, ?)",
            (short_id, json.dumps(metrics), json.dumps(edge)),
        )
        await conn.commit()

async def get_api_edge_metrics(short_id: str = None, limit: int = 100) -> list[dict]:
    async with get_db() as conn:
        if short_id:
            async with conn.execute(
                "SELECT * FROM api_edge_metrics WHERE short_id = ? ORDER BY id DESC LIMIT ?",
                (short_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM api_edge_metrics ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]
# ---------------------------------------------------------------------------
# BMS Telemetry Persistent Sessions
# ---------------------------------------------------------------------------

async def save_bms_session(short_id: str, battery_sn: str, session_name: str, data_json: str) -> str:
    """Save a fully serialized BMS Chart tracking payload as a named historical session."""
    import uuid
    session_id = str(uuid.uuid4())
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO bms_sessions (session_id, short_id, battery_sn, session_name, data_json) 
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, short_id, battery_sn, session_name, data_json)
        )
        await conn.commit()
    return session_id


async def get_bms_sessions(short_id: str, battery_sn: str = None) -> list[dict]:
    """Retrieve all saved BMS sessions for a gateway (omitting the bulky payload matrix)."""
    async with get_db() as conn:
        q = "SELECT session_id, short_id, battery_sn, session_name, created_at, data_json FROM bms_sessions WHERE short_id = ?"
        params = [short_id]
        if battery_sn:
            q += " AND battery_sn = ?"
            params.append(battery_sn)
        q += " ORDER BY created_at DESC"
        
        async with conn.execute(q, tuple(params)) as cur:
            rows = await cur.fetchall()
            out = []
            for row in rows:
                r = dict(row)
                try:
                    data = json.loads(r.pop("data_json", "{}"))
                    times = data.get("times", [])
                    r["record_count"] = len(times)
                    if len(times) >= 2:
                        from datetime import datetime
                        # times format: HH:MM:SS
                        t1 = datetime.strptime(times[0], "%H:%M:%S")
                        t2 = datetime.strptime(times[1], "%H:%M:%S")
                        diff = int((t2 - t1).total_seconds())
                        if diff < 0: diff += 86400  # Wrap around midnight
                        r["interval_sec"] = abs(diff)
                    else:
                        r["interval_sec"] = 0
                except Exception:
                    r["record_count"] = 0
                    r["interval_sec"] = 0
                out.append(r)
            return out


async def get_bms_session_data(session_id: str) -> dict:
    """Load the explicit un-truncated session tracking payload to push rendering matrix states."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM bms_sessions WHERE session_id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_bms_session(session_id: str) -> bool:
    """Permanently delete a given persistent charting session."""
    async with get_db() as conn:
        cur = await conn.execute("DELETE FROM bms_sessions WHERE session_id = ?", (session_id,))
        await conn.commit()
        return cur.rowcount > 0

# ---------------------------------------------------------------------------
# Automation Scheduler History
# ---------------------------------------------------------------------------

@retry_on_lock()
async def insert_automation_history(
    rule_id: str,
    rule_name: str,
    gateway_serial: str,
    action_type: str,
    action_payload: dict,
    status: str,
    detail: str = "",
    source: str = "user",
    request_id: str = None,
    ha_user_id: str = None,
) -> int:
    """Log an automation execution step to the history table and return the row ID."""
    import json as _json
    async with get_db() as conn:
        cursor = await conn.execute(
            """INSERT INTO automation_history 
               (rule_id, rule_name, gateway_serial, action_type, action_payload, status, detail, source, request_id, ha_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id, rule_name, gateway_serial, action_type, 
                _json.dumps(action_payload), status, detail, source, 
                request_id, ha_user_id
            ),
        )
        await conn.commit()
        return cursor.lastrowid

async def update_automation_history(
    id: int,
    status: str,
    detail: str = ""
) -> bool:
    """Update status and detail of an existing history entry."""
    async with get_db() as conn:
        await conn.execute(
            "UPDATE automation_history SET status = ?, detail = ? WHERE id = ?",
            (status, detail, id)
        )
        await conn.commit()
    return True

async def get_automation_history(
    limit: int = 100,
    gateway_serial: str = None,
    source: str = None,
    rule_id: str = None,
    request_id: str = None
) -> list[dict]:
    """Return recent automation trigger history, newest first. Filterable by gateway, source, rule, or request_id."""
    async with get_db() as conn:
        clauses, params = [], []
        if gateway_serial:
            clauses.append("gateway_serial = ?")
            params.append(gateway_serial)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if rule_id:
            clauses.append("rule_id = ?")
            params.append(rule_id)
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
            
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        
        async with conn.execute(
            f"SELECT * FROM automation_history {where} ORDER BY id DESC LIMIT ?",
            params
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Automation State (For Duration Timers)
# ---------------------------------------------------------------------------

async def get_automation_state(rule_id: str, gateway_serial: str) -> Optional[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM automation_state WHERE rule_id = ? AND gateway_serial = ?", 
            (rule_id, gateway_serial)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None

async def upsert_automation_state(rule_id: str, gateway_serial: str, last_true_ts: Optional[int], duration_secs: int = 0) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO automation_state (rule_id, gateway_serial, last_true_ts, duration_secs)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(rule_id, gateway_serial) DO UPDATE SET
                 last_true_ts=excluded.last_true_ts,
                 duration_secs=excluded.duration_secs
            """,
            (rule_id, gateway_serial, last_true_ts, duration_secs)
        )
        await conn.commit()

async def clear_automation_state(rule_id: str, gateway_serial: str) -> None:
    async with get_db() as conn:
        await conn.execute(
            "UPDATE automation_state SET last_true_ts = NULL WHERE rule_id = ? AND gateway_serial = ?", 
            (rule_id, gateway_serial)
        )
        await conn.commit()
# ── Pricing helpers ──────────────────────────────────────────────────────────

async def get_pricing_config() -> dict | None:
    """Return the single pricing_config row, or None."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM pricing_config ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


async def upsert_pricing_config(
    provider: str,
    region: str,
    enabled: bool,
    credentials: dict,
    settings: dict,
) -> None:
    import json, datetime as _dt
    async with get_db() as conn:
        existing = await (await conn.execute("SELECT id FROM pricing_config LIMIT 1")).fetchone()
        if existing:
            await conn.execute(
                """UPDATE pricing_config SET provider=?,region=?,enabled=?,
                   credentials=?,settings=?,updated_at=datetime('now') WHERE id=?""",
                (provider, region, int(enabled), json.dumps(credentials),
                 json.dumps(settings), existing[0])
            )
        else:
            await conn.execute(
                """INSERT INTO pricing_config(provider,region,enabled,credentials,settings)
                   VALUES(?,?,?,?,?)""",
                (provider, region, int(enabled), json.dumps(credentials), json.dumps(settings))
            )
        await conn.commit()


async def insert_price_snapshot(
    provider: str,
    import_c_kwh: float,
    export_c_kwh,
    demand_window: int,
    solar_bonus,
    tariff_type: str,
    renewables_pct,
    spike_status: str,
    interval_min: int,
    valid_until,
    forecast_json: str,
    utility_service_id: str = None
) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO pricing_snapshots
               (utility_service_id, provider, import_c_kwh, export_c_kwh, demand_window, solar_bonus, tariff_type, renewables_pct, spike_status, interval_min, valid_until, forecast_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                utility_service_id,
                provider,
                import_c_kwh,
                export_c_kwh,
                demand_window,
                solar_bonus,
                tariff_type,
                renewables_pct,
                spike_status,
                interval_min,
                valid_until,
                forecast_json
            ),
        )
        # TTL: delete rows older than 48 hours
        await conn.execute(
            "DELETE FROM pricing_snapshots WHERE timestamp < datetime('now', '-48 hours')"
        )
        await conn.commit()


async def get_latest_price(utility_service_id: str = None) -> dict | None:
    """Return the most recently stored price snapshot."""
    async with get_db() as conn:
        if utility_service_id:
            query = "SELECT * FROM pricing_snapshots WHERE utility_service_id = ? ORDER BY id DESC LIMIT 1"
            params = (utility_service_id,)
        else:
            query = "SELECT * FROM pricing_snapshots ORDER BY id DESC LIMIT 1"
            params = ()
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


async def get_price_history(hours: int = 24, utility_service_id: str = None) -> list[dict]:
    """Return recent price snapshots up to `hours` back."""
    async with get_db() as conn:
        if utility_service_id:
            query = "SELECT * FROM pricing_snapshots WHERE utility_service_id = ? AND timestamp >= datetime('now', ?) ORDER BY id DESC"
            params = (utility_service_id, f"-{hours} hours")
        else:
            query = "SELECT * FROM pricing_snapshots WHERE timestamp >= datetime('now', ?) ORDER BY id DESC"
            params = (f"-{hours} hours",)
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]


# ── Utility config helpers ────────────────────────────────────────────────────

async def get_utility_config() -> dict | None:
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM utility_config ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


async def upsert_utility_config(data: dict) -> None:
    """Upsert a single utility configuration row."""
    fields = [
        "utility_name","account_number","nmi_id","meter_serial","meter_type",
        "service_type","ac_type","voltage_v","max_demand_kva","network_area",
        "bill_frequency","bill_start_day","bill_period_days",
        "supply_charge_day","metering_fee","network_fixed_fee",
        "demand_charge_kw","demand_window_start","demand_window_end","demand_window_days",
        "fit_rate_c_kwh","fit_provider","notes",
    ]
    values = [data.get(f) for f in fields]
    async with get_db() as conn:
        existing = await (await conn.execute("SELECT id FROM utility_config LIMIT 1")).fetchone()
        if existing:
            sets = ", ".join(f"{f}=?" for f in fields)
            await conn.execute(
                f"UPDATE utility_config SET {sets}, updated_at=datetime('now') WHERE id=?",
                (*values, existing[0])
            )
        else:
            placeholders = ",".join(["?"] * len(fields))
            await conn.execute(
                f"INSERT INTO utility_config ({','.join(fields)}) VALUES ({placeholders})",
                values
            )
        await conn.commit()


# ── Smart Dispatch Matrix & Actuators (Phase 2) ──────────────────────────────────

async def get_sd_strategy_matrix(gateway_id: str = "all", include_disabled: bool = False) -> list[dict]:
    """Fetch strategy rows for a specific gateway or 'all'."""
    async with get_db() as conn:
        # Match specific gateway OR global 'all' rows
        if include_disabled:
            query = "SELECT * FROM sd_strategy_matrix WHERE (gateway_id='all' OR gateway_id=?) ORDER BY eval_order ASC"
        else:
            query = "SELECT * FROM sd_strategy_matrix WHERE enabled=1 AND (gateway_id='all' OR gateway_id=?) ORDER BY eval_order ASC"
        async with conn.execute(query, (gateway_id,)) as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]


async def get_sd_strategy_row(row_id: int) -> Optional[dict]:
    """Fetch a single strategy matrix row by id."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM sd_strategy_matrix WHERE id=?", (row_id,)) as cur:
            row = await cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
            return None


async def get_sd_actuators() -> list[dict]:
    """Fetch all configured actuators."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM sd_actuator_map") as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]


async def upsert_sd_strategy_row(row: dict) -> int:
    """Insert or update a strategy matrix row."""
    async with get_db() as conn:
        if row.get("id"):
            query = """
                UPDATE sd_strategy_matrix 
                SET gateway_id=?, strategy_name=?, trigger_category=?, 
                    conditions_json=?, signals_json=?, eval_order=?, enabled=?,
                    forecast_weight=?, intent_duration_mins=?, system_immutable=?
                WHERE id=?
            """
            params = (
                row.get("gateway_id", "all"),
                row.get("strategy_name"),
                row.get("trigger_category"),
                row.get("conditions_json", "{}"),
                row.get("signals_json", "[]"),
                row.get("eval_order", 100),
                row.get("enabled", 1),
                row.get("forecast_weight", 1.0),
                row.get("intent_duration_mins"),
                row.get("system_immutable", 0),
                row["id"]
            )
            await conn.execute(query, params)
            await conn.commit()
            return row["id"]
        else:
            query = """
                INSERT INTO sd_strategy_matrix 
                (gateway_id, strategy_name, trigger_category, conditions_json, signals_json, eval_order, enabled,
                 forecast_weight, intent_duration_mins, system_immutable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                row.get("gateway_id", "all"),
                row.get("strategy_name"),
                row.get("trigger_category"),
                row.get("conditions_json", "{}"),
                row.get("signals_json", "[]"),
                row.get("eval_order", 100),
                row.get("enabled", 1),
                row.get("forecast_weight", 1.0),
                row.get("intent_duration_mins"),
                row.get("system_immutable", 0)
            )
            async with conn.execute(query, params) as cur:
                row_id = cur.lastrowid
                await conn.commit()
                return row_id


async def delete_sd_strategy_row(row_id: int) -> bool:
    """Delete a strategy matrix row."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM sd_strategy_matrix WHERE id=?", (row_id,))
        await conn.commit()
        return True


async def upsert_sd_actuator(row: dict) -> str:
    """Insert or update an actuator mapping."""
    async with get_db() as conn:
        query = """
            INSERT INTO sd_actuator_map (sd_signal, action, payload_json, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sd_signal) DO UPDATE SET
                action=excluded.action,
                payload_json=excluded.payload_json,
                description=excluded.description
        """
        params = (
            row["sd_signal"],
            row["action"],
            row.get("payload_json", "{}"),
            row.get("description")
        )
        await conn.execute(query, params)
        await conn.commit()
        return row["sd_signal"]


# ── Device Registry ───────────────────────────────────────────────────────────
# CRUD helpers for device_models and device_accessories tables.
# Fallback chain for model resolution (get_device_model):
#   1. device_models WHERE user_override_name IS NOT NULL AND hw_version_int=?  → admin override
#   2. device_models WHERE hw_version_int=?                                      → seeded row
#   3. FRANKLINWH_MODELS Python dict (cloud library, read-only)                  → lib fallback
#   4. sentinel {"hw_version_int": n, "name": "Unknown", "model": "aGate (HW vN)", "sku": ""}


async def seed_device_catalog(seed_path=None) -> None:
    """Idempotent startup seed — populates device_models and device_accessories.

    Strategy:
      1. Always seed from FRANKLINWH_MODELS + FRANKLINWH_ACCESSORIES Python constants
         (read-only lib, always available regardless of Docker volume mounts).
      2. If seed_path JSON exists, its enriched rows override the lib constants
         (real_hw_version, notes, V2L flags, etc.).
      3. INSERT OR IGNORE — user overrides (source='user') are never clobbered.
    """
    import json as _json

    # ── 1. Build baseline from Python constants (always available) ──────────────
    from franklinwh_cloud.const.devices import FRANKLINWH_MODELS, FRANKLINWH_ACCESSORIES

    baseline_models: dict = {}
    for hw_ver, m in FRANKLINWH_MODELS.items():
        # BD-06 / #18 (2026-08-08): device_class derived from model shape,
        # not a hardcoded hw_version whitelist. Previous {0..6} set
        # silently misclassified any new aPower hw_version as "agate"
        # when the cloud client catalog grew. Rule: SKU prefix "AGT-*"
        # (or name starts with "aGate") → agate; anything else → apower.
        sku = str(m.get("sku", "")).upper()
        name = str(m.get("name", ""))
        is_agate = sku.startswith("AGT-") or name.startswith("aGate")
        device_class = "agate" if is_agate else "apower"
        # has_mppt: prefer the catalog's own flag (v0.4.9+ exposes it),
        # fall back to name-string match for older client versions.
        catalog_mppt = m.get("has_mppt")
        if catalog_mppt is not None:
            has_mppt = 1 if catalog_mppt else 0
        else:
            has_mppt = 1 if "aPower S" in name else 0
        baseline_models[hw_ver] = {
            "hw_version_int":  hw_ver,
            "device_class":    device_class,
            "api_field_name":  "peHwVersion" if device_class == "apower" else "sysHdVersionInt",
            "real_hw_version": None,
            "name":            m.get("name", "Unknown"),
            "sku":             m.get("sku", ""),
            "model":           m.get("model", ""),
            "country_id":      None,
            "generation":      None,
            "has_mppt":        has_mppt,
            "type":            "stackable" if has_mppt else None,
            "notes":           None,
            "is_deprecated":   0,
            "source":          "seed",
            "ac_type":         "split" if device_class == "agate" and m.get("sku", "").endswith("US") else "single" if m.get("sku", "").endswith("AU") else None,
        }

    baseline_accessories: dict = {}
    for acc_id, a in FRANKLINWH_ACCESSORIES.items():
        compat_raw = a.get("compatiable", "")  # note: library typo
        if compat_raw == "ALL":
            compat_json = "ALL"
        elif compat_raw:
            nums = [int(x) for x in compat_raw.split("|") if x.strip().isdigit()]
            compat_json = _json.dumps(nums) if nums else None
        else:
            compat_json = None
        acc_name = a.get("name", "Unknown")
        name_lower = acc_name.lower()
        if "generator" in name_lower:
            acc_type, api_type = "generator", 3
        elif "smart circuits" in name_lower:
            acc_type, api_type = "smart_circuits", 4
        elif "apbox" in name_lower:
            acc_type, api_type = "apbox", None
        elif "split-ct" in name_lower or "split_ct" in name_lower:
            acc_type, api_type = "split_ct", None
        elif "ahub" in name_lower:
            acc_type, api_type = "ahub", None
        elif "meter adapter" in name_lower or "mac" in name_lower:
            acc_type, api_type = "mac1", None
        else:
            acc_type, api_type = "unknown", None
        is_apower_acc = acc_type in ("ahub", "mac1")
        baseline_accessories[acc_id] = {
            "accessory_id":       acc_id,
            "api_accessory_type": api_type,
            "name":               acc_name,
            "sku":                a.get("sku", "").strip(),
            "accessory_type":     acc_type,
            "version":            None,
            "country_id":         None,
            "compatible_agates":  compat_json if not is_apower_acc else None,
            "compatible_apower":  compat_json if is_apower_acc else None,
            "circuit_count":      None,
            "v2l_port":           0,
            "v2l_enables":        0,
            "v2l_requires_gen":   0,
            "notes":              None,
            "is_deprecated":      0,
            "source":             "seed",
        }

    # ── 2. Overlay enriched JSON seed if available ───────────────────────────────
    if seed_path is not None and seed_path.exists():
        try:
            seed_data = _json.loads(seed_path.read_text())
            for row in seed_data.get("device_models", []):
                hw = row.get("hw_version_int")
                if hw is not None:
                    baseline_models[hw] = row
            for row in seed_data.get("device_accessories", []):
                aid = row.get("accessory_id")
                if aid is not None:
                    baseline_accessories[aid] = row
            logger.info(f"Device catalog: enriched from {seed_path.name}")
        except Exception as e:
            logger.warning(f"Device catalog: seed JSON error ({e}) — using lib constants only")
    else:
        logger.info("Device catalog: seed JSON not mounted — seeding from FRANKLINWH_MODELS + FRANKLINWH_ACCESSORIES constants")

    # ── 3. Write to DB (INSERT OR IGNORE) ────────────────────────────────────────
    model_fields = [
        "hw_version_int", "device_class", "api_field_name", "real_hw_version",
        "name", "sku", "model", "country_id", "generation", "has_mppt",
        "type", "notes", "is_deprecated", "source",
        # v22 hardware spec fields
        "max_service_amps", "nominal_kw", "peak_kw", "max_ac_amps", "ac_hz",
        "mppt_count", "mppt_isc_amps", "mppt_imp_amps", "mppt_max_kw",
        "ac_solar_max_kw", "rated_kwh", "sku_region", "ac_type",
    ]
    acc_fields = [
        "accessory_id", "api_accessory_type", "name", "sku",
        "accessory_type", "version", "country_id",
        "compatible_agates", "compatible_apower",
        "circuit_count", "v2l_port", "v2l_enables", "v2l_requires_gen",
        "notes", "is_deprecated", "source",
        # v22
        "ac_hz", "max_amps", "capability_mapping",
    ]

    async with get_db() as conn:
        existing = await (await conn.execute("SELECT COUNT(*) FROM device_models")).fetchone()
        if existing and existing[0] > 0:
            logger.debug("Device catalog already seeded — skipping.")
            return

        models_inserted = 0
        for row in baseline_models.values():
            vals = [row.get(f) for f in model_fields]
            ph = ",".join(["?"] * len(model_fields))
            await conn.execute(
                f"INSERT OR IGNORE INTO device_models ({','.join(model_fields)}) VALUES ({ph})",
                vals,
            )
            models_inserted += 1

        accessories_inserted = 0
        for row in baseline_accessories.values():
            vals = [row.get(f) for f in acc_fields]
            ph = ",".join(["?"] * len(acc_fields))
            await conn.execute(
                f"INSERT OR IGNORE INTO device_accessories ({','.join(acc_fields)}) VALUES ({ph})",
                vals,
            )
            accessories_inserted += 1

        await conn.commit()

    logger.info(
        f"Device catalog seeded: {models_inserted} models, {accessories_inserted} accessories"
    )


async def update_gateway_model(short_id: str, model: str) -> bool:
    """Set a gateway's model name. Returns True if a row changed.

    `model` is otherwise only written at registration, which is why a name
    fabricated from the hardware version survived the fix to the derivation —
    see src/services/model_repair.py.
    """
    if not short_id or not model:
        return False
    async with get_db() as conn:
        cur = await conn.execute(
            "UPDATE gateways SET model = ? WHERE short_id = ?", (model, short_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_device_model(hw_version_int: int) -> "dict | None":
    """Resolve a model row. Returns None if not found (caller falls back to lib dict)."""
    async with get_db() as conn:
        row = await (await conn.execute(
            "SELECT * FROM device_models WHERE hw_version_int=?",
            (hw_version_int,),
        )).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("user_override_name"):
        d["name"] = d["user_override_name"]
    return d


async def list_device_models(device_class: "str | None" = None) -> "list[dict]":
    """Return all device model rows, optionally filtered by device_class."""
    async with get_db() as conn:
        if device_class:
            rows = await (await conn.execute(
                "SELECT * FROM device_models WHERE device_class=? ORDER BY device_class, hw_version_int",
                (device_class,),
            )).fetchall()
        else:
            rows = await (await conn.execute(
                "SELECT * FROM device_models ORDER BY device_class, hw_version_int"
            )).fetchall()
    return [dict(r) for r in rows]


async def upsert_device_model(hw_version_int: int, **kwargs) -> None:
    """Insert or update a device model row (admin-added rows use source=user)."""
    kwargs.setdefault("source", "user")
    fields = list(kwargs.keys())
    vals = list(kwargs.values())
    sets = ", ".join(f"{f}=?" for f in fields)
    async with get_db() as conn:
        existing = await (await conn.execute(
            "SELECT hw_version_int FROM device_models WHERE hw_version_int=?",
            (hw_version_int,),
        )).fetchone()
        if existing:
            await conn.execute(
                f"UPDATE device_models SET {sets}, updated_at=datetime('now') WHERE hw_version_int=?",
                (*vals, hw_version_int),
            )
        else:
            all_fields = ["hw_version_int"] + fields
            ph = ",".join(["?"] * len(all_fields))
            await conn.execute(
                f"INSERT INTO device_models ({','.join(all_fields)}) VALUES ({ph})",
                [hw_version_int] + vals,
            )
        await conn.commit()


async def tombstone_device_model(hw_version_int: int, note: str = "") -> None:
    """Soft-delete a device model (sets is_deprecated=1)."""
    async with get_db() as conn:
        await conn.execute(
            """UPDATE device_models
               SET is_deprecated=1, deprecated_since=date('now'), deprecated_note=?,
                   updated_at=datetime('now')
               WHERE hw_version_int=?""",
            (note, hw_version_int),
        )
        await conn.commit()


async def get_accessory(accessory_id: int) -> "dict | None":
    """Return a single accessory row by its FHAI-internal ID."""
    async with get_db() as conn:
        row = await (await conn.execute(
            "SELECT * FROM device_accessories WHERE accessory_id=?",
            (accessory_id,),
        )).fetchone()
    return dict(row) if row else None


async def list_accessories(accessory_type: "str | None" = None) -> "list[dict]":
    """Return all accessory rows, optionally filtered by accessory_type."""
    async with get_db() as conn:
        if accessory_type:
            rows = await (await conn.execute(
                "SELECT * FROM device_accessories WHERE accessory_type=? ORDER BY country_id, accessory_id",
                (accessory_type,),
            )).fetchall()
        else:
            rows = await (await conn.execute(
                "SELECT * FROM device_accessories ORDER BY accessory_type, country_id, accessory_id"
            )).fetchall()
    return [dict(r) for r in rows]


async def upsert_accessory(accessory_id: int, **kwargs) -> None:
    """Insert or update an accessory row (admin-added rows use source=user)."""
    kwargs.setdefault("source", "user")
    fields = list(kwargs.keys())
    vals = list(kwargs.values())
    sets = ", ".join(f"{f}=?" for f in fields)
    async with get_db() as conn:
        existing = await (await conn.execute(
            "SELECT accessory_id FROM device_accessories WHERE accessory_id=?",
            (accessory_id,),
        )).fetchone()
        if existing:
            await conn.execute(
                f"UPDATE device_accessories SET {sets}, updated_at=datetime('now') WHERE accessory_id=?",
                (*vals, accessory_id),
            )
        else:
            all_fields = ["accessory_id"] + fields
            ph = ",".join(["?"] * len(all_fields))
            await conn.execute(
                f"INSERT INTO device_accessories ({','.join(all_fields)}) VALUES ({ph})",
                [accessory_id] + vals,
            )
        await conn.commit()


async def get_compatible_accessories(hw_version_int: int, device_class: str) -> "list[dict]":
    """Return accessories compatible with a given model (reverse lookup for UI)."""
    import json as _json
    all_acc = await list_accessories()
    result = []
    for acc in all_acc:
        compat = acc.get("compatible_agates") if device_class == "agate" else acc.get("compatible_apower")
        if not compat:
            continue
        if compat == "ALL":
            result.append(acc)
        else:
            try:
                if hw_version_int in _json.loads(compat):
                    result.append(acc)
            except (ValueError, TypeError):
                pass
    return result
# ---------------------------------------------------------------------------
# TOU Snapshot helpers (Audit Trail)
# ---------------------------------------------------------------------------

def _describe_strategy(strategy_list: list) -> str:
    """A short human description of a schedule: what distinguishes it.

    Used for snapshot labels, where a bare timestamp tells the reader nothing
    about which of nine near-identical entries is the one they want.
    """
    seasons = len(strategy_list or [])
    if not seasons:
        return "Empty schedule"

    blocks = 0
    names = []
    for season in strategy_list:
        names.append(str(season.get("seasonName") or season.get("name") or "").strip())
        for day_type in (season.get("dayTypeVoList") or []):
            blocks += len(day_type.get("detailVoList") or [])

    named = [n for n in names if n and not n.lower().startswith("season ")]
    suffix = f" ({', '.join(named[:2])})" if named else ""
    season_word = "season" if seasons == 1 else "seasons"
    block_word = "block" if blocks == 1 else "blocks"
    return f"{seasons} {season_word}, {blocks} {block_word}{suffix}"


async def insert_tou_snapshot(
    short_id: str,
    strategy_list: list,
    label: str = None,
    source: str = "gateway_save",
) -> int:
    """Append a new TOU snapshot and return its row ID."""
    from datetime import datetime, timezone
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    season_count = len(strategy_list)
    # A timestamp alone made the list unusable: nine consecutive entries read
    # "Saved <date>" and the only way to tell them apart was to open each one.
    # The shape of the schedule is what a person is actually choosing between.
    auto_label = label or f"{_describe_strategy(strategy_list)} — {ts_now}"
    async with get_db() as conn:
        cur = await conn.execute(
            """INSERT INTO tou_snapshots (short_id, label, strategy_json, season_count, source)
               VALUES (?, ?, ?, ?, ?)""",
            (short_id, auto_label, json.dumps(strategy_list), season_count, source),
        )
        await conn.commit()
        return cur.lastrowid


async def record_observed_tou(short_id: str, strategy_list: list) -> bool:
    """Snapshot the gateway's schedule when it differs from what we last knew.

    Every snapshot until now came from a write FWHAI made itself — sources
    'gateway_save' and 'smart_dispatch'. So the history answered "what did we
    do", never "what does the gateway hold", and a schedule or tariff changed
    in the FranklinWH app was invisible: no record, no audit entry, and no way
    to tell that local state had gone stale.

    Compared against the newest snapshot regardless of its source, so a change
    is recorded once rather than on every poll. Returns True when drift was
    found and stored.
    """
    if not strategy_list:
        return False

    try:
        async with get_db() as conn:
            async with conn.execute(
                "SELECT strategy_json FROM tou_snapshots WHERE short_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (short_id,),
            ) as cur:
                row = await cur.fetchone()
    except Exception:
        logger.debug("record_observed_tou: lookup failed", exc_info=True)
        return False

    incoming = json.dumps(strategy_list, sort_keys=True)
    if row and row[0]:
        try:
            if json.dumps(json.loads(row[0]), sort_keys=True) == incoming:
                return False          # unchanged — nothing to record
        except (TypeError, ValueError):
            pass                      # unparseable previous: treat as drift

    await insert_tou_snapshot(
        short_id, strategy_list,
        label=f"Observed on gateway {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        source="observed",
    )
    await log_admin_audit(
        event="Gateway:TOU_Changed_Externally",
        source="poll",
        details=(
            f"[{short_id}] The gateway's TOU schedule differs from the last known "
            f"state ({len(strategy_list)} seasons). Changed outside this app — "
            f"most likely in the FranklinWH app."
        ),
    )
    logger.info(
        "[%s] TOU schedule changed outside FWHAI — snapshot recorded (%d seasons)",
        short_id, len(strategy_list),
    )
    return True


async def get_tou_snapshots(
    short_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Recent TOU snapshots for a gateway — no strategy_json, so the list is cheap.

    The old default of 10 was a hard cap with no way past it: a gateway with 93
    snapshots offered ten, and the rest were reachable only by querying the
    database for an id. Paged now, so the history is actually navigable.
    """
    async with get_db() as conn:
        async with conn.execute(
            """SELECT id, short_id, label, season_count, source, ts
               FROM tou_snapshots
               WHERE short_id = ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (short_id, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_tou_snapshots(short_id: str) -> int:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM tou_snapshots WHERE short_id = ?", (short_id,)
        ) as cur:
            return (await cur.fetchone())[0]


async def delete_tou_snapshot(short_id: str, snapshot_id: int) -> bool:
    """Remove one snapshot. Scoped by gateway so an id cannot cross sites."""
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM tou_snapshots WHERE id = ? AND short_id = ?",
            (snapshot_id, short_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def prune_tou_snapshots(short_id: str, keep: int = 200) -> int:
    """Trim a gateway's snapshot history to the newest `keep`.

    Not part of the nightly telemetry prune, and deliberately count-based
    rather than age-based: these are restore points, and the useful one may be
    the schedule from before last summer. They are also tiny — 2.6 KB each, so
    200 is about half a megabyte — and only written on an actual change, so the
    table cannot run away on its own. This exists to be invoked, not to run
    behind the user's back.
    """
    if keep < 1:
        return 0
    async with get_db() as conn:
        cur = await conn.execute(
            """DELETE FROM tou_snapshots
               WHERE short_id = ? AND id NOT IN (
                   SELECT id FROM tou_snapshots WHERE short_id = ?
                   ORDER BY id DESC LIMIT ?
               )""",
            (short_id, short_id, keep),
        )
        await conn.commit()
        return cur.rowcount


async def get_tou_snapshot_by_id(snapshot_id: int) -> Optional[dict]:
    """Fetch a single snapshot including the full strategy_json blob."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM tou_snapshots WHERE id = ?", (snapshot_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Automation Rulebook & Rule CRUD (B1)
# ---------------------------------------------------------------------------

async def get_active_rulebook(provider: str = None) -> Optional[dict]:
    """Return the active rulebook, optionally filtered by provider."""
    async with get_db() as conn:
        q = "SELECT * FROM automation_rulebooks WHERE is_active = 1"
        params: list = []
        if provider:
            q += " AND provider = ?"
            params.append(provider)
        q += " ORDER BY id DESC LIMIT 1"
        async with conn.execute(q, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_rulebooks() -> list[dict]:
    """Return all rulebooks."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM automation_rulebooks ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_active_rules(provider: str = None) -> list[dict]:
    """Return enabled rules from the active rulebook, ordered by priority."""
    rb = await get_active_rulebook(provider)
    if not rb:
        return []
    async with get_db() as conn:
        async with conn.execute(
            """SELECT * FROM automation_rules
               WHERE rulebook_id = ? AND enabled = 1
               ORDER BY priority ASC""",
            (rb["rulebook_id"],),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_rules(rulebook_id: str = None) -> list[dict]:
    """Return all rules (enabled or not) for a rulebook, or all rules if no book specified."""
    async with get_db() as conn:
        if rulebook_id:
            async with conn.execute(
                "SELECT * FROM automation_rules WHERE rulebook_id = ? ORDER BY priority",
                (rulebook_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM automation_rules ORDER BY rulebook_id, priority"
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_rule(
    rule_id: str,
    rulebook_id: str,
    name: str,
    description: str,
    priority: int,
    enabled: bool,
    condition_json: str,
    action: str,
    action_params: str = "{}",
    cooldown_min: int = 30,
    provider_scope: Optional[str] = None,
    gateway_scope: Optional[str] = None,
) -> None:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO automation_rules
               (rule_id, rulebook_id, name, description, priority, enabled,
                condition_json, action, action_params, cooldown_min,
                provider_scope, gateway_scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rule_id) DO UPDATE SET
                 name=excluded.name,
                 description=excluded.description,
                 priority=excluded.priority,
                 enabled=excluded.enabled,
                 condition_json=excluded.condition_json,
                 action=excluded.action,
                 action_params=excluded.action_params,
                 cooldown_min=excluded.cooldown_min,
                 provider_scope=excluded.provider_scope,
                 gateway_scope=excluded.gateway_scope,
                 updated_at=datetime('now')
            """,
            (
                rule_id, rulebook_id, name, description, priority, 1 if enabled else 0,
                condition_json, action, action_params, cooldown_min,
                provider_scope, gateway_scope,
            ),
        )
        await conn.commit()


async def toggle_rule(rule_id: str, enabled: bool) -> bool:
    """Enable or disable a single rule. Returns False if rule not found."""
    async with get_db() as conn:
        cur = await conn.execute(
            "UPDATE automation_rules SET enabled=?, updated_at=datetime('now') WHERE rule_id=?",
            (1 if enabled else 0, rule_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def update_rule_priorities(rule_ids: list[str]) -> bool:
    """Update priorities for a list of rule IDs, setting priority to their index + 1."""
    if not rule_ids:
        return True
    
    async with get_db() as conn:
        for index, rule_id in enumerate(rule_ids):
            await conn.execute(
                "UPDATE automation_rules SET priority=?, updated_at=datetime('now') WHERE rule_id=?",
                (index + 1, rule_id)
            )
        await conn.commit()
        return True



async def delete_rule(rule_id: str) -> bool:
    """Delete a rule (only allowed for user-created rules, not system defaults)."""
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM automation_rules WHERE rule_id = ?", (rule_id,)
        )
        await conn.commit()
        return cur.rowcount > 0


async def log_automation_trigger(
    rule_id: str,
    rule_name: str,
    gateway_serial: str,
    action_type: str,
    status: str,
    detail: str = "",
    action_payload: dict = None,
    source: str = "edge",
    request_id: str = None,
    ha_user_id: str = None,
) -> None:
    """Alias for insert_automation_history with optional payload."""
    await insert_automation_history(
        rule_id, rule_name, gateway_serial, action_type, action_payload or {}, status, detail, source,
        request_id=request_id, ha_user_id=ha_user_id
    )




async def get_engine_mode(rulebook_id: str = "amber-au-default") -> str:
    """Return the engine_mode for the given rulebook ('signal_only'|'active'|'paused')."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT engine_mode FROM automation_rulebooks WHERE rulebook_id = ?",
            (rulebook_id,),
        ) as cur:
            row = await cur.fetchone()
    return (row["engine_mode"] if row and row["engine_mode"] else "signal_only")


async def set_engine_mode(mode: str, rulebook_id: str = "amber-au-default") -> bool:
    """Persist engine_mode. Returns True if a row was updated or created."""
    valid = {"signal_only", "active", "paused"}
    if mode not in valid:
        return False
    async with get_db() as conn:
        # Ensure rulebook exists
        await conn.execute(
            """INSERT OR IGNORE INTO automation_rulebooks 
               (rulebook_id, name, description, engine_mode, created_at, updated_at) 
               VALUES (?, 'Default Rulebook', 'Auto-generated', ?, datetime('now'), datetime('now'))""",
            (rulebook_id, mode)
        )
        cur = await conn.execute(
            """UPDATE automation_rulebooks
               SET engine_mode=?, updated_at=datetime('now')
               WHERE rulebook_id=?""",
            (mode, rulebook_id),
        )
        await conn.commit()
        return True


async def get_notification_settings() -> dict:
    """Return the singleton notification settings row."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM automation_notification_settings WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {
            "id": 1, "enabled": 0, "ha_target": "",
            "triggers": [], "actionable": 0, "actionable_ttl": 1800,
        }
    d = dict(row)
    import json as _json
    try:
        d["triggers"] = _json.loads(d.get("triggers") or "[]")
    except Exception:
        d["triggers"] = []
    return d


async def upsert_notification_settings(s: dict) -> None:
    """Insert or replace the notification settings singleton."""
    import json as _json
    triggers = _json.dumps(s.get("triggers", []))
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO automation_notification_settings
               (id, enabled, ha_target, triggers, actionable, actionable_ttl, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 enabled=excluded.enabled,
                 ha_target=excluded.ha_target,
                 triggers=excluded.triggers,
                 actionable=excluded.actionable,
                 actionable_ttl=excluded.actionable_ttl,
                 updated_at=excluded.updated_at""",
            (
                int(bool(s.get("enabled", False))),
                s.get("ha_target", ""),
                triggers,
                int(bool(s.get("actionable", False))),
                int(s.get("actionable_ttl", 1800)),
            ),
        )
        await conn.commit()


# ── Notification cooldown (backlog P2 dated 2026-07-09, schema v47) ─────────
# Suppresses same-rule notification re-fires after a pending_approval TTL
# expires without user response. See src/services/db.py migration v47 for
# schema. See src/services/smart_dispatch.py notification hook for the check
# that gates emit vs SUPPRESSED audit-log entry.

async def get_notification_cooldown_rules() -> list[dict]:
    """Return all configured cooldown rules with their current settings."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT rule_id, label, cooldown_seconds, enabled, updated_at "
            "FROM notification_cooldown_rules ORDER BY rule_id"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_notification_cooldown_rule(
    rule_id: str,
    *,
    cooldown_seconds: int | None = None,
    enabled: int | None = None,
) -> bool:
    """Update a cooldown rule's settings. Returns True if a row was updated."""
    updates = []
    params: list = []
    if cooldown_seconds is not None:
        updates.append("cooldown_seconds = ?")
        params.append(int(cooldown_seconds))
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not updates:
        return False
    updates.append("updated_at = datetime('now')")
    params.append(rule_id)
    async with get_db() as conn:
        cur = await conn.execute(
            f"UPDATE notification_cooldown_rules SET {', '.join(updates)} WHERE rule_id = ?",
            params,
        )
        await conn.commit()
        return (cur.rowcount or 0) > 0


async def is_notification_cooldown_active(
    gateway_serial: str, rule_id: str
) -> "dict | None":
    """Return the active cooldown record for (gw, rule) if one exists and
    hasn't expired. None otherwise. Rules with enabled=0 always return None
    (they act as if cooldown is disabled). Caller should check_the returned
    dict's expires_at to compose the user-facing detail line."""
    import time as _time
    now = int(_time.time())
    async with get_db() as conn:
        # First: is the rule even enabled?
        async with conn.execute(
            "SELECT enabled, cooldown_seconds FROM notification_cooldown_rules WHERE rule_id = ?",
            (rule_id,),
        ) as cur:
            rule_row = await cur.fetchone()
        if not rule_row or not int(rule_row["enabled"] or 0):
            return None
        # Then: is there an active cooldown record?
        async with conn.execute(
            "SELECT id, gateway_serial, rule_id, decision_hash, expires_at, ignored_count "
            "FROM notification_cooldown "
            "WHERE gateway_serial = ? AND rule_id = ? AND expires_at > ? "
            "ORDER BY expires_at DESC LIMIT 1",
            (gateway_serial, rule_id, now),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def set_notification_cooldown(
    gateway_serial: str,
    rule_id: str,
    *,
    decision_hash: str | None = None,
    ttl_seconds: int | None = None,
) -> None:
    """Register a new cooldown for (gateway, rule). ttl_seconds defaults to
    the rule's configured cooldown_seconds if not overridden. Bumps
    ignored_count if there's already an active cooldown for the same
    (gateway, rule, decision_hash) tuple — rare edge case."""
    import time as _time
    async with get_db() as conn:
        # Get the rule's configured cooldown length if not overridden
        if ttl_seconds is None:
            async with conn.execute(
                "SELECT cooldown_seconds FROM notification_cooldown_rules WHERE rule_id = ?",
                (rule_id,),
            ) as cur:
                r = await cur.fetchone()
            ttl_seconds = int(r["cooldown_seconds"]) if r else 7200
        expires_at = int(_time.time()) + int(ttl_seconds)
        # Look for an active cooldown to bump ignored_count on
        async with conn.execute(
            "SELECT id, ignored_count FROM notification_cooldown "
            "WHERE gateway_serial = ? AND rule_id = ? AND expires_at > ? "
            "ORDER BY expires_at DESC LIMIT 1",
            (gateway_serial, rule_id, int(_time.time())),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            await conn.execute(
                "UPDATE notification_cooldown SET expires_at = ?, ignored_count = ? WHERE id = ?",
                (expires_at, int(existing["ignored_count"] or 1) + 1, existing["id"]),
            )
        else:
            await conn.execute(
                "INSERT INTO notification_cooldown "
                "(gateway_serial, rule_id, decision_hash, expires_at, ignored_count) "
                "VALUES (?, ?, ?, ?, 1)",
                (gateway_serial, rule_id, decision_hash, expires_at),
            )
        await conn.commit()


async def sweep_expired_pendings_and_arm_cooldowns(
    gateway_serial: str,
) -> int:
    """Sweep pending_approvals for a gateway that have expired without user
    response, arm a cooldown for each's `ev_key` (stored in action_context
    when the engine created the record), and delete the pending. Returns the
    number of cooldowns armed.

    This is the mechanism that converts "user ignored the notification" into
    "suppress future same-rule notifications for the cooldown window." Called
    from the SD engine's notification hook BEFORE the per-tick emit check.

    Only records with responded_at IS NULL are considered "ignored." Records
    that were explicitly responded to (Approve/Skip/Override) already have
    responded_at set and don't count as ignored.
    """
    import time as _time
    now = int(_time.time())
    armed = 0
    async with get_db() as conn:
        async with conn.execute(
            "SELECT request_id, rule_id, action_context FROM pending_approvals "
            "WHERE gateway_serial = ? AND expires_at < ? AND responded_at IS NULL",
            (gateway_serial, now),
        ) as cur:
            rows = await cur.fetchall()

    for r in rows:
        try:
            ctx_raw = r["action_context"] or "{}"
            ctx = json.loads(ctx_raw) if isinstance(ctx_raw, str) else (ctx_raw or {})
            ev_key = ctx.get("ev_key") or ""
        except Exception:
            ev_key = ""
        if not ev_key:
            # Legacy record (pre-v47) without ev_key in context — best-effort
            # skip the arm; still delete the pending so we don't loop over it.
            async with get_db() as conn:
                await conn.execute(
                    "DELETE FROM pending_approvals WHERE request_id = ?",
                    (r["request_id"],),
                )
                await conn.commit()
            continue
        # Arm the cooldown
        await set_notification_cooldown(
            gateway_serial=gateway_serial,
            rule_id=ev_key,
        )
        # Delete the expired pending
        async with get_db() as conn:
            await conn.execute(
                "DELETE FROM pending_approvals WHERE request_id = ?",
                (r["request_id"],),
            )
            await conn.commit()
        armed += 1
    return armed


async def prune_expired_notification_cooldowns() -> int:
    """Delete cooldown rows past their expires_at + 24h grace period.
    Returns the number of rows deleted. Called from the poll loop's
    housekeeping tick."""
    import time as _time
    cutoff = int(_time.time()) - 86400  # 24h grace
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM notification_cooldown WHERE expires_at < ?", (cutoff,)
        )
        await conn.commit()
        return cur.rowcount or 0


async def add_notification_log(
    direction: str,
    event: str,
    details: str,
    details_json: Optional[str] = None,
) -> None:
    """Add a bounded entry to the notification debug log.

    Batch N (2026-07-25) — `details` should be a brief human summary
    (≤ ~100 chars) suitable for at-a-glance rendering in the SD Notif
    Audit Ledger table. `details_json` is optional full-fidelity JSON
    (payload, HA responses, etc.) for opt-in expand-on-demand UI.
    Historical call sites without details_json still work — they just
    persist as before with details_json=NULL.
    """
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO automation_notification_log "
            "(direction, event, details, details_json) VALUES (?, ?, ?, ?)",
            (direction, event, details, details_json)
        )
        # Keep only the last 100 entries
        await conn.execute(
            "DELETE FROM automation_notification_log WHERE id NOT IN (SELECT id FROM automation_notification_log ORDER BY id DESC LIMIT 100)"
        )
        await conn.commit()


async def get_notification_logs(limit: int = 50) -> list[dict]:
    """Retrieve the most recent notification log entries."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM automation_notification_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Pending Approval (User Approval strategy) ─────────────────────────────────
# Supports multiple concurrent requests per gateway via request_id reconciliation.
# Older records are automatically cleaned up when they expire.

async def get_pending_approval(gateway_serial: str = None, request_id: str = None, rule_id: str = None) -> "dict | None":
    """Return the pending approval record by request_id or the latest for a gateway."""
    from datetime import datetime, timezone
    sql = "SELECT * FROM pending_approvals"
    params = []
    if request_id:
        sql += " WHERE request_id = ?"
        params.append(request_id)
    elif gateway_serial and rule_id:
        sql += " WHERE gateway_serial = ? AND rule_id = ? ORDER BY created_at DESC LIMIT 1"
        params.append(gateway_serial)
        params.append(rule_id)
    elif gateway_serial:
        sql += " WHERE gateway_serial = ? ORDER BY created_at DESC LIMIT 1"
        params.append(gateway_serial)
    else:
        return None

    async with get_db() as conn:
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    rec = dict(row)
    
    # Check expiry (stored as epoch timestamp)
    expires_at = rec.get("expires_at")
    if expires_at and int(datetime.now(timezone.utc).timestamp()) > expires_at:
        await clear_pending_approval(request_id=rec["request_id"])
        return None
    return rec


async def set_pending_approval(
    gateway_serial: str,
    *,
    request_id: str,
    rule_id: str = None,
    rule_name: str,
    action: str,
    dispatch_summary: str,
    ttl_secs: int = 1800,
    no_reply_action: str = "skip",
    action_context: dict = None
) -> None:
    """Store a pending approval record in the database."""
    from datetime import datetime, timezone
    expires_at = int(datetime.now(timezone.utc).timestamp()) + ttl_secs
    ctx_json = json.dumps(action_context or {})
    
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO pending_approvals 
               (request_id, gateway_serial, rule_id, rule_name, action, dispatch_summary, expires_at, no_reply_action, action_context)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(request_id) DO UPDATE SET
                 gateway_serial=excluded.gateway_serial,
                 rule_id=excluded.rule_id,
                 rule_name=excluded.rule_name,
                 action=excluded.action,
                 dispatch_summary=excluded.dispatch_summary,
                 expires_at=excluded.expires_at,
                 no_reply_action=excluded.no_reply_action,
                 action_context=excluded.action_context
            """,
            (request_id, gateway_serial, rule_id, rule_name, action, dispatch_summary, expires_at, no_reply_action, ctx_json),
        )
        await conn.commit()


async def update_pending_approval_response(request_id: str, response: str) -> bool:
    """Record a user response for a pending actionable notification."""
    from datetime import datetime, timezone
    responded_at = datetime.now(timezone.utc).isoformat()
    
    async with get_db() as conn:
        cur = await conn.execute(
            "UPDATE pending_approvals SET response = ?, responded_at = ? WHERE request_id = ?",
            (response, responded_at, request_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def clear_pending_approval(gateway_serial: str = None, request_id: str = None) -> None:
    """Remove pending approval record(s) by gateway or request ID."""
    sql = "DELETE FROM pending_approvals"
    params = []
    if request_id:
        sql += " WHERE request_id = ?"
        params.append(request_id)
    elif gateway_serial:
        sql += " WHERE gateway_serial = ?"
        params.append(gateway_serial)
    else:
        return

    async with get_db() as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def check_notification_rate_limit(gateway_serial: str, max_per_hour: int = 3) -> bool:
    """
    Return True if a notification CAN be sent (under rate limit).
    Tracks notification timestamps in app_config key "notif_rate:{gateway_serial}".
    Enforces max_per_hour sliding window.
    """
    import json as _json
    from datetime import datetime, timezone, timedelta
    key = f"notif_rate:{gateway_serial}"
    raw = await get_config_value(key)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    try:
        timestamps = [datetime.fromisoformat(ts) for ts in _json.loads(raw or "[]")]
    except Exception:
        timestamps = []
    # Keep only timestamps within last hour
    recent = [ts for ts in timestamps if ts > cutoff]
    if len(recent) >= max_per_hour:
        return False  # rate limited
    # Record this send
    recent.append(now)
    await set_config_value(key, _json.dumps([ts.isoformat() for ts in recent]))
    return True


async def seed_default_rulebook() -> None:
    """
    Idempotent: insert the shipped Amber AU default rulebook and its 5 system rules.
    All rules use APPLY_PRESET referencing the reserved "Amber *" preset namespace.
    Safe to call on every boot — ON CONFLICT(rule_id) DO UPDATE preserves user edits.
    The rulebook itself is only inserted if it doesn't already exist.
    """
    RULEBOOK_ID = "amber-au-default"
    async with get_db() as conn:
        await conn.execute(
            """INSERT OR IGNORE INTO automation_rulebooks
               (rulebook_id, name, provider, description, is_active, is_system)
               VALUES (?, ?, ?, ?, 1, 1)""",
            (
                RULEBOOK_ID,
                "Amber Electric — Default AU",
                "amber",
                "Shipped default rulebook for Amber Electric AU. "
                "Rules use direct hardware actions (GRID_CHARGE, GRID_EXPORT, STANDBY). "
                "When no rule matches, HOLD is returned (no-op — native gateway mode continues).",
            ),
        )
        await conn.commit()

    # ── Default rules — priority ascending ──────────────────────────────────
    # Actions reference reserved preset names (must exist in SchedulePresets).
    # soc_pct conditions are intentionally omitted from DSL — the engine reads
    # live SoC from gateway telemetry at evaluation time.
    DEFAULT_RULES = [
        (
            "amber-au-spike-protect", RULEBOOK_ID,
            "Price Spike — Confirmed Spike",
            "Price spike confirmed: protect battery SoC and avoid expensive grid import.",
            10, True,
            json.dumps({"operator": "AND", "conditions": [
                {"field": "spike_status", "op": "EQ", "value": "spike"}
            ]}),
            "STANDBY",
            json.dumps({}),
            60, "amber", None,
        ),
        (
            "amber-au-spike-potential", RULEBOOK_ID,
            "Price Spike — Spike Developing",
            "Price spike developing: preserve SoC for imminent spike coverage.",
            20, True,
            json.dumps({"operator": "AND", "conditions": [
                {"field": "spike_status", "op": "EQ", "value": "potential"}
            ]}),
            "STANDBY",
            json.dumps({}),
            60, "amber", None,
        ),
        (
            "amber-au-force-charge", RULEBOOK_ID,
            "Force Charge — Negative / Extremely Low Price",
            "Grid price is negative or extremely low. Charge the battery aggressively from the grid.",
            30, True,
            json.dumps({"operator": "AND", "conditions": [
                {"field": "descriptor", "op": "IN",
                 "value": ["negative", "extremelyLow"]},
                {"field": "spike_status", "op": "EQ", "value": "none"},
            ]}),
            "GRID_CHARGE",
            json.dumps({}),
            30, "amber", None,
        ),
        (
            "amber-au-solar-sponge", RULEBOOK_ID,
            "Export Bonus — Solar Sponge Window",
            "SA/VIC Solar Sponge (export bonus) period active. Maximise battery export into the grid.",
            40, True,
            json.dumps({"operator": "AND", "conditions": [
                {"field": "tariff_period", "op": "EQ", "value": "solarSponge"}
            ]}),
            "GRID_EXPORT",
            json.dumps({}),
            40, "amber", None,
        ),
        (
            "amber-au-peak-discharge", RULEBOOK_ID,
            "Peak Discharge — Peak / Spike Tariff",
            "Peak tariff active. Export battery energy into the grid and avoid expensive import.",
            60, True,
            json.dumps({"operator": "OR", "conditions": [
                {"field": "tariff_type", "op": "EQ", "value": "PEAK"},
                {"field": "tariff_type", "op": "EQ", "value": "SPIKE"},
            ]}),
            "GRID_EXPORT",
            json.dumps({}),
            30, None, None,
        ),
        (
            "amber-au-resume-default", RULEBOOK_ID,
            "No Override (Catch-All)",
            "No active price signal. No SD override issued — gateway continues in native mode.",
            999, True,
            json.dumps({}),   # empty = always true / catch-all
            "NONE",           # no-op: native gateway mode stays in place
            json.dumps({}),
            0, None, None,
        ),
    ]

    for (
        rule_id, rulebook_id, name, description, priority, enabled,
        condition_json, action, action_params, cooldown_min,
        provider_scope, gateway_scope
    ) in DEFAULT_RULES:
        await upsert_rule(
            rule_id=rule_id, rulebook_id=rulebook_id,
            name=name, description=description,
            priority=priority, enabled=enabled,
            condition_json=condition_json,
            action=action, action_params=action_params,
            cooldown_min=cooldown_min,
            provider_scope=provider_scope,
            gateway_scope=gateway_scope,
        )


def get_smart_dispatch_reserved_presets() -> list[dict]:
    """
    Return metadata for all reserved Smart Dispatch preset names.
    These are the canonical names the automation rules reference via APPLY_PRESET.
    The actual TOU schedule content is stored in SchedulePresets (schedule_presets.json).
    """
    return [
        {
            "name": "Smart Default",
            "reserved": True,
            "description": "Mandatory baseline. Applied by catch-all rule when no price "
                           "signal is active. Must be configured before auto-execution "
                           "is enabled.",
            "role": "baseline",
        },
        {
            "name": "Smart Force Charge",
            "reserved": True,
            "description": "Aggressive grid-charge schedule for negative or extremely low "
                           "price windows. Typically: charge hard 22:00–07:00, self-use "
                           "rest of day.",
            "role": "force_charge",
        },
        {
            "name": "Smart Peak Discharge",
            "reserved": True,
            "description": "Export-maximised schedule for PEAK and SPIKE tariff periods. "
                           "Battery discharges to grid during high-price windows.",
            "role": "peak_discharge",
        },
        {
            "name": "Smart Solar Sponge",
            "reserved": True,
            "description": "Export bonus schedule for SA/VIC Solar Sponge windows. "
                           "Battery exports aggressively when the network requests "
                           "generation support from distributed storage.",
            "role": "solar_sponge",
        },
        {
            "name": "Smart Spike Hold",
            "reserved": True,
            "description": "Hold mode for confirmed or developing price spikes. "
                           "Minimises import and preserves SoC for post-spike recovery.",
            "role": "spike_hold",
        },
    ]


async def seed_smart_dispatch_tou_templates(presets_mgr) -> int:
    """
    Seed skeleton TOU schedule presets for each reserved preset name.
    Only inserts if the preset does not already exist (preserves user edits).

    Skeletons are 24-hour-complete FranklinWH TOU schedules in the flat
    detailVoList format (single-season, Everyday day-type = dayType 3).
    Users should review and push these via the Schedule tab before enabling
    auto-execution.

    Returns the number of new presets created.
    """
    # dispatchId meanings (FranklinWH):
    #   1 = Self-Use            (solar → load → battery → grid)
    #   2 = Load First          (grid → load → battery)
    #   4 = Grid Charge (Force) (grid → battery at max rate)
    #   5 = Off / Backup        (battery holds, no export)
    #   9 = Grid Export (Force) (battery → grid at max rate)

    TEMPLATES = {
        "Smart Default": {
            "description": "Balanced daily baseline — self-use during day, "
                           "backup hold overnight. Edit to match your household.",
            "schedule": [
                {"startHourTime": "00:00", "endHourTime": "07:00",
                 "name": "Overnight Hold", "waveType": 0, "dispatchId": 5,
                 "maxChargeSoc": 100, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 0},
                {"startHourTime": "07:00", "endHourTime": "22:00",
                 "name": "Day Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "22:00", "endHourTime": "24:00",
                 "name": "Evening Hold", "waveType": 0, "dispatchId": 5,
                 "maxChargeSoc": 100, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 0},
            ],
        },
        "Smart Force Charge": {
            "description": "Aggressive grid charge during negative/extremely-low price "
                           "windows. Charges battery from grid at maximum rate overnight.",
            "schedule": [
                {"startHourTime": "00:00", "endHourTime": "07:00",
                 "name": "Force Charge (Cheap Window)", "waveType": 0, "dispatchId": 4,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 5000, "gridDischargeMax": 0},
                {"startHourTime": "07:00", "endHourTime": "16:00",
                 "name": "Day Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "16:00", "endHourTime": "21:00",
                 "name": "Peak Backup Hold", "waveType": 0, "dispatchId": 5,
                 "maxChargeSoc": 100, "minDischargeSoc": 30,
                 "gridChargeMax": 0, "gridDischargeMax": 0},
                {"startHourTime": "21:00", "endHourTime": "24:00",
                 "name": "Evening Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
            ],
        },
        "Smart Peak Discharge": {
            "description": "Maximise export and avoid import during PEAK/SPIKE tariff. "
                           "Discharge battery to grid during high-price windows.",
            "schedule": [
                {"startHourTime": "00:00", "endHourTime": "07:00",
                 "name": "Overnight Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 90, "minDischargeSoc": 30,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "07:00", "endHourTime": "16:00",
                 "name": "Day Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "16:00", "endHourTime": "21:00",
                 "name": "Peak — Force Export", "waveType": 0, "dispatchId": 9,
                 "maxChargeSoc": 100, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "21:00", "endHourTime": "24:00",
                 "name": "Evening Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
            ],
        },
        "Smart Solar Sponge": {
            "description": "Maximise export during SA/VIC Solar Sponge (export bonus) "
                           "window. Discharges battery aggressively to grid.",
            "schedule": [
                {"startHourTime": "00:00", "endHourTime": "10:00",
                 "name": "Overnight/Morning Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "10:00", "endHourTime": "15:00",
                 "name": "Solar Sponge — Force Export", "waveType": 0, "dispatchId": 9,
                 "maxChargeSoc": 100, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
                {"startHourTime": "15:00", "endHourTime": "24:00",
                 "name": "Afternoon/Evening Self-Use", "waveType": 0, "dispatchId": 1,
                 "maxChargeSoc": 95, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 5000},
            ],
        },
        "Smart Spike Hold": {
            "description": "Hold mode during confirmed or developing price spikes. "
                           "No import, no export — preserve SoC for post-spike recovery.",
            "schedule": [
                {"startHourTime": "00:00", "endHourTime": "24:00",
                 "name": "Spike Hold — Backup Mode", "waveType": 0, "dispatchId": 5,
                 "maxChargeSoc": 100, "minDischargeSoc": 20,
                 "gridChargeMax": 0, "gridDischargeMax": 0},
            ],
        },
    }

    created = 0
    for preset_name, tmpl in TEMPLATES.items():
        existing = presets_mgr.load_preset(preset_name)
        if existing.get("success"):
            logger.debug(f"seed_smart_dispatch_tou_templates: preset '{preset_name}' already exists — skipping")
            continue
        result = presets_mgr.save_preset(
            name=preset_name,
            description=tmpl["description"],
            schedule=tmpl["schedule"],
            unverified=True,   # mark as unverified until user reviews and pushes
        )
        if result.get("success"):
            logger.info(f"seed_smart_dispatch_tou_templates: seeded preset '{preset_name}'")
            created += 1
        else:
            logger.warning(f"seed_smart_dispatch_tou_templates: failed to seed '{preset_name}': {result}")
    return created


async def migrate_amber_presets_to_generic(presets_mgr) -> None:
    """
    Database migration to rename existing Amber presets in schedule_presets.json and
    associated rules in the SQLite DB automation_rules table from 'Amber *' to 'Smart *'.
    """
    preset_mapping = {
        "Amber Default": "Smart Default",
        "Amber Force Charge": "Smart Force Charge",
        "Amber Peak Discharge": "Smart Peak Discharge",
        "Amber Solar Sponge": "Smart Solar Sponge",
        "Amber Spike Hold": "Smart Spike Hold",
    }

    if presets_mgr and hasattr(presets_mgr, "_presets"):
        modified = False
        for preset in presets_mgr._presets:
            old_name = preset.get("name")
            if old_name in preset_mapping:
                new_name = preset_mapping[old_name]
                if not any(p.get("name") == new_name for p in presets_mgr._presets):
                    preset["name"] = new_name
                    modified = True
                    logger.info(f"Migration: Renamed schedule preset '{old_name}' to '{new_name}' in memory.")
        if modified:
            presets_mgr._save()
            logger.info("Migration: Saved updated schedule presets to disk.")

    async with get_db() as conn:
        async with conn.execute("SELECT id, rule_id, name, action_params FROM automation_rules") as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            rule_db_id = row[0]
            rule_id = row[1]
            rule_name = row[2]
            action_params_str = row[3] or "{}"

            try:
                params = json.loads(action_params_str)
            except Exception:
                continue

            old_preset = params.get("preset_name")
            if old_preset in preset_mapping:
                params["preset_name"] = preset_mapping[old_preset]
                new_params_str = json.dumps(params)

                new_name = rule_name
                for old_val, new_val in preset_mapping.items():
                    new_name = new_name.replace(old_val, new_val)
                new_name = new_name.replace("Amber", "Smart")

                await conn.execute(
                    "UPDATE automation_rules SET action_params = ?, name = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_params_str, new_name, rule_db_id)
                )
                logger.info(f"Migration: Updated rule '{rule_name}' (id={rule_id}) to use preset '{params['preset_name']}'")

        await conn.commit()


async def seed_automation_builder_defaults() -> int:
    """
    Deprecated: Smart Dispatch rules are now evaluated dynamically from automation_rules table
    or managed via scheduler API.
    """
    return 0

# ── Smart Dispatch Engine Configuration ──────────────────────────────────────────

async def get_smart_dispatch_config(gateway_id: str) -> dict:
    """Get smart dispatch configuration for a gateway. Overrides global defaults with specific settings."""
    gateway_id = _normalise_gateway_id(gateway_id)
    async with get_db() as conn:
        global_row = None
        async with conn.execute("SELECT * FROM smart_dispatch_config WHERE gateway_id = 'global'") as cur:
            global_row = await cur.fetchone()

        specific_row = None
        if gateway_id != 'global':
            async with conn.execute("SELECT * FROM smart_dispatch_config WHERE gateway_id = ?", (gateway_id,)) as cur:
                specific_row = await cur.fetchone()

        base_config = {
            "gateway_id": gateway_id,
            "strategy_mode": SAFE_DEFAULT_STRATEGY_MODE,  # signal only, no hardware commands
            "min_soc": 20.0,
            "max_soc": 90.0,
            "max_charge_price": 0.0,
            "min_export_price": 0.0,
            "export_bonus_threshold": 5.0,
            "charge_power_mode": "default",
            "charge_power_value": 0.0,
            "discharge_power_mode": "default",
            "discharge_power_value": 0.0,
            "daily_earnings_target": 0.0,
            "monthly_earnings_target": 0.0,
            "solar_curtail_entity": None,
            "enphase_enabled": 0,
            "enphase_host": None,
            "enphase_user": "installer",
            "enphase_password": None,
            "enphase_slew_rate": 500,
            "enphase_export_limit_w": 0,
            "allow_auto_offgrid": 0,
            "notification_mode": "ask",
            "notify_on_demand_charge": 1,
            "notify_on_negative_export": 1,
            "notify_on_spike": 1,
            "notify_on_export_bonus": 0,
            "notify_on_earnings": 0,
            "notify_on_force_charge": 1,
            "notify_on_force_export": 0,
            "info_notify_targets": "",
            # Peak window SOC guard rails
            "min_peak_window_soc": 60.0,
            "max_peak_window_soc": 90.0,
            "shadow_mode": 0,
            "weather_extreme_impact": 0,
            "site_has_high_loads": 0,
            "multi_utility_service": 0,
            "utility_export_limit_w": 0,
            "has_apbox_excess_solar": 0,
            "apower_s_mppt": 0,
            "strategy_priorities_json": '["self_consumption", "peak_shaving", "export_exception", "battery_topup"]',
            "last_full_generation_time": None,
            "default_operating_mode": "gateway_default",
            "baseline_tou_snapshot": None,
            "active_override_uuid": None,
            "active_override_expires_at": None,
            "rampTime": 99,
            "maxChargeSoc": 100,
            "minDischargeSoc": 0,
            "chargePower": 5000,
            "dischargePower": 5000,
        }

        if global_row:
            base_config.update(dict(global_row))
        if specific_row:
            base_config.update(dict(specific_row))

        # 'disabled' at the global level is no longer a hard kill-switch for SD.
        # This resolves the clash between Global Defaults and the Automation Engine's
        # true global kill-switch (engine_mode: paused).
        global_dict = dict(global_row) if global_row else {}

        # Inject weather_load_influence dynamically from app_config (default True)
        val = await get_config_value(f"weather_load_influence_{gateway_id}", "1")
        base_config["weather_load_influence"] = bool(int(val or "1"))

        base_config["gateway_id"] = gateway_id
        return base_config

# gateway_id values that must never become a config row of their own. The
# /config endpoints bind gateway_id from a query parameter typed `str`, so a UI
# call carrying a JavaScript null arrives as the literal string "null" and the
# upsert below cheerfully creates a row for it.
#
# Two such rows ("NULL" and "null") accumulated on a live install and broke
# migration v30, which uppercases every gateway_id except 'global': "null"
# collided with the existing "NULL", raised UNIQUE constraint failed, and
# aborted the entire casing normalisation on every single boot for months.
_GATEWAY_ID_SENTINELS = frozenset({"", "null", "none", "undefined", "nan"})


def _normalise_gateway_id(gateway_id) -> str:
    """Collapse null-ish gateway ids onto 'global' before they reach the DB."""
    if gateway_id is None:
        return "global"
    gid = str(gateway_id).strip()
    if gid.lower() in _GATEWAY_ID_SENTINELS:
        logger.warning(
            f"smart_dispatch_config: gateway_id {gateway_id!r} is a null-ish "
            f"sentinel — treating as 'global'. A caller is passing an unset id."
        )
        return "global"
    return gid


async def upsert_smart_dispatch_config(gateway_id: str, **kwargs) -> None:
    """Update smart dispatch config fields."""
    gateway_id = _normalise_gateway_id(gateway_id)
    if "weather_load_influence" in kwargs:
        val = "1" if kwargs["weather_load_influence"] else "0"
        await set_config_value(f"weather_load_influence_{gateway_id}", val)

    allowed = {
        "strategy_mode",
        "min_soc", "max_soc", "export_bonus_threshold",
        "charge_power_mode", "charge_power_value",
        "discharge_power_mode", "discharge_power_value",
        "daily_earnings_target", "monthly_earnings_target",
        "max_charge_price", "min_export_price",
        "solar_curtail_entity", "notification_mode", "allow_auto_offgrid",
        "enphase_enabled", "enphase_host", "enphase_user", "enphase_password",
        "enphase_slew_rate", "enphase_export_limit_w",
        "notify_on_demand_charge", "notify_on_negative_export",
        "notify_on_spike", "notify_on_export_bonus",
        "notify_on_earnings", "notify_on_force_charge", "notify_on_force_export",
        "info_notify_targets",
        "min_peak_window_soc", "max_peak_window_soc",
        "shadow_mode",
        "weather_extreme_impact", "site_has_high_loads", "multi_utility_service",
        "utility_export_limit_w", "has_apbox_excess_solar", "apower_s_mppt",
        "strategy_priorities_json", "last_full_generation_time", "default_operating_mode",
        "baseline_tou_snapshot", "active_override_uuid", "active_override_expires_at",
        "rampTime", "maxChargeSoc", "minDischargeSoc", "chargePower", "dischargePower",
    }
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return

    # Filter out empty strings for numeric fields
    for field in ["min_soc", "max_soc", "max_charge_price", "min_export_price", "export_bonus_threshold",
                  "charge_power_value", "discharge_power_value",
                  "daily_earnings_target", "monthly_earnings_target",
                  "weather_extreme_impact", "site_has_high_loads", "multi_utility_service",
                  "utility_export_limit_w", "has_apbox_excess_solar", "apower_s_mppt",
                  "rampTime", "maxChargeSoc", "minDischargeSoc", "chargePower", "dischargePower"]:
        if field in data and data[field] == "":
            data[field] = 0

    # Seed the safe mode when this call creates the row, so the outcome does not
    # depend on the column default — databases migrated before the default was
    # corrected still carry 'active'. Deliberately absent from the UPDATE clause:
    # including it would reset a configured mode to "info" every time an
    # unrelated setting was saved.
    insert_data = dict(data)
    insert_data.setdefault("strategy_mode", SAFE_DEFAULT_STRATEGY_MODE)

    cols = ", ".join(insert_data.keys())
    placeholders = ", ".join("?" for _ in insert_data)
    updates = ", ".join(f"{k}=excluded.{k}" for k in data)
    async with get_db() as conn:
        await conn.execute(
            f"""INSERT INTO smart_dispatch_config (gateway_id, {cols}, updated_at)
                VALUES (?, {placeholders}, datetime('now'))
                ON CONFLICT(gateway_id) DO UPDATE SET
                  {updates},
                  updated_at=excluded.updated_at
            """,
            (gateway_id, *insert_data.values()),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Solar Forecast Config CRUD
# ---------------------------------------------------------------------------

_SOLAR_DEFAULTS = {
    "enabled":                  False,
    "source":                   "auto",
    "ha_solar_actual_entity":   None,
    "ha_solar_forecast_entity": None,
    "ha_solar_curtail_entity":  None,
    "lat":                      None,
    "lng":                      None,
    "azimuth":                  180.0,
    "tilt":                     22.5,
    "kwp":                      5.0,
    "forecast_solar_api_key":   None,
    "forecast_solar_rate_limit_mins": 30,
    "solcast_api_key":          None,
    "solcast_site_id":          None,
    "home_load_assumption_kw":  0.5,

    # Enphase Direct Envoy / Control Mode fields
    "enphase_enabled":            0,
    "enphase_host":               None,
    "enphase_user":               "installer",
    "enphase_password":           None,
    "enphase_slew_rate":          500,
    "enphase_export_limit_w":     0,
    "enphase_mode":               "none",
    "enphase_token":              None,
    "enphase_serial":             None,
    "enphase_enlighten_user":     None,
    "enphase_enlighten_password": None,
    "enphase_token_expiry":       None,
}


async def get_solar_forecast_config() -> dict:
    """Return solar forecast config (singleton row), with defaults if not yet saved."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM solar_forecast_config WHERE id = 1") as cur:
            row = await cur.fetchone()
    if row:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 0))
        return d
    return {"id": 1, **_SOLAR_DEFAULTS}


async def upsert_solar_forecast_config(**kwargs) -> None:
    """Persist solar forecast config (singleton). Only specified kwargs are updated."""
    allowed = set(_SOLAR_DEFAULTS.keys())
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return
    # Boolean coercion
    if "enabled" in data:
        data["enabled"] = 1 if data["enabled"] else 0
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    updates = ", ".join(f"{k}=excluded.{k}" for k in data)
    async with get_db() as conn:
        await conn.execute(
            f"""INSERT INTO solar_forecast_config (id, {cols}, updated_at)
                VALUES (1, {placeholders}, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                  {updates},
                  updated_at=excluded.updated_at
            """,
            (*data.values(),),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Amber Evaluation Log
# ---------------------------------------------------------------------------

async def log_pricing_eval(
    gateway_id: str,
    trigger_category: str,
    action: str,
    *,
    preset_name: Optional[str] = None,
    rule_name: Optional[str] = None,
    reason: Optional[str] = None,
    dispatch_summary: Optional[str] = None,
    requires_approval: bool = False,
    execution_status: str = "signal",
    import_c_kwh: Optional[float] = None,
    export_c_kwh: Optional[float] = None,
    soc_pct: Optional[float] = None,
    spike_status: Optional[str] = None,
    demand_window: bool = False,
    shadowed_rules_json: str = '[]',
    shadow_reason: Optional[str] = None,
) -> None:
    """Append a decision row; prune to 200 rows per gateway.

    Batch P (2026-07-30): shadow_reason optional kwarg. Populated when
    SD defers to an external controller (VPP / Modbus / Manual). Values
    like 'vpp_active' let consumers filter for "SD would have wanted X
    but didn't act because Y owned the gateway".
    """
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO pricing_eval_log
               (gateway_id, trigger_category, action, preset_name, rule_name,
                reason, dispatch_summary, requires_approval, execution_status,
                import_c_kwh, export_c_kwh, soc_pct, spike_status, demand_window,
                shadowed_rules_json, shadow_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gateway_id, trigger_category, action, preset_name, rule_name,
                reason, dispatch_summary, 1 if requires_approval else 0,
                execution_status, import_c_kwh, export_c_kwh, soc_pct,
                spike_status, 1 if demand_window else 0, shadowed_rules_json,
                shadow_reason,
            ),
        )
        # Rolling prune: keep newest 200 rows per gateway
        await conn.execute(
            """DELETE FROM pricing_eval_log
               WHERE gateway_id = ?
                 AND id NOT IN (
                     SELECT id FROM pricing_eval_log
                     WHERE gateway_id = ?
                     ORDER BY id DESC LIMIT 200
                 )
            """,
            (gateway_id, gateway_id),
        )
        await conn.commit()


async def get_pricing_eval_log(gateway_id: str, limit: int = 50) -> list[dict]:
    """Return recent evaluation decisions, newest first."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT * FROM pricing_eval_log
               WHERE gateway_id = ?
               ORDER BY id DESC LIMIT ?
            """,
            (gateway_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_latest_pricing_eval(gateway_id: str) -> Optional[dict]:
    """Return the single most recent eval decision for a gateway."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM pricing_eval_log WHERE gateway_id = ? ORDER BY id DESC LIMIT 1",
            (gateway_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Amber Usage Cache (hourly earnings tracking)
# ---------------------------------------------------------------------------

async def cache_amber_usage(gateway_id: str, records: list[dict], channel: str) -> int:
    """
    Upsert Amber usage records into amber_usage_cache.
    records: list of dicts with start_time, end_time, kwh, cost, tariff_type, quality.
    Returns count of rows inserted/updated.
    """
    if not records:
        return 0
    count = 0
    async with get_db() as conn:
        for rec in records:
            await conn.execute(
                """INSERT INTO amber_usage_cache
                   (gateway_id, channel, start_time, end_time, kwh, cost, tariff_type, quality)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(gateway_id, channel, start_time) DO UPDATE SET
                     end_time=excluded.end_time,
                     kwh=excluded.kwh,
                     cost=excluded.cost,
                     tariff_type=excluded.tariff_type,
                     quality=excluded.quality,
                     fetched_at=datetime('now')
                """,
                (
                    gateway_id, channel,
                    rec.get("start_time"), rec.get("end_time"),
                    rec.get("kwh", 0.0), rec.get("cost", 0.0),
                    rec.get("tariff_type"), rec.get("quality"),
                ),
            )
            count += 1
        # Purge rows older than 90 days
        await conn.execute(
            "DELETE FROM amber_usage_cache WHERE gateway_id = ? AND start_time < datetime('now', '-90 days')",
            (gateway_id,),
        )
        await conn.commit()
    return count


async def get_amber_earnings(
    gateway_id: str,
    since_iso: str,
    channel: str = "feed_in",
) -> float:
    """
    Feed-in cost values are negative in Amber (they are credits), so we negate
    the sum to return a positive earnings figure.
    Returns 0.0 if no data.
    """
    async with get_db() as conn:
        async with conn.execute(
            """SELECT COALESCE(SUM(cost), 0.0) as total
               FROM amber_usage_cache
               WHERE gateway_id = ?
                 AND channel = ?
                 AND start_time >= ?
            """,
            (gateway_id, channel, since_iso),
        ) as cur:
            row = await cur.fetchone()
    raw = float(row["total"]) if row else 0.0
    # Feed-in cost is negative (Amber convention: you earn, not spend)
    return abs(raw)


# ---------------------------------------------------------------------------
# Utility Services CRUD (BL-010)
# ---------------------------------------------------------------------------

def _row_to_utility_service_dict(row, description) -> dict:
    """Convert an aiosqlite row to a dict, resolving the column-name collision
    between `u.*` (which includes legacy `pricing_provider`, `pricing_credentials`,
    `pricing_settings` columns on `utility_services`) and the JOIN aliases of
    the same names. `dict(row)` keeps the FIRST occurrence per name — so the
    legacy (often empty) u.* values shadow the authoritative joined values
    from `pricing_models`. We walk the cursor description in order so the
    LAST-positioned value wins for these three columns, which corresponds to
    the joined `pricing_models` value we actually want.

    Without this resolver, switching a gateway's pricing_model_id had no
    observable effect on the live service — the emulation_mode flag and the
    new provider's credentials were silently dropped (DEF-PB-04, fixed
    2026-06-17 after LocalVolts emulation activation appeared to be ignored)."""
    out = {}
    for idx, desc in enumerate(description):
        col_name = desc[0]
        # Last write wins — overwrites the earlier u.* duplicate
        out[col_name] = row[idx]
    return out


async def get_all_utility_services() -> list[dict]:
    """Return all utility services with their linked pricing model configuration."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT u.*,
                      COALESCE(m.id, 'franklinwh_tou') as pricing_provider,
                      COALESCE(m.credentials, '{}') as pricing_credentials,
                      COALESCE(m.settings, '{}') as pricing_settings
               FROM utility_services u
               LEFT JOIN pricing_models m ON COALESCE(u.pricing_model_id, 'franklinwh_tou') = m.id
               ORDER BY u.created_at"""
        ) as cur:
            rows = await cur.fetchall()
            desc = cur.description
    return [_row_to_utility_service_dict(r, desc) for r in rows]

async def get_utility_service(service_id: str) -> Optional[dict]:
    """Return a specific utility service with its linked pricing model configuration."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT u.*,
                      COALESCE(m.id, 'franklinwh_tou') as pricing_provider,
                      COALESCE(m.credentials, '{}') as pricing_credentials,
                      COALESCE(m.settings, '{}') as pricing_settings
               FROM utility_services u
               LEFT JOIN pricing_models m ON COALESCE(u.pricing_model_id, 'franklinwh_tou') = m.id
               WHERE u.id = ?""",
            (service_id,)
        ) as cur:
            row = await cur.fetchone()
            desc = cur.description
    return _row_to_utility_service_dict(row, desc) if row else None

# The v58 tariff fields, written separately from upsert_utility_service.
#
# That statement hand-maintains every column four times over — column list,
# VALUES, ON CONFLICT and parameters — and preserves with
# COALESCE(excluded.x, x) against parameters that default to 0 rather than
# None. A falsy default therefore reads as "supplied", which is how a saved
# service once overwrote stored values with defaults. Adding twenty-three more
# fields to it would multiply that surface for no benefit.
#
# This writes only the keys actually present in the payload, so an absent field
# is untouched and a deliberate zero is stored.
TARIFF_FIELDS_V58 = (
    "plan_timezone", "plan_type", "export_limit_kw",
    "export_allowed", "battery_charge_permitted", "battery_discharge_permitted",
    "min_monthly_bill_c",
    "demand_interval_min", "demand_interval_count", "demand_charge_basis",
    "export_free_kwh_day",
)


async def update_utility_service_tariff(service_id: str, data: dict) -> list[str]:
    """Write whichever v58 tariff fields the payload carries. Returns the
    names written, for the audit trail."""
    present = [f for f in TARIFF_FIELDS_V58 if f in data]
    if not service_id or not present:
        return []

    assignments = ", ".join(f"{f} = :{f}" for f in present)
    params = {f: data[f] for f in present}
    params["sid"] = service_id
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE utility_services SET {assignments}, updated_at = datetime('now') "
            "WHERE id = :sid",
            params,
        )
        await conn.commit()
    return present


# ── standing charges (list) ──────────────────────────────────────────────────
# supply_charge_day, metering_fee and network_fixed_fee remain and are still
# summed by tariff_costing. These are additive, for the fees those three cannot
# name — membership, connection, and whatever a retailer invents next.

async def list_standing_charges(service_id: str) -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM utility_standing_charges WHERE service_id = ? ORDER BY id",
            (service_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def add_standing_charge(service_id: str, label: str, amount_c: float,
                              basis: str = "per_day",
                              months: str | None = None) -> int:
    async with get_db() as conn:
        cur = await conn.execute(
            "INSERT INTO utility_standing_charges (service_id, label, amount_c, basis, months) "
            "VALUES (?, ?, ?, ?, ?)",
            (service_id, label, float(amount_c or 0), basis or "per_day", months),
        )
        await conn.commit()
        return cur.lastrowid


async def delete_standing_charge(service_id: str, charge_id: int) -> int:
    """Scoped to the service, so an id from another plan cannot be removed."""
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM utility_standing_charges WHERE id = ? AND service_id = ?",
            (charge_id, service_id),
        )
        await conn.commit()
        return cur.rowcount


async def upsert_utility_service(service_data: dict) -> None:
    """Insert or update a utility service."""
    async with get_db() as conn:
        import uuid
        service_id = service_data.get("id")
        if not service_id:
            service_id = str(uuid.uuid4())
            service_data["id"] = service_id

        pricing_model_id = service_data.get("pricing_model_id") or service_data.get("pricing_provider") or "franklinwh_tou"

        await conn.execute(
            """INSERT INTO utility_services (
                   id, name, retailer_name, account_number, nmi, meter_serial, meter_type,
                   service_amps, pricing_provider, pricing_credentials, pricing_settings,
                   pricing_model_id,
                   bill_frequency, bill_start_day, bill_period_days, supply_charge_day,
                   metering_fee, network_fixed_fee, demand_charge_kw, demand_window_start,
                   demand_window_end, demand_window_days, fit_rate_c_kwh, fit_scheme_name, notes,
                   fwh_site_id, fwh_site_name, account_name, tariff_type, tariff_validated_at,
                   tariff_company_id, tariff_company_name, nem_type, vpp_enrolled, vpp_provider,
                   site_id, site_name,
                   updated_at
               ) VALUES (
                   :id, :name, :retailer_name, :account_number, :nmi, :meter_serial, :meter_type,
                   :service_amps, :pricing_provider, :pricing_credentials, :pricing_settings,
                   :pricing_model_id,
                   :bill_frequency, :bill_start_day, :bill_period_days, :supply_charge_day,
                   :metering_fee, :network_fixed_fee, :demand_charge_kw, :demand_window_start,
                   :demand_window_end, :demand_window_days, :fit_rate_c_kwh, :fit_scheme_name, :notes,
                   :fwh_site_id, :fwh_site_name, :account_name, :tariff_type, :tariff_validated_at,
                   :tariff_company_id, :tariff_company_name, :nem_type, :vpp_enrolled, :vpp_provider,
                   :site_id, :site_name,
                   datetime('now')
               )
               ON CONFLICT(id) DO UPDATE SET
                   name=COALESCE(excluded.name, name),
                   retailer_name=COALESCE(excluded.retailer_name, retailer_name),
                   account_number=COALESCE(excluded.account_number, account_number),
                   nmi=COALESCE(excluded.nmi, nmi),
                   meter_serial=COALESCE(excluded.meter_serial, meter_serial),
                   meter_type=COALESCE(excluded.meter_type, meter_type),
                   service_amps=COALESCE(excluded.service_amps, service_amps),
                   pricing_provider=COALESCE(excluded.pricing_provider, pricing_provider),
                   pricing_credentials=COALESCE(excluded.pricing_credentials, pricing_credentials),
                   pricing_settings=COALESCE(excluded.pricing_settings, pricing_settings),
                   pricing_model_id=COALESCE(excluded.pricing_model_id, pricing_model_id),
                   bill_frequency=COALESCE(excluded.bill_frequency, bill_frequency),
                   bill_start_day=COALESCE(excluded.bill_start_day, bill_start_day),
                   bill_period_days=COALESCE(excluded.bill_period_days, bill_period_days),
                   supply_charge_day=COALESCE(excluded.supply_charge_day, supply_charge_day),
                   metering_fee=COALESCE(excluded.metering_fee, metering_fee),
                   network_fixed_fee=COALESCE(excluded.network_fixed_fee, network_fixed_fee),
                   demand_charge_kw=COALESCE(excluded.demand_charge_kw, demand_charge_kw),
                   demand_window_start=COALESCE(excluded.demand_window_start, demand_window_start),
                   demand_window_end=COALESCE(excluded.demand_window_end, demand_window_end),
                   demand_window_days=COALESCE(excluded.demand_window_days, demand_window_days),
                   fit_rate_c_kwh=COALESCE(excluded.fit_rate_c_kwh, fit_rate_c_kwh),
                   fit_scheme_name=COALESCE(excluded.fit_scheme_name, fit_scheme_name),
                   notes=COALESCE(excluded.notes, notes),
                   fwh_site_id=COALESCE(excluded.fwh_site_id, fwh_site_id),
                   fwh_site_name=COALESCE(excluded.fwh_site_name, fwh_site_name),
                   account_name=COALESCE(excluded.account_name, account_name),
                   tariff_type=COALESCE(excluded.tariff_type, tariff_type),
                   tariff_validated_at=COALESCE(excluded.tariff_validated_at, tariff_validated_at),
                   tariff_company_id=COALESCE(excluded.tariff_company_id, tariff_company_id),
                   tariff_company_name=COALESCE(excluded.tariff_company_name, tariff_company_name),
                   nem_type=COALESCE(excluded.nem_type, nem_type),
                   vpp_enrolled=COALESCE(excluded.vpp_enrolled, vpp_enrolled),
                   vpp_provider=COALESCE(excluded.vpp_provider, vpp_provider),
                   site_id=COALESCE(excluded.site_id, site_id),
                   site_name=COALESCE(excluded.site_name, site_name),
                   updated_at=datetime('now')
            """,
            {
                "id": service_id,
                "name": service_data.get("name"),
                "retailer_name": service_data.get("retailer_name"),
                "account_number": service_data.get("account_number"),
                "nmi": service_data.get("nmi"),
                "meter_serial": service_data.get("meter_serial"),
                "meter_type": service_data.get("meter_type"),
                "service_amps": service_data.get("service_amps"),
                "pricing_provider": service_data.get("pricing_provider", "flat"),
                "pricing_credentials": service_data.get("pricing_credentials", "{}"),
                "pricing_settings": service_data.get("pricing_settings", "{}"),
                "pricing_model_id": pricing_model_id,
                "bill_frequency": service_data.get("bill_frequency", "quarterly"),
                "bill_start_day": service_data.get("bill_start_day", 1),
                "bill_period_days": service_data.get("bill_period_days"),
                "supply_charge_day": service_data.get("supply_charge_day", 0),
                "metering_fee": service_data.get("metering_fee", 0),
                "network_fixed_fee": service_data.get("network_fixed_fee", 0),
                "demand_charge_kw": service_data.get("demand_charge_kw", 0),
                "demand_window_start": service_data.get("demand_window_start"),
                "demand_window_end": service_data.get("demand_window_end"),
                "demand_window_days": service_data.get("demand_window_days", "weekdays"),
                "fit_rate_c_kwh": service_data.get("fit_rate_c_kwh"),
                "fit_scheme_name": service_data.get("fit_scheme_name"),
                "notes": service_data.get("notes"),
                # v18 fields
                "fwh_site_id":         service_data.get("fwh_site_id"),
                "fwh_site_name":       service_data.get("fwh_site_name"),
                "account_name":        service_data.get("account_name"),
                "tariff_type":         service_data.get("tariff_type"),
                "tariff_validated_at": service_data.get("tariff_validated_at"),
                "tariff_company_id":   service_data.get("tariff_company_id"),
                "tariff_company_name": service_data.get("tariff_company_name"),
                "nem_type":            service_data.get("nem_type"),
                "vpp_enrolled":        int(service_data.get("vpp_enrolled") or 0),
                "vpp_provider":        service_data.get("vpp_provider"),
                # DEF-PB-01: user-defined site grouping
                "site_id":             service_data.get("site_id") or None,
                "site_name":           service_data.get("site_name") or None,
            }
        )
        await conn.commit()

async def delete_utility_service(service_id: str) -> None:
    """Delete a utility service."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM utility_services WHERE id = ?", (service_id,))
        await conn.commit()

async def link_gateway_to_utility_service(
    short_id: str, utility_service_id: str, gateway_phase: str | None = None
) -> None:
    """Link a gateway to a utility service (composite PK — a gateway can have up to 2 links
    when CT Split—Grid is installed). gateway_phase is L1/L2/L3 for multi-gateway 3-phase."""
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO agate_utility_links
                   (gateway_short_id, utility_service_id, gateway_phase, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(gateway_short_id, utility_service_id) DO UPDATE SET
                   gateway_phase = excluded.gateway_phase,
                   updated_at    = datetime('now')
            """,
            (short_id, utility_service_id, gateway_phase)
        )
        await conn.commit()

async def unlink_gateway_from_utility_service(
    short_id: str, utility_service_id: str | None = None
) -> None:
    """Unlink a gateway from one specific utility service, or all services if service_id is None."""
    async with get_db() as conn:
        if utility_service_id:
            await conn.execute(
                "DELETE FROM agate_utility_links WHERE gateway_short_id = ? AND utility_service_id = ?",
                (short_id, utility_service_id)
            )
        else:
            await conn.execute(
                "DELETE FROM agate_utility_links WHERE gateway_short_id = ?", (short_id,)
            )
        await conn.commit()

async def get_gateway_utility_links() -> dict[str, list[str]]:
    """Return dict of {gateway_short_id: [utility_service_id, ...]}.
    A gateway with CT Split—Grid may appear with 2 service IDs.
    """
    async with get_db() as conn:
        async with conn.execute(
            "SELECT gateway_short_id, utility_service_id, gateway_phase FROM agate_utility_links"
        ) as cur:
            rows = await cur.fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r["gateway_short_id"], []).append(r["utility_service_id"])
    return result

async def get_gateway_utility_links_flat() -> dict[str, str]:
    """Backward-compat helper: returns {gateway_short_id: first_utility_service_id}.
    Used by code that assumes 1:1 (solar setup, capabilities endpoint).
    """
    links = await get_gateway_utility_links()
    return {gw: ids[0] for gw, ids in links.items() if ids}


async def get_utility_service_for_gateway(short_id: str) -> Optional[dict]:
    """Return the utility service dict for a specific gateway with its linked pricing model configuration."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT u.*, 
                      COALESCE(m.id, 'franklinwh_tou') as pricing_provider,
                      COALESCE(m.credentials, '{}') as pricing_credentials,
                      COALESCE(m.settings, '{}') as pricing_settings
               FROM utility_services u
               JOIN agate_utility_links l ON u.id = l.utility_service_id
               LEFT JOIN pricing_models m ON COALESCE(u.pricing_model_id, 'franklinwh_tou') = m.id
               WHERE l.gateway_short_id = ?
            """,
            (short_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None

async def get_gateways_for_utility_service(utility_service_id: str) -> list[str]:
    """Return a list of gateway short IDs mapped to a specific utility service."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT gateway_short_id FROM agate_utility_links WHERE utility_service_id = ?",
            (utility_service_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [r["gateway_short_id"] for r in rows]


# ── Utility Service Windows (Ph-2c) ──────────────────────────────────────────

async def get_utility_service_windows(service_id: str) -> list[dict]:
    """Return all time windows for a utility service, ordered by type then start time."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM utility_service_windows WHERE service_id = ? ORDER BY window_type, start_time",
            (service_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_utility_service_window(service_id: str, data: dict) -> int:
    """Insert a new time window for a utility service. Returns the new row ID."""
    async with get_db() as conn:
        cursor = await conn.execute(
            """INSERT INTO utility_service_windows
               (service_id, window_type, label, start_time, end_time, day_type,
                months, rate, rate_kind)
               VALUES (:service_id, :window_type, :label, :start_time, :end_time,
                       :day_type, :months, :rate, :rate_kind)""",
            {
                "service_id":  service_id,
                "window_type": data.get("window_type"),
                "label":       data.get("label"),
                "start_time":  data.get("start_time"),
                "end_time":    data.get("end_time"),
                "day_type":    data.get("day_type", "weekdays"),
                "months":      data.get("months"),
                "rate":        data.get("rate"),
                # Direction is a field, never a sign — see migration v60.
                "rate_kind":   (data.get("rate_kind") or "credit"),
            }
        )
        await conn.commit()
    return cursor.lastrowid


async def delete_utility_service_window(window_id: int, service_id: str) -> None:
    """Delete a specific window — scoped by service_id for safety."""
    async with get_db() as conn:
        await conn.execute(
            "DELETE FROM utility_service_windows WHERE id = ? AND service_id = ?",
            (window_id, service_id)
        )
        await conn.commit()


# ── Utility Service Audit Log (Ph-2d) ────────────────────────────────────────

async def write_utility_service_audit(
    service_id: str,
    event: str,
    *,
    actor: str = "ui",
    field: str = None,
    old_value: str = None,
    new_value: str = None,
    detail: str = None,
) -> None:
    """Write a single audit log entry for a utility service operation."""
    try:
        async with get_db() as conn:
            await conn.execute(
                """INSERT INTO utility_service_audit_log
                   (service_id, event, actor, field, old_value, new_value, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (service_id, event, actor, field, old_value, new_value, detail)
            )
            await conn.commit()
    except Exception as exc:
        logger.warning(f"Failed to write utility service audit log for {service_id}: {exc}")


async def get_utility_service_audit(service_id: str, limit: int = 20) -> list[dict]:
    """Return recent audit log entries for a utility service (newest first)."""
    async with get_db() as conn:
        async with conn.execute(
            """SELECT * FROM utility_service_audit_log
               WHERE service_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (service_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]

# ── Smart Dispatch Schedules ──────────────────────────────────────────

async def get_smart_dispatch_schedules(gateway_id: str) -> list[dict]:
    """Get all time-based schedules. If gateway_id is provided, includes global and specific rules."""
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM smart_dispatch_schedules WHERE gateway_id = 'global' OR gateway_id = ? ORDER BY start_time ASC",
            (gateway_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

async def add_smart_dispatch_schedule(gateway_id: str, period: str, start_time: str, end_time: str, min_soc: float, max_kw_percent: float = None, action: str = 'CHARGE') -> int:
    """Add a new time-based schedule."""
    async with get_db() as conn:
        cursor = await conn.execute(
            """INSERT INTO smart_dispatch_schedules 
               (gateway_id, period, start_time, end_time, min_soc, max_kw_percent, action) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (gateway_id, period, start_time, end_time, min_soc, max_kw_percent, action)
        )
        await conn.commit()
        return cursor.lastrowid

async def delete_smart_dispatch_schedule(schedule_id: int) -> bool:
    """Delete a time-based schedule."""
    async with get_db() as conn:
        cursor = await conn.execute(
            "DELETE FROM smart_dispatch_schedules WHERE id = ?", (schedule_id,)
        )
        await conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Gateway Solar Sources CRUD
# ---------------------------------------------------------------------------
async def auto_register_hardware_solar(gateway_id: str, hardware_type: str) -> None:
    """
    Automatically seed a gateway_solar_sources row for hardware detected via telemetry.
    hardware_type should be 'mppt' or 'apbox'.
    """
    source_type = 'mppt_1' if hardware_type == 'mppt' else 'apbox_pv_1'
    label = 'aPower MPPT' if hardware_type == 'mppt' else 'aPBox Remote PV'
    brand = 'FranklinWH'
    
    async with get_db() as conn:
        # Check if any source of this type already exists for this gateway
        async with conn.execute(
            "SELECT COUNT(*) FROM gateway_solar_sources WHERE gateway_id=? AND source_type=?",
            (gateway_id, source_type)
        ) as cur:
            count = (await cur.fetchone())[0]
            
        if count == 0:
            await conn.execute(
                """INSERT INTO gateway_solar_sources 
                   (gateway_id, source_type, port, kwp, label, source_name, brand, detected_by)
                   VALUES (?, ?, 1, 5.0, ?, ?, ?, 'discover')""",
                (gateway_id, source_type, label, label, brand)
            )
            await conn.commit()
            logger.info(f"Auto-registered {hardware_type} solar source for gateway {gateway_id}")


async def get_gateway_solar_sources(gateway_id: str) -> list[dict]:
    """Return all solar sources for a gateway, ordered by created_at."""
    async with get_db() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM gateway_solar_sources WHERE gateway_id = ? ORDER BY created_at",
            (gateway_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_gateway_solar_kwp_total(gateway_id: str) -> float:
    """Return total nameplate kWp across all enabled solar sources for a gateway.

    Used by the SD forecast API for Y-axis max scaling.
    Includes all source types where enabled=1, including split_ct when the user
    has opted in via the 'Include in forecast' toggle on the Solar Setup tab.
    Split-CT sources default to enabled=0 (informational only) unless explicitly
    toggled on by the user.
    Falls back to 0.0 if no sources configured.
    """
    async with get_db() as conn:
        async with conn.execute(
            "SELECT COALESCE(SUM(kwp), 0.0) FROM gateway_solar_sources "
            "WHERE gateway_id = ? AND enabled = 1",
            (gateway_id,),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0


async def add_gateway_solar_source(
    gateway_id: str,
    source_type: str,
    kwp: float,
    label: str | None = None,
    source_name: str | None = None,
    port: int | None = None,
    accessory_id: str | None = None,
    detected_by: str = "manual",
    utility_service_id: str | None = None,
    # Ph-SM: inverter metadata
    brand: str | None = None,
    inverter_type: str | None = None,
    phase_count: int = 1,
    ac_voltage: int | None = None,
    ac_hz: int | None = None,
    pv_control: int = 0,
    pv_control_type: str | None = None,
    pv_control_entity: str | None = None,
    # Ph-2/3/6: amperage + metering + capability toggles
    max_amps: int = 63,
    solar_metering_mode: str = "single_phase_internal",
    off_grid_capable: int = 0,
    pv_data_api: int = 0,
) -> dict:
    """Insert a new solar source. Returns the created row as a dict."""
    import secrets
    src_id = secrets.token_hex(8)
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO gateway_solar_sources
               (id, gateway_id, source_type, port, accessory_id, kwp, label,
                source_name, utility_service_id, detected_by, enabled,
                brand, inverter_type, phase_count, ac_voltage, ac_hz,
                pv_control, pv_control_type, pv_control_entity,
                max_amps, solar_metering_mode, off_grid_capable, pv_data_api)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (src_id, gateway_id, source_type, port, accessory_id, kwp, label,
             source_name, utility_service_id, detected_by,
             brand, inverter_type, phase_count, ac_voltage, ac_hz,
             pv_control, pv_control_type, pv_control_entity,
             max_amps, solar_metering_mode, off_grid_capable, pv_data_api),
        )
        await conn.commit()
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM gateway_solar_sources WHERE id = ?", (src_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {"id": src_id}


async def update_gateway_solar_source(src_id: str, gateway_id: str, **kwargs) -> bool:
    """Update a solar source.
    Allowed fields: kwp, label, source_name, port, accessory_id, enabled, utility_service_id,
    brand, inverter_type, phase_count, ac_voltage, ac_hz, pv_control, pv_control_type,
    pv_control_entity, max_amps, solar_metering_mode, off_grid_capable, pv_data_api.
    gateway_id is checked to prevent cross-gateway writes.
    Returns True if a row was updated.
    """
    allowed = {
        "kwp", "label", "source_name", "port", "accessory_id", "enabled", "utility_service_id",
        "brand", "inverter_type", "phase_count", "ac_voltage", "ac_hz",
        "pv_control", "pv_control_type", "pv_control_entity",
        # Ph-2/3/6
        "max_amps", "solar_metering_mode", "off_grid_capable", "pv_data_api",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k} = datetime('now')" if k == "updated_at" else f"{k} = ?"
        for k in fields
    )
    values = [v for k, v in fields.items() if k != "updated_at"]
    values += [src_id, gateway_id]
    async with get_db() as conn:
        cur = await conn.execute(
            f"UPDATE gateway_solar_sources SET {set_clause} "
            f"WHERE id = ? AND gateway_id = ?",
            values,
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_gateway_solar_source(src_id: str, gateway_id: str) -> bool:
    """Delete a solar source. Only deletes 'manual' sources (not Discover-detected)."""
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM gateway_solar_sources "
            "WHERE id = ? AND gateway_id = ? AND detected_by = 'manual'",
            (src_id, gateway_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def update_gateway_topology(
    short_id: str,
    service_amps: int | None = None,
    grid_type: str | None = None,
    gateway_phase: str | None = None,
    three_phase_group_id: str | None = None,
) -> bool:
    """Update installer-configurable grid topology fields on a gateway.
    Ph-4: service_amps (derating), grid_type, gateway_phase, three_phase_group_id.
    Returns True if a row was updated.
    """
    fields: dict = {}
    if service_amps is not None: fields["service_amps"] = service_amps
    if grid_type    is not None: fields["grid_type"]    = grid_type
    if gateway_phase is not None: fields["gateway_phase"] = gateway_phase
    if three_phase_group_id is not None: fields["three_phase_group_id"] = three_phase_group_id
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [short_id]
    async with get_db() as conn:
        cur = await conn.execute(
            f"UPDATE gateways SET {set_clause} WHERE short_id = ?", values
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_solar_sources_for_utility_service(service_id: str) -> list[dict]:
    """Return all solar sources linked to a specific utility service.

    Used by the Utility Service edit modal to show which solar panels feed
    into this electricity circuit.
    """
    async with get_db() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT gss.*, g.name AS gateway_name
               FROM gateway_solar_sources gss
               LEFT JOIN gateways g ON g.short_id = gss.gateway_id
               WHERE gss.utility_service_id = ?
               ORDER BY gss.created_at""",
            (service_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_all_solar_sources_with_gateway() -> list[dict]:
    """Return all solar sources across all gateways, each enriched with gateway name,
    linked utility service name, and a display label.

    Used by the Utility Service edit modal dropdown to assign solar sources
    to an electricity service.
    """
    async with get_db() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT
                  gss.*,
                  g.name  AS gateway_name,
                  us.name AS linked_service_name
               FROM gateway_solar_sources gss
               LEFT JOIN gateways g ON g.short_id = gss.gateway_id
               LEFT JOIN utility_services us ON us.id = gss.utility_service_id
               ORDER BY g.name, gss.created_at"""
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def link_solar_source_to_utility_service(
    src_id: str,
    utility_service_id: str | None,
) -> bool:
    """Set or clear the utility_service_id on a solar source.

    Pass utility_service_id=None to unlink.
    Returns True if a row was updated.
    """
    async with get_db() as conn:
        cur = await conn.execute(
            "UPDATE gateway_solar_sources SET utility_service_id=?, updated_at=datetime('now') WHERE id=?",
            (utility_service_id, src_id),
        )
        await conn.commit()
        return cur.rowcount > 0

# ── FORECAST LOADS ─────────────────────────────────────────────────────────────

async def get_all_forecast_loads() -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM forecast_loads ORDER BY name ASC"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

async def get_forecast_load(load_id: str) -> dict | None:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM forecast_loads WHERE id = ?", (load_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

def _slugify_forecast_load_name(name: str, fallback: str = "load") -> str:
    """Slugify a forecast load name for use in the AB home_load.<gw>.<slug>.* namespace."""
    import re as _re
    s = _re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or fallback


async def _resolve_slug_for_insert(conn, gateway_id: str, requested_slug: str, name: str, fallback_id: str) -> str:
    """Return a unique-within-gateway slug, deduping with _2/_3 suffixes."""
    base = (requested_slug or "").strip() or _slugify_forecast_load_name(name, fallback_id)
    async with conn.execute(
        "SELECT slug FROM forecast_loads WHERE COALESCE(gateway_id, 'global') = ? AND slug != ''",
        (gateway_id,),
    ) as cur:
        used = {r["slug"] for r in await cur.fetchall()}
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


async def upsert_forecast_load(load: dict) -> str:
    """
    Insert or update a forecast load. Provide 'id' to update, omit to insert.

    Slug semantics:
      - On INSERT: derive a slug from the name and dedupe within the load's
        gateway_id. Caller may pass an explicit `slug` to override; we still
        dedupe it.
      - On UPDATE: by default, the existing slug is preserved (renames do
        not change the slug — protects AB rules from silent breakage).
        If the caller explicitly passes a non-empty `slug` in the payload,
        we honour it (with dedupe).
    """
    async with get_db() as conn:
        if "id" in load and load["id"]:
            load_id = load["id"]

            # Decide what slug to write: explicit override wins, otherwise
            # keep the existing slug intact.
            explicit_slug = (load.get("slug") or "").strip() if "slug" in load else ""
            if explicit_slug:
                gw = load.get("gateway_id", "global") or "global"
                # Dedupe but exclude self
                async with conn.execute(
                    "SELECT slug FROM forecast_loads WHERE COALESCE(gateway_id, 'global') = ? AND slug != '' AND id != ?",
                    (gw, load_id),
                ) as cur:
                    used = {r["slug"] for r in await cur.fetchall()}
                base = explicit_slug
                candidate = base
                n = 2
                while candidate in used:
                    candidate = f"{base}_{n}"
                    n += 1
                new_slug = candidate
                await conn.execute(
                    """
                    UPDATE forecast_loads
                    SET name = ?, slug = ?, category = ?, ha_entity_id = ?,
                        ha_energy_entity_id = ?, ha_switch_entity_id = ?, ha_binary_entity_id = ?,
                        measurement_type = ?, peak_kw = ?, avg_kw = ?, schedule_json = ?, enabled = ?, gateway_id = ?,
                        dispatch_category = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        load["name"],
                        new_slug,
                        load["category"],
                        load.get("ha_entity_id"),
                        load.get("ha_energy_entity_id"),
                        load.get("ha_switch_entity_id"),
                        load.get("ha_binary_entity_id"),
                        load.get("measurement_type", "forecast"),
                        float(load.get("peak_kw", 0.0)),
                        float(load.get("avg_kw", 0.0)),
                        load.get("schedule_json", "[]"),
                        int(load.get("enabled", 1)),
                        load.get("gateway_id", "global"),
                        load.get("dispatch_category", "2-Essential Load"),
                        load_id,
                    )
                )
            else:
                # Keep existing slug — do not auto-rename on name change
                await conn.execute(
                    """
                    UPDATE forecast_loads
                    SET name = ?, category = ?, ha_entity_id = ?,
                        ha_energy_entity_id = ?, ha_switch_entity_id = ?, ha_binary_entity_id = ?,
                        measurement_type = ?, peak_kw = ?, avg_kw = ?, schedule_json = ?, enabled = ?, gateway_id = ?,
                        dispatch_category = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        load["name"],
                        load["category"],
                        load.get("ha_entity_id"),
                        load.get("ha_energy_entity_id"),
                        load.get("ha_switch_entity_id"),
                        load.get("ha_binary_entity_id"),
                        load.get("measurement_type", "forecast"),
                        float(load.get("peak_kw", 0.0)),
                        float(load.get("avg_kw", 0.0)),
                        load.get("schedule_json", "[]"),
                        int(load.get("enabled", 1)),
                        load.get("gateway_id", "global"),
                        load.get("dispatch_category", "2-Essential Load"),
                        load_id,
                    )
                )
        else:
            # INSERT — derive slug + dedupe within gateway
            gw = load.get("gateway_id", "global") or "global"
            slug = await _resolve_slug_for_insert(
                conn, gw, load.get("slug", ""), load.get("name", ""), ""
            )
            cur = await conn.execute(
                """
                INSERT INTO forecast_loads
                (name, slug, category, ha_entity_id, ha_energy_entity_id, ha_switch_entity_id, ha_binary_entity_id,
                 measurement_type, peak_kw, avg_kw, schedule_json, enabled, gateway_id, dispatch_category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    load["name"],
                    slug,
                    load["category"],
                    load.get("ha_entity_id"),
                    load.get("ha_energy_entity_id"),
                    load.get("ha_switch_entity_id"),
                    load.get("ha_binary_entity_id"),
                    load.get("measurement_type", "forecast"),
                    float(load.get("peak_kw", 0.0)),
                    float(load.get("avg_kw", 0.0)),
                    load.get("schedule_json", "[]"),
                    int(load.get("enabled", 1)),
                    load.get("gateway_id", "global"),
                    load.get("dispatch_category", "2-Essential Load"),
                )
            )
            # Fetch the generated ID since we use default randomblob
            async with conn.execute("SELECT id FROM forecast_loads WHERE rowid = ?", (cur.lastrowid,)) as id_cur:
                row = await id_cur.fetchone()
                load_id = row["id"]

        await conn.commit()
        return load_id

async def delete_forecast_load(load_id: str) -> None:
    async with get_db() as conn:
        await conn.execute("DELETE FROM forecast_loads WHERE id = ?", (load_id,))
        await conn.commit()


# ── ENERGY DEVICES ──────────────────────────────────────────────────────────────

async def get_all_energy_devices() -> list[dict]:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM sd_energy_devices ORDER BY name ASC"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

async def get_energy_device(device_id: str) -> dict | None:
    async with get_db() as conn:
        async with conn.execute(
            "SELECT * FROM sd_energy_devices WHERE id = ?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def upsert_energy_device(device: dict) -> str:
    """Insert or update an energy device. Provide 'id' to update, omit to insert."""
    async with get_db() as conn:
        if "id" in device and device["id"]:
            device_id = device["id"]
            await conn.execute(
                """
                UPDATE sd_energy_devices
                SET name = ?, category = ?, ha_power_entity = ?, ha_energy_entity = ?, ha_switch_entity = ?,
                    peak_kw = ?, avg_kw = ?, schedule_json = ?, enabled = ?, gateway_id = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    device["name"],
                    device["category"],
                    device.get("ha_power_entity"),
                    device.get("ha_energy_entity"),
                    device.get("ha_switch_entity"),
                    float(device.get("peak_kw", 0.0)),
                    float(device.get("avg_kw", 0.0)),
                    device.get("schedule_json", "[]"),
                    int(device.get("enabled", 1)),
                    device.get("gateway_id", "global"),
                    device_id,
                )
            )
        else:
            import uuid
            device_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO sd_energy_devices
                (id, name, category, ha_power_entity, ha_energy_entity, ha_switch_entity, peak_kw, avg_kw, schedule_json, enabled, gateway_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    device["name"],
                    device["category"],
                    device.get("ha_power_entity"),
                    device.get("ha_energy_entity"),
                    device.get("ha_switch_entity"),
                    float(device.get("peak_kw", 0.0)),
                    float(device.get("avg_kw", 0.0)),
                    device.get("schedule_json", "[]"),
                    int(device.get("enabled", 1)),
                    device.get("gateway_id", "global"),
                )
            )
        await conn.commit()
        return device_id

async def delete_energy_device(device_id: str) -> None:
    async with get_db() as conn:
        await conn.execute("DELETE FROM sd_energy_devices WHERE id = ?", (device_id,))
        await conn.commit()


# ── Smart Dispatch Override Security Sessions & Forecast History ─────────────

async def create_security_session(gateway_id: str, unlocked_at: str, expires_at: str, token: str) -> None:
    """Create a new 24h security override session."""
    async with get_db() as conn:
        # First invalidate any existing sessions
        await conn.execute("DELETE FROM sd_security_sessions WHERE gateway_id = ?", (gateway_id,))
        await conn.execute(
            """
            INSERT INTO sd_security_sessions (gateway_id, unlocked_at, expires_at, session_token)
            VALUES (?, ?, ?, ?)
            """,
            (gateway_id, unlocked_at, expires_at, token)
        )
        await conn.commit()


async def get_active_security_session(gateway_id: str) -> dict | None:
    """Get the active, unexpired security session if it exists."""
    async with get_db() as conn:
        row = await conn.execute(
            """
            SELECT * FROM sd_security_sessions 
            WHERE gateway_id = ? AND datetime('now') < datetime(expires_at)
            """,
            (gateway_id,)
        )
        res = await row.fetchone()
        return dict(res) if res else None


async def delete_active_security_sessions(gateway_id: str) -> None:
    """Clear active sessions for a gateway."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM sd_security_sessions WHERE gateway_id = ?", (gateway_id,))
        await conn.commit()


async def check_and_trigger_session_reversion(gateway_id: str) -> bool:
    """
    Ensure the security override reversion invariant:
    If there is NO active, unexpired security session for this gateway or 'global',
    any modified system safeguards (system_immutable = 1) must be restored to their factory defaults.
    Also deletes any expired sessions from the database.
    """
    import json as _json
    from pathlib import Path
    
    # 1. Clean up/delete any expired sessions from the database first
    async with get_db() as conn:
        await conn.execute(
            """
            DELETE FROM sd_security_sessions
            WHERE datetime('now') >= datetime(expires_at)
            """
        )
        await conn.commit()
        
        # 2. Check if there are any active sessions left for this gateway or 'global'
        async with conn.execute(
            """
            SELECT id FROM sd_security_sessions
            WHERE gateway_id = ? OR gateway_id = 'global'
            """,
            (gateway_id,)
        ) as cur:
            active_session = await cur.fetchone()
            
        if active_session:
            # An active override session is currently valid. Modifications are allowed, so do not revert.
            return False

        # 3. No active override session exists. We must check if any system_immutable rules are modified.
        # Resolve seed path relative to this module (same pattern as
        # `seed_system_strategies` at :2803); the previous hardcoded
        # `/Users/davidhona/dev/...` path silently returned False on any
        # machine other than the original author's laptop, which is why
        # this test was failing on CI (Ubuntu) for months.
        seed_path = Path(__file__).parent.parent.parent / "db" / "seed" / "system_strategies_seed.json"
        if not seed_path.exists():
            return False
            
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                rules = _json.load(f)
        except Exception as e:
            logger.error(f"Failed to load seed JSON in reversion check: {e}")
            return False

        # Let's check if any rule deviates from the seed defaults
        deviates = False
        for rule in rules:
            name = rule["strategy_name"]
            # Fetch current state of this rule in the DB
            async with conn.execute(
                """
                SELECT trigger_category, conditions_json, signals_json, eval_order, enabled 
                FROM sd_strategy_matrix 
                WHERE strategy_name = ? AND system_immutable = 1
                """,
                (name,)
            ) as cur:
                row = await cur.fetchone()
                
            if row:
                trigger_cat, cond_json, sig_json, ev_order, enabled = row
                # Normalize json strings for robust comparison
                try:
                    db_cond = _json.loads(cond_json or "{}")
                    seed_cond = _json.loads(rule["conditions_json"] or "{}")
                    db_sig = _json.loads(sig_json or "[]")
                    seed_sig = _json.loads(rule["signals_json"] or "[]")
                except Exception:
                    deviates = True
                    break
                    
                if (trigger_cat != rule["trigger_category"] or
                    db_cond != seed_cond or
                    db_sig != seed_sig or
                    ev_order != rule["eval_order"] or
                    enabled == 0):
                    deviates = True
                    break
            else:
                # Rule doesn't exist, which is a deviation (it should exist)
                deviates = True
                break
                
        if not deviates:
            return False
            
        # 4. Deviations found! Restore system strategies to safe factory defaults.
        for rule in rules:
            name = rule["strategy_name"]
            await conn.execute(
                """
                UPDATE sd_strategy_matrix
                SET trigger_category=?, conditions_json=?, signals_json=?, eval_order=?, enabled=1
                WHERE strategy_name = ? AND system_immutable = 1
                """,
                (
                    rule["trigger_category"],
                    rule["conditions_json"],
                    rule["signals_json"],
                    rule["eval_order"],
                    name
                )
            )
        await conn.commit()
        
        # 5. Write the audit log entry
        # Exact message required:
        # `[System Cleanup] Security override session expired. Restored 'Negative Export Protection' to active factory default parameters.`
        await conn.execute(
            """
            INSERT INTO admin_audit_log (event, source, user, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                "MIXER_UPDATE",
                "smart_dispatch",
                "system",
                "[System Cleanup] Security override session expired. Restored 'Negative Export Protection' to active factory default parameters."
            )
        )
        await conn.commit()
        
        logger.warning(
            f"Smart Dispatch: security override expired/deleted for gateway '{gateway_id}'. "
            "Restored all system safeguard rules to defaults."
        )
        return True



async def save_forecast_history(generated_at: str, start: str, end: str, plan_json: str) -> None:
    """Save an optimized rolling forecast plan to the forecast history."""
    async with get_db() as conn:
        await conn.execute(
            """
            INSERT INTO sd_forecast_history (generated_at, plan_horizon_start, plan_horizon_end, plan_json)
            VALUES (?, ?, ?, ?)
            """,
            (generated_at, start, end, plan_json)
        )
        await conn.commit()


async def prune_forecast_history(max_keep: int = 48) -> None:
    """Keep only the last max_keep forecast histories."""
    async with get_db() as conn:
        # Delete entries older than the latest max_keep
        await conn.execute(
            """
            DELETE FROM sd_forecast_history
            WHERE id NOT IN (
                SELECT id FROM sd_forecast_history
                ORDER BY id DESC LIMIT ?
            )
            """,
            (max_keep,)
        )
        await conn.commit()


async def get_latest_forecast_history() -> Optional[dict]:
    """Retrieve the newest optimized plan from the forecast history."""
    async with get_db() as conn:
        async with conn.execute(
            """
            SELECT * FROM sd_forecast_history
            ORDER BY id DESC LIMIT 1
            """
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# SmartDispatch lookahead notification dedup (Batch S / v51, 2026-08-02)
# Replaces `SmartDispatchEngine._lookahead_sent` and `._lookahead_last_check`
# in-memory dicts so dedup state survives container restarts. See
# `src/services/smart_dispatch.py::_maybe_send_lookahead_notifications`.

async def get_lookahead_sent_at(
    gateway_serial: str, kind: str, hour_bucket: str = ""
) -> float:
    """Return the last-send unix ts for (gateway, kind, hour_bucket), or 0.0
    if never sent (or pruned). For the per-gateway scan cadence check, pass
    kind='__scan__' and leave hour_bucket empty."""
    async with get_db() as conn:
        async with conn.execute(
            """
            SELECT sent_at FROM sd_lookahead_dedup
            WHERE gateway_serial=? AND kind=? AND hour_bucket=?
            """,
            (gateway_serial, kind, hour_bucket),
        ) as cur:
            row = await cur.fetchone()
    return float(row["sent_at"]) if row else 0.0


async def mark_lookahead_sent(
    gateway_serial: str, kind: str, hour_bucket: str, sent_at: float
) -> None:
    """Record a lookahead notification send (or scan tick, when kind=='__scan__'
    and hour_bucket==''). Upserts so subsequent sends inside the same
    hour-bucket update the timestamp in place."""
    async with get_db() as conn:
        await conn.execute(
            """
            INSERT INTO sd_lookahead_dedup (gateway_serial, kind, hour_bucket, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(gateway_serial, kind, hour_bucket)
            DO UPDATE SET sent_at=excluded.sent_at, created_at=datetime('now')
            """,
            (gateway_serial, kind, hour_bucket, sent_at),
        )
        await conn.commit()


async def prune_lookahead_sent(older_than_secs: int = 86400) -> int:
    """Delete dedup rows older than `older_than_secs` (default 24h). Returns
    row count deleted. Called opportunistically at the end of each scan."""
    import time as _time
    cutoff = _time.time() - older_than_secs
    async with get_db() as conn:
        cur = await conn.execute(
            "DELETE FROM sd_lookahead_dedup WHERE sent_at < ?",
            (cutoff,),
        )
        await conn.commit()
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# SmartDispatch Macro Discovery snapshots (Phase 2.A / v52, 2026-08-05)
# Written daily @03:00 site-local per gateway by
# `src/services/smart_dispatch/macro.py::MacroDiscovery.run`. Read by
# the future Meso planner as one input to the 24h dispatch plan.

async def save_macro_snapshot(gateway_serial: str, snapshot_json: str) -> None:
    """Insert a Macro Discovery snapshot for `gateway_serial`. Snapshots are
    append-only; the Meso planner uses `get_latest_macro_snapshot` to fetch
    the freshest one. Older rows can be pruned via `prune_macro_snapshots`."""
    async with get_db() as conn:
        await conn.execute(
            """
            INSERT INTO sd_macro_snapshot (gateway_serial, snapshot_json)
            VALUES (?, ?)
            """,
            (gateway_serial, snapshot_json),
        )
        await conn.commit()


async def get_latest_macro_snapshot(gateway_serial: str) -> Optional[dict]:
    """Return the newest Macro snapshot row for a gateway, or None."""
    async with get_db() as conn:
        async with conn.execute(
            """
            SELECT id, gateway_serial, generated_at, snapshot_json
            FROM sd_macro_snapshot
            WHERE gateway_serial = ?
            ORDER BY generated_at DESC, id DESC
            LIMIT 1
            """,
            (gateway_serial,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def prune_macro_snapshots(keep_last_n: int = 30) -> int:
    """Retain the newest `keep_last_n` snapshots per gateway (default 30 =
    one month at daily cadence). Called opportunistically after each Macro
    run. Returns row count deleted."""
    async with get_db() as conn:
        # Row-number window per gateway; delete anything past keep_last_n.
        cur = await conn.execute(
            """
            DELETE FROM sd_macro_snapshot
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                        ROW_NUMBER() OVER (
                            PARTITION BY gateway_serial
                            ORDER BY generated_at DESC, id DESC
                        ) AS rn
                    FROM sd_macro_snapshot
                )
                WHERE rn > ?
            )
            """,
            (keep_last_n,),
        )
        await conn.commit()
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Home Assistant Multi-Instance & Notification Devices CRUD Helpers
# ---------------------------------------------------------------------------

async def get_ha_instances() -> list[dict]:
    """Return all configured Home Assistant instances."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM ha_instances ORDER BY created_at") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_ha_instance(instance_id: str) -> Optional[dict]:
    """Return a specific Home Assistant instance by ID."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM ha_instances WHERE id = ?", (instance_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_ha_instance(inst: dict) -> None:
    """Insert or update a Home Assistant instance."""
    import uuid
    instance_id = inst.get("id") or str(uuid.uuid4())
    enabled = int(bool(inst.get("enabled", 1)))
    is_default = int(bool(inst.get("is_default", 0)))

    async with get_db() as conn:
        if is_default:
            # Clear other defaults first
            await conn.execute("UPDATE ha_instances SET is_default = 0")
        
        # Check if this is the only one, or if there is no default yet, make it default
        async with conn.execute("SELECT COUNT(*) FROM ha_instances WHERE is_default = 1") as cur:
            default_count = (await cur.fetchone())[0]
        if default_count == 0:
            is_default = 1

        await conn.execute(
            """
            INSERT INTO ha_instances (id, alias, host, token, enabled, is_default)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                alias = excluded.alias,
                host = excluded.host,
                token = excluded.token,
                enabled = excluded.enabled,
                is_default = excluded.is_default
            """,
            (
                instance_id,
                inst.get("alias", "Home Assistant"),
                inst.get("host", ""),
                inst.get("token", ""),
                enabled,
                is_default
            )
        )
        await conn.commit()


async def delete_ha_instance(instance_id: str) -> None:
    """Delete a Home Assistant instance. If it was default, make another one default."""
    async with get_db() as conn:
        # Check if we are deleting the default instance
        async with conn.execute("SELECT is_default FROM ha_instances WHERE id = ?", (instance_id,)) as cur:
            row = await cur.fetchone()
            was_default = row[0] if row else 0

        # Cascade delete is handled by sqlite's foreign keys, but we'll execute it
        await conn.execute("DELETE FROM ha_instances WHERE id = ?", (instance_id,))
        
        if was_default:
            # Set another instance as default if any exist
            async with conn.execute("SELECT id FROM ha_instances LIMIT 1") as cur:
                next_row = await cur.fetchone()
                if next_row:
                    await conn.execute("UPDATE ha_instances SET is_default = 1 WHERE id = ?", (next_row[0],))
        await conn.commit()


async def get_notification_devices() -> list[dict]:
    """Return all configured notification target devices with their HA instance details."""
    async with get_db() as conn:
        async with conn.execute(
            """
            SELECT d.*, i.alias AS ha_instance_alias, i.host AS ha_instance_host
            FROM notification_devices d
            LEFT JOIN ha_instances i ON d.ha_instance_id = i.id
            ORDER BY d.created_at
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_notification_device(device_id: str) -> Optional[dict]:
    """Return a specific notification device by ID."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM notification_devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_notification_device(dev: dict) -> None:
    """Insert or update a notification device."""
    import uuid
    device_id = dev.get("id") or str(uuid.uuid4())
    enabled = int(bool(dev.get("enabled", 1)))
    owner_username = dev.get("owner_username")

    async with get_db() as conn:
        await conn.execute(
            """
            INSERT INTO notification_devices (id, ha_instance_id, alias, service_target, enabled, owner_username)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ha_instance_id = excluded.ha_instance_id,
                alias = excluded.alias,
                service_target = excluded.service_target,
                enabled = excluded.enabled,
                owner_username = excluded.owner_username
            """,
            (
                device_id,
                dev.get("ha_instance_id", ""),
                dev.get("alias", "My Device"),
                dev.get("service_target", ""),
                enabled,
                owner_username
            )
        )
        await conn.commit()


async def delete_notification_device(device_id: str) -> None:
    """Delete a notification device."""
    async with get_db() as conn:
        await conn.execute("DELETE FROM notification_devices WHERE id = ?", (device_id,))
        await conn.commit()


# ── Pricing Models Decoupled Helper Operations ───────────────────────

async def get_pricing_model(model_id: str) -> Optional[dict]:
    """Return a specific pricing model."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM pricing_models WHERE id = ?", (model_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_pricing_models() -> list[dict]:
    """Return all pricing models."""
    async with get_db() as conn:
        async with conn.execute("SELECT * FROM pricing_models ORDER BY id") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_pricing_model(model_id: str, credentials: str | dict, settings: str | dict) -> None:
    """Update credentials and settings for a specific pricing model."""
    if isinstance(credentials, dict):
        credentials = json.dumps(credentials)
    if isinstance(settings, dict):
        settings = json.dumps(settings)
    async with get_db() as conn:
        await conn.execute(
            """UPDATE pricing_models 
               SET credentials = ?, settings = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (credentials, settings, model_id)
        )
        await conn.commit()


async def get_pricing_model_status_metadata() -> dict[str, str]:
    """Return computed Active/Available/Unavailable statuses for all pricing models."""
    async with get_db() as conn:
        # Get distinct pricing_model_ids for active services linked to gateways
        async with conn.execute(
            """SELECT DISTINCT COALESCE(pricing_model_id, 'franklinwh_tou')
               FROM utility_services 
               WHERE id IN (SELECT DISTINCT utility_service_id FROM agate_utility_links)"""
        ) as cur:
            active_ids = {r[0] for r in await cur.fetchall() if r[0]}

        # Retrieve all models to evaluate status
        async with conn.execute("SELECT id, credentials FROM pricing_models") as cur:
            models = await cur.fetchall()

    statuses = {}
    for m_id, creds_str in models:
        if m_id in active_ids:
            statuses[m_id] = "Active"
            continue

        try:
            creds = json.loads(creds_str or "{}")
        except Exception:
            creds = {}

        if m_id == "amber":
            api_token = creds.get("api_token")
            is_available = bool(api_token)
        elif m_id == "localvolts":
            api_key = creds.get("api_key")
            partner_id = creds.get("partner_id")
            nmi_id = creds.get("nmi_id")
            is_available = bool(api_key and partner_id and nmi_id)
        else:
            is_available = True

        statuses[m_id] = "Available" if is_available else "Unavailable"

    return statuses



