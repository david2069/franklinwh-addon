/**
 * FranklinWH HA Integrator — Frontend App State
 * Alpine.js global store + dashboard logic
 */

// ── Utility ─────────────────────────────────────────────────
const fmt = {
  soc:   (v) => v != null ? `${Math.round(v)}` : '--',
  kw:    (v) => v != null ? `${(+v).toFixed(2)}` : '--',
  kwh:   (v) => v != null ? `${(+v).toFixed(1)}` : '--',
  hz:    (v) => v != null ? `${(+v).toFixed(2)}` : '--',
  // Display the model the backend resolved. Do not map hardware versions here.
  //
  // This used to read 101 -> aPower, 102 -> aGate, 103 -> aHub. The seeded
  // catalog says 100-104 are all device_class "agate", name "aGate X" --
  // hardware revisions of one product told apart by SKU (AGT-R1V1-US,
  // AGT-R1V2-US, AGT-R1V1-AU, AGT-R1V3-US), not different products. A US
  // gateway on 101 therefore rendered as "aPower", and no version maps to an
  // aHub at all. sysHdVersion comes from getHomeGatewayList and describes the
  // gateway, so an aPower cannot be reporting one.
  //
  // _resolve_models() in api_gateways.py resolves it through db.get_device_model(),
  // which honours admin overrides -- a table here would silently outrank them,
  // and hardcoding a hardware map in the UI is what .cursorrules rule 5 forbids.
  // A bare integer means resolution did not happen: label it a hardware version
  // rather than name a product we cannot identify, matching the backend's own
  // "aGate (hw N)" wording for a version the catalog does not know.
  model: (v) => {
    if (!v) return '—';
    const s = String(v).trim();
    return /^\d+$/.test(s) ? `aGate (hw ${s})` : s;
  },
  ago:   (s) => {
    if (s == null) return 'never';
    if (s < 60)   return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s/60)}m ago`;
    return `${Math.floor(s/3600)}h ago`;
  },
};

function socBarColour(soc) {
  if (soc == null) return 'var(--text-muted)';
  if (soc >= 80) return 'var(--ok)';
  if (soc >= 30) return 'var(--accent)';
  if (soc >= 15) return 'var(--warn)';
  return 'var(--danger)';
}

function socRingOffset(soc) {
  // stroke-dasharray = 238.8 (2π × 38)  — ring radius updated to 38
  if (soc == null) return 238.8;
  const pct = Math.min(100, Math.max(0, soc)) / 100;
  return 238.8 * (1 - pct);
}

function socReserveDotOffset(reserve) {
  // Returns stroke-dashoffset to place a tiny 1-unit arc AT the reserve SOC angle.
  // dasharray="1 237.8" + this offset = dot appears exactly at the reserve% mark.
  if (reserve == null || reserve <= 0) return 238.8;
  const pct = Math.min(100, Math.max(0, reserve)) / 100;
  // dashoffset = C - position + (1/2) centering the 1px dot = C - position
  return 238.8 - (pct * 238.8);
}

// ── Polling ──────────────────────────────────────────────────
async function fetchJSON(url, options = {}) {
  try {
    if (options.body && !options.headers) {
      options.headers = { 'Content-Type': 'application/json' };
    }
    const r = await fetch(url, options);
    if (!r.ok) {
      let detail = `HTTP ${r.status}`;
      try {
        const j = await r.json();
        if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
        else if (j && j.error) detail = j.error;
      } catch (err) { /* ignore */ }
      throw new Error(detail);
    }
    return await r.json();
  } catch (e) {
    console.warn('[FWH]', url, e.message);
    return { ok: false, error: e.message };
  }
}

// ── Main Alpine App ──────────────────────────────────────────
document.addEventListener('alpine:init', () => {

  Alpine.store('app', {
    // Tab navigation
    activeTab: 'dashboard',
    // Open on a desktop, closed on a phone. It used to default open
    // everywhere, so on a narrow screen the sidebar covered the content on
    // every load — and if the page then ran out of memory, reopening it landed
    // on the same covered view.
    sidebarCollapsed: window.matchMedia('(max-width: 1023px)').matches,

    // Reactive, unlike the `window.innerWidth < 1024` that used to sit inside
    // an x-show expression: Alpine evaluates that once and never again, so
    // rotating the phone left the overlay backdrop in the wrong state.
    isNarrow: window.matchMedia('(max-width: 1023px)').matches,

    // Gateway data
    gateways: [],          // from /api/gateways
    gatewaysLoaded: false, // track initial payload state
    gatewayStatus: [],     // from /api/gateways/status/all
    selectedGateway: null, // short_id of selected gateway

    // Front-end asset fingerprint, from /api/health. Captured on the first
    // poll; a later change means the JS and CSS on the server no longer match
    // what this tab is running.
    assetsId: null,
    assetsStale: false,

    // System info (from /api/health)
    health: {},
    mqttConnected: false,
    env: '',
    version: '--',

    dashboardSiteFilter: 'ALL',
    amberTabVisible: false,   // set true when provider=amber or emulation enabled
    sdView: 'overview',    // sub-tab: 'overview'|'dispatch'|'prices'|'notifications'
    sdEngineMode: 'signal_only', // loaded from /api/automation/signal on init; 'signal_only'|'active'|'paused'
    // per-gateway: 'info'|'auto'|'user_approval'|'disabled' — synced from engine panel.
    // MUST start null, not a guessed value (GH #8). The pill renders whatever is
    // here until api/automation/signal resolves, so any hardcoded default is a
    // claim about the backend we have not verified yet. null renders the neutral
    // "…" / "Loading…" state that sdModeBadge() and smart_dispatch.html already
    // provide, and no approval UI is shown until the server confirms the mode.
    sdDispatchStrategy: null,
    // SD tab auto-refresh preference — default OFF per user directive (backlog
    // Issue B, 2026-07-08 → 2026-07-11). When true, sdEnginePanel + sdAuditLog
    // resume 60s ticks; also gated by document.visibilityState so hidden tabs
    // don't churn. Persisted to localStorage so it survives page reload.
    sdAutoRefresh: (typeof localStorage !== 'undefined' && localStorage.getItem('fhai_sd_auto_refresh') === 'true'),
    setSdAutoRefresh(v) {
      const b = !!v;
      this.sdAutoRefresh = b;
      try { localStorage.setItem('fhai_sd_auto_refresh', b ? 'true' : 'false'); } catch (e) {}
    },
    // Reactive mirror of document.visibilityState so templates can render
    // "Paused (tab hidden)" without each component wiring its own
    // visibilitychange listener. Updated by the alpine:init listener below.
    docVisible: (typeof document !== 'undefined') && document.visibilityState === 'visible',
    haConnectedGlobal: false,       // true when HA REST API is reachable
    haNotifConfigured: false,       // true when ha_target is set AND ha_enabled=true
    haImpaired: false,              // true when strategy=user_approval but HA not ready
    shadowModeActive: false,        // true when SD shadow_mode is enabled
    isSolarActive: true,
    isDynamicPricing: true,
    weatherInfluenceEnabled: true,
    siteProfile: 'hems',

    setSdView(v) { this.sdView = v; },

    // SD status badge — label + colours used by sidebar pill and SD page header.
    // strategy: 'disabled'|'info'|'auto'|'user_approval'
    sdModeBadge() {
      const s = this.sdDispatchStrategy;
      const shadow = this.shadowModeActive;
      const prefix = shadow ? '[SHADOW] ' : '';
      
      if (s === 'disabled') return { label: prefix + 'Off',  cls: 'bg-red-500/20 text-red-400 border-red-500/40',    glowing: true  };
      if (s === 'info')     return { label: prefix + 'Info', cls: 'bg-blue-500/20 text-blue-300 border-blue-500/40',  glowing: false };
      if (s === 'user_approval') return { label: prefix + 'User', cls: 'bg-amber-500/20 text-amber-300 border-amber-500/40', glowing: false };
      if (s === 'auto')     return { label: prefix + 'Auto', cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40', glowing: shadow };
      return { label: '…', cls: 'bg-gray-700 text-gray-400 border-gray-600', glowing: false };
    },

    // Schedule mode badge — Running|Custom based on gateway runtime_mode + active_dispatch_name
    scheduleBadge() {
      const gw = this.currentGateway;
      if (!gw) return null;
      const mode = (gw.runtime_mode || gw.operating_mode || '').toLowerCase();
      if (!mode.includes('time') && !mode.includes('tou')) return null;
      // Custom Sandbox blocks are named by scheduler_core: 'Grid Charge', 'Grid Discharge', 'Standby'
      const dispatchName = (gw.active_dispatch_name || '').toLowerCase();
      const isCustom = ['grid charge', 'grid discharge', 'standby'].some(n => dispatchName.includes(n));
      if (isCustom) return { label: 'Custom', cls: 'bg-orange-500/20 text-orange-300 border-orange-500/40', title: 'TOU Custom Sandbox active (SD or Automation override)' };
      return { label: 'Running', cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40', title: 'Normal Time-of-Use schedule running' };
    },

    // using $store.app.presetRoleDescription() always resolve, even before
    // the smartDispatchPanel component scope initialises (Tailwind CDN scans DOM).
    presetRoleDescription(role) {
      const m = {
        baseline:       'The fallback preset — applied whenever no higher-priority rule matches. Typically a self-consumption or TOU hold mode. Required for auto-execution.',
        force_charge:   'Applied when Amber prices are extremely low or negative. Forces battery to charge from grid to maximise off-peak storage.',
        peak_discharge: 'Applied during PEAK or SPIKE tariff periods. Discharges battery to reduce grid import at the highest-cost times.',
        solar_sponge:   'Applied during solarSponge / Export Bonus windows when feed-in prices are high. Maximises export revenue or solar absorption.',
        spike_hold:     'Applied when a confirmed price spike is detected. Holds battery in reserve or pauses grid interactions to protect against spike costs.',
      };
      return m[role] || 'Custom preset — role not categorised. Verify its TOU blocks and dispatch mode in the Schedule tab.';
    },

    // Dispatch mode label + colour for TOU block badges (FranklinWH dispatchId mapping)
    dispatchModeBadge(dispatchId) {
      const MAP = {
        1: { label: 'Self-Use',      cls: 'bg-emerald-900/60 text-emerald-300 border-emerald-700/50' },
        2: { label: 'Charge Grid',   cls: 'bg-teal-900/60    text-teal-300    border-teal-700/50'    },
        3: { label: 'Discharge',     cls: 'bg-orange-900/60  text-orange-300  border-orange-700/50'  },
        4: { label: 'Force Charge',  cls: 'bg-cyan-900/60    text-cyan-300    border-cyan-700/50'    },
        5: { label: 'Feed-In',       cls: 'bg-yellow-900/60  text-yellow-300  border-yellow-700/50'  },
        6: { label: 'Grid Hold',     cls: 'bg-gray-800       text-gray-400    border-gray-600'       },
        7: { label: 'Eco Mode',      cls: 'bg-lime-900/60    text-lime-300    border-lime-700/50'    },
      };
      return MAP[dispatchId] || { label: `Mode ${dispatchId ?? '?'}`, cls: 'bg-gray-800 text-gray-400 border-gray-600' };
    },

    // setEngineMode — kept on store so @click="$store.app.setEngineMode()" always
    // resolves regardless of whether smartDispatchPanel() scope has mounted.
    // After the API call succeeds, dispatches 'amber:engine-mode-changed' so the
    // panel component can refresh its signal without being tightly coupled.
    async setEngineMode(mode) {
      try {
        const r = await fetch('api/automation/engine/mode', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode }),
        });
        if (r.ok) {
          this.sdEngineMode = mode;
          // Notify the smartDispatchPanel component to refresh its signal
          window.dispatchEvent(new CustomEvent('sd:engine-mode-changed', { detail: { mode } }));
        }
      } catch (e) {
        console.warn('[FWH] setEngineMode failed', e);
      }
    },

    get currentGateway() {
      if (!this.selectedGateway || !Array.isArray(this.gateways)) return null;
      return this.gateways.find(g => g.short_id === this.selectedGateway);
    },

    get isVppMode() {
      const gw = this.currentGateway;
      if (!gw) return false;
      const rt = gw.runtime_mode;
      return rt === 'VPP Mode' || (gw.run_status_desc || '').toLowerCase().includes('vpp');
    },

    get vppPowerKw() {
      const gw = this.currentGateway;
      if (!gw) return 0.0;
      return gw.battery_kw || 0.0;
    },

    get activeMode() {
      const gw = this.currentGateway;
      if (!gw) return null;
      const rt = gw.runtime_mode;
      // VPP Mode: either runtime_mode is 'VPP Mode' OR run_status_desc signals VPP dispatch
      if (rt === 'VPP Mode' || (gw.run_status_desc || '').toLowerCase().includes('vpp')) {
        return { id: 4, name: 'VPP Mode', desc: 'Virtual Power Plant dispatched', icon: 'fas fa-network-wired', textClass: 'c-live' };
      }
      // Manual Dispatch Mode (e.g., Manual Charge, Manual Discharge)
      if (rt && rt.startsWith('Manual ')) {
        const action = rt.replace('Manual ', '');
        return {
          id: 5,
          name: rt,
          desc: `Manual ${action.toLowerCase()} dispatch active`,
          icon: action === 'Charge' ? 'fas fa-arrow-down' : 'fas fa-arrow-up',
          textClass: 'c-accent'
        };
      }
      // Primary: match by runtime_mode string name (e.g. "Time-of-Use", "Self-Consumption")
      if (rt) {
        const byName = this.MODES.find(m => m.name.toLowerCase() === rt.toLowerCase());
        if (byName) return byName;
      }
      // Fallback: match by integer operating_mode_id (work_mode: 1/2/3)
      if (gw.operating_mode_id) {
        return this.MODES.find(m => m.id === gw.operating_mode_id) || null;
      }
      return null;
    },


    getBatteryStateText(gw) {
      if (!gw) return 'Standby';
      if (gw.runtime_mode === 'Manual Charge') return 'Charging';
      if (gw.runtime_mode === 'Manual Discharge') return 'Discharging';
      if (gw.battery_kw != null) {
        if (gw.battery_kw < -0.05) return 'Charging';
        if (gw.battery_kw > 0.05) return 'Discharging';
      }
      if (gw.run_status_desc) {
        const desc = gw.run_status_desc.toLowerCase();
        if (desc.includes('charging') || desc.includes('charge')) return 'Charging';
        if (desc.includes('discharging') || desc.includes('discharge')) return 'Discharging';
        if (desc.includes('standby')) return 'Standby';
      }
      return 'Standby';
    },

    runStatus(gw) {
      if (!gw) return '—';
      return gw.run_status_desc || gw.runtime_mode || gw.operating_mode || 'Standby';
    },

    // UI state
    loading: true,
    refreshing: false,
    lastRefresh: null,
    refreshInterval: null,
    toasts: [],
    toastAutoDismiss: true,
    toastDismissDelaySeconds: 10,
    automationsPinEnabled: false,
    suppressedTabs: "",
    enabledModules: [],
    // Persona gates (v0.6.0, GH #11) — server-computed tab visibility from
    // the auto-detected persona matrix. { <tab_slug>: bool }. Missing key
    // means "show" (permissive default). Populated from /api/system/features.
    personaGates: {},

    // Cross-tab deep-link: set before calling setTab('logs')
    // Format: { level, source, search, since_hours }  (all optional)
    pendingLogFilter: null,


    /** Notice that the server's front-end assets changed, and offer a reload.
     *
     * Offer, never act. Reloading silently would discard a part-written form —
     * a TOU block, a password, an unsaved schedule — to fix a problem the user
     * has not noticed yet. So this is a toast with a button and no auto-dismiss:
     * it waits as long as it takes, and the user reloads when they are ready.
     *
     * Fires once per tab. A second prompt for the same fact is just noise.
     */
    checkAssetsFreshness(health) {
      const id = health && health.assets_id;
      if (!id) return;

      if (this.assetsId === null) {
        this.assetsId = id;
        return;
      }
      if (id === this.assetsId || this.assetsStale) return;

      this.assetsStale = true;
      this.addToast('A newer version of the dashboard is available.', 'warn', {
        detail: 'This tab is running older JavaScript. Reload when convenient — '
              + 'anything you are part-way through typing will be lost.',
        autoDismiss: false,
        action: { label: 'Reload', fn: () => window.location.reload() },
      });
    },

    /**
     * Show a toast notification.
     * Backward-compatible: existing addToast(msg, level) calls work unchanged.
     * New callers may pass opts for richer behaviour — see docs/notifications.md.
     *
     * @param {string} msg              Primary message ("Subject → Value" for new calls)
     * @param {string} [level='info']   'critical'|'warn'|'error'|'info' (aliases: 'ok','success' → 'info')
     * @param {object} [opts]           Optional — all fields optional
     * @param {string}   [opts.detail]      Secondary context line
     * @param {boolean}  [opts.autoDismiss] Auto-dismiss after dismissMs (opt-in, info-level only)
     * @param {number}   [opts.dismissMs]   Auto-dismiss delay ms (default 6000)
     * @param {object}   [opts.action]      CTA button: { label, fn }
     * @param {boolean}  [opts.log]         Override server logging (default true for warn/critical)
     */
    // ── Confirmation dialogs ─────────────────────────────────────────────
    //
    // window.confirm() is a browser chrome dialog: unstyled, unthemed, and on
    // some platforms it does not even look like it belongs to the page it is
    // interrupting. For a prompt that asks "shall I turn off HTTPS" that reads
    // as something the browser is warning you about rather than something the
    // app is asking — which is exactly the wrong impression.
    //
    // Promise-based so call sites stay one line:
    //     if (!await this.$store.app.confirm({ ... })) return;
    confirmState: {
      open: false, title: '', body: '', detail: '',
      confirmLabel: 'Confirm', cancelLabel: 'Cancel', danger: false,
    },
    _confirmResolve: null,

    // ── Relay + gateway-clock helpers ─────────────────────────────────────
    //
    // The relay row rendered "GRD:O GEN:O SOL:O" whatever the gateway was
    // doing. The template compared `relay === 0`, but gateway_service's
    // _relay_on() returns a boolean, and `false === 0` is false in JS — so
    // closed, open and unknown all fell through to the same branch. Every
    // possible value displayed as 'O'.
    //
    // The labels were also ambiguous. FranklinWH's schema calls 1 "OPEN",
    // meaning *connected* — the opposite of the electrical convention where an
    // open relay passes nothing. ON/OFF says what is true without inheriting
    // that trap.
    relayLabel(value) {
      if (value === null || value === undefined) return '—';
      return value ? 'ON' : 'OFF';
    },

    relayTitle(name, value) {
      if (value === null || value === undefined) return `${name}: not reported`;
      return value
        ? `${name}: connected (FranklinWH reports this as OPEN)`
        : `${name}: disconnected`;
    },

    relayClass(value, onClass) {
      if (value === null || value === undefined) return 'text-[var(--text-muted)]';
      return value ? onClass : 'text-[var(--text-muted)]';
    },

    /** Wall-clock time where the gateway is, from its configured timezone.
     *
     * Not a reading of the device's own clock — the cloud API exposes no such
     * field. It is the current instant rendered in the gateway's IANA zone,
     * which is what you need when reading a TOU schedule whose blocks are
     * local wall-clock.
     */
    gatewayTime(timezone) {
      if (!timezone) return null;
      try {
        return new Intl.DateTimeFormat(undefined, {
          timeZone: timezone, hour: '2-digit', minute: '2-digit',
          second: '2-digit', hour12: false,
        }).format(new Date());
      } catch (e) {
        return null;   // an unknown zone must not take the row down
      }
    },

    /** SQLite datetime('now') is UTC and stored without a marker, so
     *  `new Date('2026-09-13 23:31:14')` parsed it as local and displayed a
     *  time ten hours off. */
    formatUtcStamp(value, timezone) {
      if (!value) return '—';
      const iso = String(value).includes('T')
        ? String(value)
        : String(value).replace(' ', 'T');
      const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
      if (isNaN(d)) return String(value);
      try {
        return d.toLocaleString(undefined, timezone ? { timeZone: timezone } : {});
      } catch (e) {
        return d.toLocaleString();
      }
    },

    confirm(opts = {}) {
      // A second prompt while one is open would orphan the first promise and
      // hang whatever was awaiting it.
      if (this.confirmState.open) return Promise.resolve(false);

      Object.assign(this.confirmState, {
        open: true,
        title: opts.title || 'Are you sure?',
        body: opts.body || '',
        detail: opts.detail || '',
        confirmLabel: opts.confirmLabel || 'Confirm',
        cancelLabel: opts.cancelLabel || 'Cancel',
        danger: Boolean(opts.danger),
      });
      return new Promise((resolve) => { this._confirmResolve = resolve; });
    },

    resolveConfirm(answer) {
      this.confirmState.open = false;
      const resolve = this._confirmResolve;
      this._confirmResolve = null;
      if (resolve) resolve(Boolean(answer));
    },

    addToast(msg, level = 'info', opts = {}) {
      // Normalise aliases — transparent to all existing call sites.
      //
      // 'warning' is here because it was NOT handled and the markup styles only
      // critical/error/warn/info: a 'warning' toast rendered with no border, no
      // icon and no colour, so a rejected form looked like nothing had
      // happened at all. An unrecognised level must degrade to something
      // visible, never to invisible.
      if (level === 'ok' || level === 'success') level = 'info';
      else if (level === 'warning') level = 'warn';
      else if (!['info', 'warn', 'error', 'critical'].includes(level)) level = 'info';

      const id = Date.now() + Math.random();
      
      // Inherit the global auto-dismiss preference, with two exceptions.
      //
      // A toast carrying an action button is the only route to that action —
      // "Restart now", "Reload" — so timing it out destroys the thing it
      // exists for, and the user is left knowing something is needed but not
      // how to do it. Same for critical: if it mattered enough to be critical,
      // it matters enough to still be there when they look back at the screen.
      //
      // An explicit opts.autoDismiss still wins, so a caller can opt a
      // transient actionable toast back in deliberately.
      const stickyByDefault = Boolean(opts.action) || level === 'critical';
      const autoDismiss = opts.autoDismiss !== undefined
        ? opts.autoDismiss
        : (stickyByDefault ? false : this.toastAutoDismiss);
      const delayMs = opts.dismissMs || (this.toastDismissDelaySeconds * 1000) || 10000;

      const toast = {
        id,
        msg,
        level,
        ts: Date.now(),                        // always present — displayed as relative time
        detail:      opts.detail      || null,
        action:      opts.action      || null,  // { label, fn }
        autoDismiss: autoDismiss,
      };
      this.toasts.push(toast);

      // Auto-dismiss if enabled (either globally or via opts)
      if (autoDismiss) {
        setTimeout(() => this.dismissToast(id), delayMs);
      }

      // Server-side log: warn + critical only (fire-and-forget, never blocks UI)
      const shouldLog = opts.log !== false && (level === 'warn' || level === 'critical');
      if (shouldLog) {
        fetch('api/system/toast-log', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ level, msg, detail: opts.detail || null }),
        }).catch(() => {}); // intentionally swallowed — log failure must not break UI
      }
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    // Settings & Modals
    showSettings: false,
    showAddGateway: false,
    showLinkedGateway: false,
    showMqttExplorer: false,
    showMqttExplorer: false,
    theme: localStorage.getItem('fwh_theme') || 'auto',
    iconTheme: localStorage.getItem('fwh_icon_theme') || 'vivid',
    accent: localStorage.getItem('fwh_accent') || 'orange',
    accentColors: [
      { id: 'blue',   hex: '#3b82f6', label: 'Ocean Blue' },
      { id: 'green',  hex: '#10b981', label: 'Forest Green' },
      { id: 'purple', hex: '#8b5cf6', label: 'Royal Purple' },
      { id: 'orange', hex: '#f97316', label: 'Sunset Orange' },
      { id: 'gray',   hex: '#64748b', label: 'Slate Gray' },
    ],
    haConfig: { host: '', token: '', showToken: false, saving: false, testing: false, result: null },


    MODES: [
      { id: 1, name: 'Time-of-Use',      desc: 'Optimise costs using peak/off-peak schedules', icon: 'fas fa-clock', textClass: 'c-accent' },
      { id: 2, name: 'Self-Consumption', desc: 'Maximise use of solar energy at home', icon: 'fas fa-leaf', textClass: 'c-ok' },
      { id: 3, name: 'Emergency Backup', desc: 'Maximum battery retention for outages', icon: 'fas fa-exclamation-triangle', textClass: 'c-live' }
    ],

    setTheme(t) {
      this.theme = t;
      localStorage.setItem('fwh_theme', t);
      this.applyTheme();
    },
    setAccent(c) {
      this.accent = c;
      localStorage.setItem('fwh_accent', c);
      document.documentElement.setAttribute('data-accent', c);
    },
    applyTheme() {
      let active = this.theme;
      if (active === 'auto') {
        active = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      }
      document.documentElement.setAttribute('data-theme', active);
      if (active === 'dark') {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    },
    setIconTheme(t) {
      this.iconTheme = t;
      localStorage.setItem('fwh_icon_theme', t);
      this.applyIconTheme();
    },
    applyIconTheme() {
      document.documentElement.setAttribute('data-icon-theme', this.iconTheme);
    },

    // Init
    async init() {
      this.initRouting();
      this.applyTheme();
      this.applyIconTheme();
      document.documentElement.setAttribute('data-accent', this.accent);
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
        if (this.theme === 'auto') this.applyTheme();
      });

      this.loadHaConfig();

      await this.refresh();
      this.loading = false;
      // Auto-refresh every 15 seconds
      this.refreshInterval = setInterval(() => this.refresh(), 15000);

      // Load Toast and other settings from Features API (non-blocking)
      fetchJSON('api/system/features').then(d => {
        if (d) {
          this.toastAutoDismiss = d.toast_auto_dismiss !== false;
          this.toastDismissDelaySeconds = d.toast_dismiss_delay_seconds || 10;
          this.automationsPinEnabled = d.automations_pin_enabled !== false;
          this.suppressedTabs = d.suppressed_tabs || "";
          this.enabledModules = d.enabled_modules || [];
          this.personaGates = d.persona_gates || {};
        }
      }).catch(() => {});

      // Check Amber tab visibility (fire-and-forget — non-blocking)
      fetchJSON('api/pricing/config').then(r => {
        if (r?.ok) {
          const isAmber  = r.data?.provider === 'amber';
          const emulate  = r.data?.settings?.amber_emulation_mode;
          this.amberTabVisible = isAmber || !!emulate;
        }
      }).catch(() => {});

      // Load engine mode + dispatch strategy so badge shows correct state on first render.
      // Pass the current gateway short_id so the strategy reflects the gateway-specific config.
      const _initGwId = this.currentGateway?.short_id || 'global';
      fetchJSON(`api/automation/signal?short_id=${_initGwId}`).then(d => {
        if (d?.engine_mode) this.sdEngineMode = d.engine_mode;
        if (d?.dispatch_strategy) this.sdDispatchStrategy = d.dispatch_strategy;
        if (d?.shadow_mode !== undefined) this.shadowModeActive = !!d.shadow_mode;
        // Recompute impairment now the real strategy has landed. Without this
        // the flag keeps whatever the api/ha/status callback computed, which
        // races: if ha/status resolves first it evaluated against the unknown
        // (null) strategy and would never see user_approval.
        this._updateHaImpairment();
      }).catch(() => {});

      // Load HA connectivity status for impairment detection (non-blocking)
      fetchJSON('api/ha/status').then(d => {
        if (d) {
          this.haConnectedGlobal = !!d.connected;
          // haNotifConfigured: HA is connected AND a notification target is configured
          this.haNotifConfigured = !!d.connected && !!d.ha_target_set;
        }
        this._updateHaImpairment();
      }).catch(() => {});
    },

    _updateHaImpairment() {
      // Impaired = strategy requires HA but HA is not ready
      this.haImpaired = (this.sdDispatchStrategy === 'user_approval') && !this.haNotifConfigured;
    },

    async loadHaConfig() {
      try {
        const r = await fetch('api/ha/config');
        const d = await r.json();
        this.haConfig.host = d.ha_host || '';
        this.haConfig.token = ''; // Never pre-fill
      } catch (e) { console.warn('[FWH] Failed to load HA config', e); }
    },

    async saveHaConfig() {
      this.haConfig.saving = true;
      this.haConfig.result = null;
      try {
        const r = await fetch('api/ha/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ha_host: this.haConfig.host, ha_token: this.haConfig.token })
        });
        const d = await r.json();
        if (d.ok !== false) {
          this.haConfig.token = '';
          this.haConfig.result = { ok: true, msg: '✓ Settings saved' };
          window.dispatchEvent(new CustomEvent('ha:connection-updated'));
        } else {
          this.haConfig.result = { ok: false, msg: '✗ ' + (d.error || 'Save failed') };
        }
      } catch (e) {
        this.haConfig.result = { ok: false, msg: '✗ Error' };
      } finally {
        this.haConfig.saving = false;
        setTimeout(() => this.haConfig.result = null, 5000);
      }
    },

    async testHaConfig() {
      this.haConfig.testing = true;
      this.haConfig.result = null;
      try {
        const r = await fetch('api/ha/test', { method: 'POST' });
        const d = await r.json();
        if (d.ok) {
          this.haConfig.result = { ok: true, msg: '✓ Connected: ' + (d.ha_name || 'HA') };
        } else {
          this.haConfig.result = { ok: false, msg: '✗ ' + (d.error || 'Failed') };
        }
      } catch (e) {
        this.haConfig.result = { ok: false, msg: '✗ Error' };
      } finally {
        this.haConfig.testing = false;
        setTimeout(() => this.haConfig.result = null, 5000);
      }
    },

    async refresh() {
      this.refreshing = true;
      try {
        const [health, gateways, statuses] = await Promise.all([
          fetchJSON('api/health'),
          fetchJSON('api/gateways'),
          fetchJSON('api/gateways/status/all'),
        ]);

        if (health && health.ok !== false) {
          this.health = health;
          this.checkAssetsFreshness(health);
          this.mqttConnected = health.mqtt_connected;
          this.env = health.env;
          this.version = health.version;
        }
        if (gateways && Array.isArray(gateways)) {
          this.gateways = gateways;
          this.gatewaysLoaded = true;
          // Auto-select first if nothing selected
          if (!this.selectedGateway && gateways.length > 0) {
            const stored = localStorage.getItem('fwh_active_gw');
            if (stored && gateways.some(g => g.short_id === stored)) {
              this.selectedGateway = stored;
            } else {
              this.selectedGateway = gateways[0].short_id;
              localStorage.setItem('fwh_active_gw', this.selectedGateway);
            }
          }
        }
        if (statuses && Array.isArray(statuses)) {
          this.gatewayStatus = statuses;
        }
        this.lastRefresh = Date.now();
      } finally {
        this.refreshing = false;
      }
    },

    async refreshGateway(shortId) {
      // Force an immediate out-of-cycle poll for one aGate, then refresh the card data.
      if (this.refreshingGw?.[shortId]) return; // debounce
      if (!this.refreshingGw) this.refreshingGw = {};
      this.refreshingGw[shortId] = true;
      try {
        await fetchJSON(`api/gateways/${shortId}/poll`, { method: 'POST' });
        // Re-fetch just the gateway list and statuses to update the card
        const [gateways, statuses] = await Promise.all([
          fetchJSON('api/gateways'),
          fetchJSON('api/gateways/status/all'),
        ]);
        if (gateways) this.gateways = gateways;
        if (statuses) this.gatewayStatus = statuses;
        this.lastRefresh = Date.now();
      } finally {
        this.refreshingGw[shortId] = false;
      }
    },

    // Getters
    get selectedGatewayObj() {
      if (!Array.isArray(this.gateways)) return null;
      return this.gateways.find(g => g.short_id === this.selectedGateway) || null;
    },
    get gatewaysBySite() {
      const groups = new Map();
      for (const gw of this.gateways) {
        const siteId = gw.site_id || 'unassigned';
        if (!groups.has(siteId)) {
          groups.set(siteId, { 
            site_id: siteId, 
            label: siteId === 'unassigned' ? 'Gateways' : `Site ID: ${siteId}`, 
            gateways: [] 
          });
        }
        groups.get(siteId).gateways.push(gw);
      }
      return Array.from(groups.values());
    },
    get availableSites() {
      const sites = new Set();
      if (Array.isArray(this.gateways)) {
        for (const gw of this.gateways) {
          sites.add(gw.site_id || 'unassigned');
        }
      }
      return Array.from(sites).sort();
    },
    get filteredGatewaysBySite() {
      const base = this.gatewaysBySite;
      if (this.dashboardSiteFilter === 'ALL') return base;
      return base.filter(siteGrp => siteGrp.site_id === this.dashboardSiteFilter);
    },
    get selectedGatewayStatus() {
      if (!Array.isArray(this.gatewayStatus)) return null;
      return this.gatewayStatus.find(s => s.short_id === this.selectedGateway) || null;
    },
    getStatusFor(short_id) {
      if (!Array.isArray(this.gatewayStatus)) return null;
      return this.gatewayStatus.find(s => s.short_id === short_id) || null;
    },
    // Returns the user-assigned cloud name for a gateway (blank/generic fallbacks → null)
    gatewayLabel(gw) {
      if (!gw) return null;
      const n = (gw.name || '').trim();
      const generic = ['agate', 'agate x', ''].includes(n.toLowerCase());
      return generic ? null : n;
    },
    // Returns display string: "{name}" or just "{short_id}" if no meaningful name.
    // short_id is omitted when a label exists — it's shown in the full serial row nearby.
    // Used in topbar dropdown, nav labels, all gateway references.
    gatewayName(gw) {
      if (!gw) return '—';
      const label = this.gatewayLabel(gw);
      return label || gw.short_id;
    },

    isTabSuppressed(tab) {
      // Persona gates take precedence — server-computed from persona matrix.
      // Missing key = permissive (show). Explicit false = hide.
      if (this.personaGates && Object.prototype.hasOwnProperty.call(this.personaGates, tab)
          && this.personaGates[tab] === false) {
        return true;
      }
      if (Array.isArray(this.enabledModules) && this.enabledModules.length > 0) {
        const lowerTab = tab.toLowerCase();
        const MAP = {
          weather: 'weather',
          solar_setup: 'solar',
          smart_dispatch: 'smart_dispatch',
          dynamic_pricing: 'dynamic_pricing',
          pricing: 'dynamic_pricing',
          automations: 'automations',
          metrics: 'metrics',
          apimetrics: 'metrics',
          security: 'security'
        };
        const moduleName = MAP[lowerTab];
        if (moduleName && !this.enabledModules.includes(moduleName)) {
          return true;
        }
      }
      if (!this.suppressedTabs) return false;
      return this.suppressedTabs.split(',').map(s => s.trim().toLowerCase()).includes(tab.toLowerCase());
    },

    // URL-slug aliases for legacy external bookmarks / HA REST sensors.
    // Old slug → canonical slug. Alias is applied at both URL read (initRouting)
    // and programmatic setTab() so the app has ONE internal tab name.
    // Add here rather than sprinkle activeTab === 'old' checks across templates.
    _slugAliases: {
      amber: 'smart_dispatch',  // BD-01 / #14 rename — external URLs keep working
    },
    _canonicalSlug(tab) {
      return (this._slugAliases && this._slugAliases[tab]) || tab;
    },
    // ── Per-tab module lazy loading (GH #4) ──────────────────────────────────
    // Every tab's JS used to be fetched, parsed and executed on EVERY page
    // load: 21 files, ~607 KB, regardless of which tab you opened. Opening
    // Health (2.7 KB of its own logic) also parsed Smart Dispatch (142 KB) and
    // Schedule (82 KB). On iOS Safari that parse cost is a large part of why
    // the standard pages exhaust memory while the eink pages do not (#4).
    //
    // Each module defines the x-data factory for exactly one tab, and the tab
    // body lives inside <template x-if="activeTab === '…'"> — so the element,
    // and therefore the factory call, does not exist until the tab is active.
    // Loading the script just before activation is sufficient and needs no
    // build step.
    // tab slug -> the module(s) that define its x-data factories.
    //
    // NOT 1:1. Some factories are shared across tabs, and a lazy loader that
    // assumed one-module-per-tab broke two of them:
    //   solar_setup   uses haEntityPicker(), defined in smart_dispatch.js
    //   smart_dispatch uses a factory from home_automation_tab.js
    // Both worked only because every module used to be loaded eagerly.
    //
    // Derived by extracting every `x-data="factory("` from each tab template
    // (following {% include %}) and mapping it to the file that defines it.
    // If you add a shared component, add its module here too — the tab sweep
    // in tests/ and the Playwright check will catch it if you forget.
    _tabModules: {
      control:         ['control_tab.js?v=8'],
      smart_circuits:  ['smart_circuits_tab.js'],
      battery:         ['battery_tab.js'],
      health:          ['health_tab.js?v=2'],
      schedule:        ['schedule_tab.js?v=11'],
      logs:            ['logs_tab.js?v=5'],
      terminal:        ['terminal_tab.js'],
      support:         ['support_tab.js'],
      gateways:        ['gateways_tab.js'],
      mqtt_admin:      ['mqtt_admin_tab.js?v=10'],
      ha_entities:     ['ha_entities_tab.js'],
      home_automation: ['home_automation_tab.js?v=4'],
      power:           ['power_tab.js'],
      apimetrics:      ['api_metrics_tab.js?v=17'],
      metrics:         ['metrics_tab.js'],
      pricing:         ['pricing_tab.js?v=7'],
      dynamic_pricing: ['dynamic_pricing_tab.js?v=10'],
      automations:     ['automations_tab.js?v=5'],
      weather:         ['weather_tab.js?v=4'],
      security:        ['security_tab.js?v=6'],
      smart_dispatch:  ['home_automation_tab.js?v=4', 'smart_dispatch.js?v=24'],
      solar_setup:     ['smart_dispatch.js?v=24'],
    },
    _scriptPromises: {},


    /** Load one script once. Never rejects — a failed module must not wedge
     *  navigation, so we resolve and let the tab render whatever it can. */
    _loadScript(src) {
      if (this._scriptPromises[src]) return this._scriptPromises[src];
      const p = new Promise((resolve) => {
        const el = document.createElement('script');
        // Relative, exactly as the removed static tags were — the page's
        // <base href="{{ base_path }}/"> resolves it, so HA Ingress prefixes
        // keep working without threading the prefix through here.
        el.src = `static/js/${src}`;
        el.onload = () => resolve();
        el.onerror = () => {
          console.error(`[tabs] failed to load ${src}`);
          delete this._scriptPromises[src];   // allow a retry on a later visit
          resolve();
        };
        document.head.appendChild(el);
      });
      this._scriptPromises[src] = p;
      return p;
    },

    /** Ensure every module a tab depends on is loaded. Tabs with no entry
     *  (dashboard, engine_diag) resolve immediately — their logic is inline. */
    ensureTabModule(tab) {
      const mods = this._tabModules[tab];
      if (!mods || !mods.length) return Promise.resolve();
      return Promise.all(mods.map((m) => this._loadScript(m)));
    },

    setTab(tab) {
      // Nav guard: if a tab component has registered a guard (e.g. Schedule with unsaved changes)
      // call it first — it can abort navigation and show a confirmation modal.
      if (typeof window._scheduleNavGuard === 'function') {
        const proceed = window._scheduleNavGuard(tab);
        if (!proceed) return;  // guard blocked navigation
      }
      if (typeof window._mixerNavGuard === 'function') {
        const proceed = window._mixerNavGuard(tab);
        if (!proceed) return;  // guard blocked navigation
      }
      const canon = this._canonicalSlug(tab);

      // On a phone the sidebar is an overlay. Choosing a tab and then still
      // looking at the sidebar is never what was wanted, and the only way back
      // was the hamburger — which the sidebar itself covers.
      if (this.isNarrow) this.sidebarCollapsed = true;

      // Update the URL immediately so the address bar tracks the click even if
      // the module takes a moment; activation waits for the module so the
      // x-data factory exists by the time Alpine instantiates the tab body.
      const url = new URL(window.location);
      url.searchParams.set('tab', canon);
      window.history.replaceState({}, '', url);

      this.ensureTabModule(canon).then(() => {
        this.activeTab = canon;
        if (canon === 'smart_dispatch') this.sdView = 'overview';
      });
    },
    selectGateway(id) { 
      this.selectedGateway = id;
      localStorage.setItem('fwh_active_gw', id);
      // Strategy is per-gateway, so the outgoing gateway's value is now stale and
      // must not be shown against the incoming one (GH #8). Clear to the neutral
      // loading state until this gateway's signal resolves.
      this.sdDispatchStrategy = null;
      this._updateHaImpairment();
      // Refresh signal to get gateway-specific shadow_mode
      fetchJSON(`api/automation/signal?short_id=${id}`).then(d => {
        if (d?.dispatch_strategy) this.sdDispatchStrategy = d.dispatch_strategy;
        if (d?.shadow_mode !== undefined) this.shadowModeActive = !!d.shadow_mode;
        this._updateHaImpairment();
      }).catch(() => {});
    },
    initRouting() {
      // Disable browser back button — push a duplicate entry so back stays in the app.
      window.history.pushState(null, '', window.location.href);
      window.addEventListener('popstate', () => {
        window.history.pushState(null, '', window.location.href);
      });

      // Deep links bypass setTab(), so they must load the module themselves —
      // otherwise landing directly on ?tab=schedule renders a body whose
      // x-data factory does not exist yet.
      const activate = (slug) => {
        const canon = this._canonicalSlug(slug);
        this.ensureTabModule(canon).then(() => { this.activeTab = canon; });
      };

      // 1. Try ?tab=
      const params = new URLSearchParams(window.location.search);
      if (params.has('tab')) {
        activate(params.get('tab'));
        return;
      }
      // 2. Try #hash
      const hash = window.location.hash.replace('#', '');
      if (hash) {
        activate(hash);
      }
    }
  });

  // Track the breakpoint so the sidebar overlay follows rotation.
  (() => {
    const mq = window.matchMedia('(max-width: 1023px)');
    const apply = (e) => {
      const store = Alpine.store('app');
      if (!store) return;
      store.isNarrow = e.matches;
      // Coming back to a wide screen should not leave the sidebar hidden.
      if (!e.matches) store.sidebarCollapsed = false;
    };
    mq.addEventListener ? mq.addEventListener('change', apply) : mq.addListener(apply);
  })();

  // Keep Alpine.store('app').docVisible in sync with the real page visibility
  // so any component ($store.app.docVisible) can render "paused" states
  // without wiring its own listener. Reactive.
  document.addEventListener('visibilitychange', () => {
    try { Alpine.store('app').docVisible = document.visibilityState === 'visible'; } catch (e) {}
  });

});

