/* Control Tab Alpine Component — v4
   Fix 1: has_solar from DB profile. Fix 2: full off-grid modal clone.
   Fix 3: raw JSON contrast. Fix 4: tou-raw API. Fix 5: enum mode labels.
   Loaded by admin.html before Alpine initialises. */
function controlTab() {
  return {
    busy: false,
    fetchError: false,
    dispatchAction: null,
    // Seeded to a safe 2 kW and re-based once the gateway's real capability
    // arrives — see maxDispatchKw. Both this and the slider's ceiling used to
    // be literals, so a site with two aPower 2 units (20 kW of inverter) got a
    // slider that stopped at 5.
    dispatchPowerKw: 2.0,
    dispatchDurationMin: 60,
    dispatchChargeSoc: 95,
    dispatchStopSoc:  30,

    /** What the attached aPowers can actually deliver, summed.
     *
     * capacity.max_discharge_kw is resolved per unit from the device registry:
     * aPower X 5 kW, aPower 2 10 kW, aPower S 11.5 kW, and an aGate X 1.3 can
     * carry a mix of them. Falls back to 5 kW only when nothing identifies the
     * hardware — under-promising, because a slider offering power the
     * inverters cannot deliver is worse than one that stops short.
     */
    get maxDispatchKw() {
      const cap = this.$store.app.selectedGatewayObj?.capacity || {};
      const kw = this.dispatchAction === 'Charge'
        ? cap.max_charge_kw
        : cap.max_discharge_kw;
      return Number(kw) > 0 ? Number(kw) : 5.0;
    },

    get dispatchPowerPct() {
      const max = this.maxDispatchKw;
      return max > 0 ? Math.round((this.dispatchPowerKw / max) * 100) : 0;
    },

    /** Keep the chosen power inside what the hardware can do — the ceiling
     *  changes when the gateway selection or the direction changes. */
    clampDispatchPower() {
      const max = this.maxDispatchKw;
      if (this.dispatchPowerKw > max) this.dispatchPowerKw = max;
      if (this.dispatchPowerKw < 0.5) this.dispatchPowerKw = 0.5;
    },

    get solarKw()   { return this.$store.app.selectedGatewayObj?.solar_kw   ?? 0; },
    get homeKw()    { return this.$store.app.selectedGatewayObj?.home_kw    ?? 0; },
    get batteryKw() { return this.$store.app.selectedGatewayObj?.battery_kw ?? 0; },
    get gridKw()    { return this.$store.app.selectedGatewayObj?.grid_kw    ?? 0; },

    // Quick Access computed labels
    get gridImportLabel() {
      const gwObj = this.$store.app.selectedGatewayObj;
      if (!gwObj) return '—';
      if (gwObj.grid_import_unlimited) return 'Unlimited';
      const lim = gwObj.grid_import_limit;
      if (lim === 0 || lim === '0') return 'Blocked';
      return lim ? `${lim} kW` : 'Unlimited';
    },
    get isOffGridActive() {
      // True when the gateway is currently in simulated off-grid mode
      const gs = this.$store.app.selectedGatewayObj?.grid_connection_state
             ?? this.$store.app.selectedGatewayObj?.grid_status;
      return gs === 'SimulatedOffGrid';
    },

    currentModeInfo: null,
    reservesInfo: [],
    batteryStatus: null,
    activeTOU: null,

    get telemetryMode() {
      return this.$store.app.selectedGatewayObj?.operating_mode_id;
    },
    get isModeMismatch() {
      const configMode = this.currentModeInfo?.workMode;
      const teleMode = this.telemetryMode;
      // Mismatch is only valid if both are known and different.
      // We also ignore mismatch if it is only a case/hyphenation difference in strings, 
      // but here we compare IDs (integers).
      return teleMode != null && configMode != null && teleMode !== configMode;
    },

    // ── Mode Confirmation Modal ──────────────────────────────────────
    pendingMode: null,
    modalSoc: 20,
    backupForever: true,
    backupDays: 0,
    backupHours: 2,
    backupMinutes: 0,
    backupNextMode: 2,
    showEmergencyBackupConfirm: false,
    showBackupHistory: false,

    // ── Storm Hedge Modal ────────────────────────────────────────────
    showStormHedgeModal: false,
    stormEn: false,
    stormAdvanceTimeH: 2.0,
    stormDecisionStrategy: 1,
    stormEventVOList: [],

    // ── Grid Import/Export Modal ─────────────────────────────────────
    showPcsControlModal: false,
    pcsImportMode: 'custom',
    pcsImportMaxKw: '5000',
    pcsExportMode: 'unlimited',
    pcsExportMaxKw: '',

    // ── Reserve SOC Modal (Change 2) ─────────────────────────────────
    showReserveSocModal: false,
    editSoc:    20,    // Self-Consumption reserve
    editTouSoc: 15,    // TOU reserve
    socSaving: false,
    socSaveResult: null,  // 'ok' | 'error' | null

    // ── SOC Limits (inline — always visible) ─────────────────────────
    backupHistory: [],
    editMinDischarge:  30,
    editMaxCharge:     95,
    socLimitsSaving:   false,
    socLimitsSaveResult: null,
    // Battery Status expandable sub-section (collapsed by default to align columns)
    showSocControls: false,

    // ── Mode Info Diagnostics Modal ──────────────────────────────────
    showModeInfoModal: false,
    modeInfoLoading: false,
    modeInfoRaw: null,         // full raw result from /mode/tou-raw
    touModeList: [],           // result.list[] parsed from tou-raw
    modeInfoCurrentMode: null, // from /mode/current
    modeInfoRawExpanded: false,

    // ── Go Off-Grid Modal (full clone from power.html) ────────────────
    showOffGridModal: false,
    pendingGridState: 'Connected',   // GridConnectionState enum string
    reconnectSoc: 5,                 // Auto Re-connection SOC threshold
    ackOffGridRisk: false,           // Acknowledgement checkbox
    offGridConfirming: false,        // spinner while API call in-flight
    gridPending: false,              // prevents re-click while aGate processes
    gridCommandPendingSince: null,

    // ── Dispatch countdown + Live refresh ────────────────────────────
    dispatchLiveStatus: {},
    dispatchPollHandle: null,
    pfRefreshing: false,
    smartDispatchConfig: null,
    showSdOverrideModal: false,

    MODES: [
      { id: 1, name: 'Time-of-Use',       desc: 'Optimise costs using peak/off-peak schedules',  icon: 'fa-clock',          sym: '⏰' },
      { id: 2, name: 'Self-Consumption',  desc: 'Maximise use of solar energy at home',           icon: 'fa-leaf',           sym: '🌿' },
      { id: 3, name: 'Emergency Backup',  desc: 'Maximum battery retention for outages',          icon: 'fa-shield-halved',  sym: '🛡' },
    ],

    get availableModes() {
      if (!this.reservesInfo || this.reservesInfo.length === 0) return this.MODES;
      const validIds = this.reservesInfo.map(r => r.workMode);
      return this.MODES.filter(m => validIds.includes(m.id));
    },

    get batteryPowerStatus() {
      const kw = this.batteryStatus?.batteryKw ?? 0;
      if (kw < -0.1) return 'Charging';
      if (kw >  0.1) return 'Discharging';
      return 'Standby';
    },

    get capacityLabel() {
      const cap = this.$store.app.selectedGatewayObj?.capacity;
      if (!cap) return '— / — kWh';
      const avail = cap.available != null ? parseFloat(cap.available).toFixed(1) : '—';
      const total = cap.total    != null ? parseFloat(cap.total).toFixed(1)    : '—';
      return `${avail} / ${total} kWh`;
    },

    modeColour(id) {
      if (id === 1) return { bg: 'rgba(168,85,247,.18)',  text: '#c084fc', border: 'rgba(168,85,247,.35)' };
      if (id === 2) return { bg: 'rgba(34,197,94,.15)',   text: '#4ade80', border: 'rgba(34,197,94,.35)'  };
                    return { bg: 'rgba(59,130,246,.15)',  text: '#60a5fa', border: 'rgba(59,130,246,.35)' };
    },

    modeEnumLabel(id) {
      const m = this.MODES.find(x => x.id === parseInt(id));
      return m ? m.name : 'Unknown';
    },

    getReserveForMode(workModeId) {
      if (!this.reservesInfo || !this.reservesInfo.length) return workModeId === 3 ? 100 : 20;
      const e = this.reservesInfo.find(r => r.workMode === workModeId);
      return e ? e.soc : (workModeId === 3 ? 100 : 20);
    },

    formatDuration(min) {
      if (!min || min < 1) return '—';
      if (min < 60) return `${min} min`;
      const h = Math.floor(min / 60);
      const m = min % 60;
      return m === 0 ? `${h}h` : `${h}h ${m}m`;
    },

    formatRemaining(s) {
      if (!s || s <= 0) return '—';
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = Math.floor(s % 60);
      if (h > 0) return `${h}h ${String(m).padStart(2,'0')}m`;
      if (m > 0) return `${m}m ${String(sec).padStart(2,'0')}s`;
      return `${sec}s`;
    },

    selectMode(modeDef) {
      if (this.busy) return;
      this.pendingMode = modeDef;
      this.modalSoc = parseInt(this.getReserveForMode(modeDef.id), 10);
    },

    // ── Modal openers ────────────────────────────────────────────────
    openStormHedgeModal()  { this.showStormHedgeModal  = true; },

    async openPcsControlModal() {
      this.showPcsControlModal = true;
      const gwObj = this.$store.app.selectedGatewayObj;
      if (!gwObj) return;
      const imp = gwObj.grid_import_limit;
      const exp = gwObj.grid_export_limit;
      const impUnlimited = gwObj.grid_import_unlimited ?? gwObj.control?._pcs_grid_import_unlimited;
      const expUnlimited = gwObj.grid_export_unlimited ?? gwObj.control?._pcs_grid_export_unlimited;
      if (impUnlimited || imp === -1 || imp == null) { this.pcsImportMode = 'unlimited'; }
      else if (imp === 0 || imp === '0')             { this.pcsImportMode = 'disabled';  }
      else { this.pcsImportMode = 'custom'; this.pcsImportMaxKw = String(imp); }
      if (expUnlimited || exp === -1 || exp == null) { this.pcsExportMode = 'unlimited'; }
      else if (exp === 0 || exp === '0')             { this.pcsExportMode = 'disabled';  }
      else { this.pcsExportMode = 'custom'; this.pcsExportMaxKw = String(exp); }
    },

    // Change 2: Reserve SOC modal
    openReserveSocModal() {
      this.editSoc    = this.batteryStatus?.selfReserve ?? 20;
      this.editTouSoc = this.batteryStatus?.touReserve  ?? 15;
      this.socSaveResult = null;
      this.showReserveSocModal = true;
    },

    // Fix 4: Mode Info diagnostics modal — fetches full raw TOU API response
    async openModeInfoModal() {
      this.showModeInfoModal   = true;
      this.modeInfoLoading     = true;
      this.modeInfoRawExpanded = false;
      this.modeInfoRaw         = null;
      this.touModeList         = [];
      const gw = this.$store.app.selectedGateway;
      try {
        const [rawR, curR] = await Promise.all([
          fetch(`api/gateways/${gw}/mode/tou-raw`),
          fetch(`api/gateways/${gw}/mode/current`),
        ]);
        if (rawR.ok) {
          const d = await rawR.json();
          if (d.ok && d.result) {
            this.modeInfoRaw  = d.result;
            // API shape: {result: {list: [...], currendId: N, ...}}
            const inner = d.result?.result ?? d.result;
            this.touModeList = inner?.list ?? [];
          }
        }
        if (curR.ok) { const d = await curR.json(); if (d.ok) this.modeInfoCurrentMode = d.mode; }
      } catch (e) { console.warn('Mode info fetch error:', e); }
      finally { this.modeInfoLoading = false; }
    },

    // Fix 5: enum label for a workMode integer (never uses API tariff name as primary label)
    modeEnumLabel(workMode) {
      if (workMode === 1) return 'Time-of-Use';
      if (workMode === 2) return 'Self-Consumption';
      if (workMode === 3) return 'Emergency Backup';
      return `Mode ${workMode}`;
    },

    // Fix 2: Go Off-Grid modal — full clone from power.html
    async openOffGridModal() {
      const gwObj = this.$store.app.selectedGatewayObj;
      // GridConnectionState enum: 'Connected' | 'SimulatedOffGrid' | 'Outage' | 'NotGridTied'
      this.pendingGridState = gwObj?.grid_connection_state ?? gwObj?.grid_status ?? 'Connected';
      this.reconnectSoc     = gwObj?.offgrid_reserve_soc ?? 5;
      this.ackOffGridRisk   = false;
      this.offGridConfirming = false;
      this.showOffGridModal  = true;
    },

    async executeOffGrid() {
      this.offGridConfirming = true;
      const gw = this.$store.app.selectedGateway;
      const goingOffGrid = (this.pendingGridState === 'Connected');
      const payload = { offgrid: goingOffGrid, soc: this.reconnectSoc };
      try {
        const r = await fetch(`api/gateways/${gw}/profile/islanding`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const res = await r.json().catch(() => ({}));
        if (res?.ok) {
          const action = goingOffGrid ? 'Off-Grid' : 'On-Grid';
          this.$store.app.addToast(`${action} command queued — allow a few seconds for relays to actuate.`, 'success');
          this.showOffGridModal  = false;
          this.ackOffGridRisk    = false;
          this.gridPending       = true;
          this.gridCommandPendingSince = Date.now();
          setTimeout(() => this.$store.app.refresh(), 2000);
        } else {
          throw new Error(res?.error || res?.detail || `HTTP ${r.status}`);
        }
      } catch (err) {
        this.$store.app.addToast('Grid islanding command failed: ' + err.message, 'error');
      } finally { this.offGridConfirming = false; }
    },

    // ── Lifecycle ────────────────────────────────────────────────────
    async init() {
      this.$watch('$store.app.selectedGateway', (val) => {
        if (val) {
          this.refreshData();
          this.startDispatchPoll(); // refresh dispatch state on gateway switch
        }
      });
      if (this.$store.app.selectedGateway) {
        this.refreshData();
        this.startDispatchPoll(); // fetch active dispatch status on page load
      }
    },

    // ── Dispatch polling ─────────────────────────────────────────────
    startDispatchPoll() {
      this.stopDispatchPoll();
      const gw = this.$store.app.selectedGateway;
      if (!gw) return;
      // Grace window before an inactive reading is allowed to cancel polling
      // (GH #6). A freshly-issued dispatch is not reported active until the
      // command reaches the gateway and comes back through the cloud, which
      // takes longer than the 3s tick. Without this, the first tick sees
      // active=false, kills the interval, and the card never updates again.
      const startedAt = Date.now();
      const GRACE_MS = 15000;
      const poll = async () => {
        try {
          const r = await fetch(`api/gateways/${gw}/dispatch/status`);
          if (r.ok) {
            const d = await r.json();
            this.dispatchLiveStatus = d;
            const settled = (Date.now() - startedAt) > GRACE_MS;
            if (!d.active && settled && this.dispatchPollHandle) {
              clearInterval(this.dispatchPollHandle);
              this.dispatchPollHandle = null;
            }
          }
        } catch (e) { /* silent */ }
      };
      poll();
      this.dispatchPollHandle = setInterval(poll, 3000);
    },

    stopDispatchPoll() {
      if (this.dispatchPollHandle) {
        clearInterval(this.dispatchPollHandle);
        this.dispatchPollHandle = null;
      }
      this.dispatchLiveStatus = {};
    },

    // Re-read telemetry repeatedly after a dispatch command until the change
    // shows up (GH #6). Operating mode and battery flow travel
    // gateway -> FranklinWH cloud -> FHAI, which routinely takes longer than
    // the single 1s refresh this used to do. One shot repaints the PRE-command
    // state and nothing ever corrects it, so the Control tab and top nav sit on
    // "Self-Consumption / Discharging" while the FWH app already shows the new
    // state. Backoff totals ~15s, which covers observed propagation, and stops
    // early once telemetry actually moves.
    async _settleAfterDispatch() {
      // workMode is the key set from api/gateways/{gw}/mode/current (see the
      // comparisons in executeMode); batteryKw catches charge/discharge flips
      // even when the mode itself is unchanged.
      const sample = () => JSON.stringify([
        this.currentModeInfo?.workMode ?? null,
        this.batteryStatus?.batteryKw ?? null,
        this.dispatchLiveStatus?.active ?? null,
      ]);
      const before = sample();
      for (const ms of [1000, 2000, 4000, 8000]) {
        await new Promise(res => setTimeout(res, ms));
        await this.refreshData();
        if (sample() !== before) return;   // telemetry moved — done
      }
    },

    async refreshPowerFlow() {
      if (!this.$store.app.selectedGateway || this.pfRefreshing) return;
      this.pfRefreshing = true;
      try { await this.$store.app.refreshGateway(this.$store.app.selectedGateway); } catch (e) { /* silent */ }
      finally { setTimeout(() => { this.pfRefreshing = false; }, 600); }
    },

    async refreshData() {
      if (!this.$store.app.selectedGateway) return;
      const gw = this.$store.app.selectedGateway;
      this.fetchError = false;
      try {
        // First, force an immediate out-of-cycle API poll so the gateway telemetry updates
        await this.$store.app.refreshGateway(gw);

        const [modeR, resR, sdR] = await Promise.all([
          fetch(`api/gateways/${gw}/mode/current`),
          fetch(`api/gateways/${gw}/mode/reserves`),
          fetch(`api/smart_dispatch/config?gateway_id=${gw}`)
        ]);
        if (modeR.ok) { const d = await modeR.json(); if (d.ok) this.currentModeInfo = d.mode; }
        if (resR.ok)  { const d = await resR.json();  if (d.ok && d.reserves) this.reservesInfo = d.reserves; }
        if (sdR.ok)   { const d = await sdR.json();   if (d.ok) this.smartDispatchConfig = d.config; }

        let gwObj = this.$store.app.selectedGatewayObj;
        // If store hasn't loaded yet (init race) OR the snapshot is
        // zero-collapsed, fetch live data directly.
        //
        // `== null` alone was not enough. The store transiently reports
        // battery_soc=0 with an empty capacity block (a zero-collapse — the
        // same phenomenon _normalise_stats guards against server-side), and 0
        // is not null, so the re-fetch was skipped and the zero got cached
        // into batteryStatus. Since refreshData() runs only on page init and
        // gateway switch — there is no timer — the Control tab then showed
        // 0% / 0 batteries / 0 kWh permanently, while the top nav (bound
        // straight to the store) self-corrected seconds later. Observed live:
        // store 44.3% -> 0 at t+2s -> tab cached 0 at t+3s -> store recovered
        // at t+16s -> tab still 0 at t+30s.
        //
        // battery_count is the honest discriminator: a genuinely flat battery
        // reports soc=0 with count>=1, whereas a collapsed payload reports no
        // batteries at all. So 0% on a real system is still respected.
        const _collapsed = gwObj
          && (gwObj.battery_soc == null || !(gwObj.capacity && gwObj.capacity.battery_count));
        if (_collapsed) {
          try {
            const liveR = await fetch(`api/gateways/${gw}/data`);
            if (liveR.ok) { const live = await liveR.json(); gwObj = { ...gwObj, ...live }; }
          } catch (e) { /* silent — gwObj from store still usable */ }
        }
        if (gwObj) {
          const cap = gwObj.capacity ?? {};
          this.batteryStatus = {
            soc:         gwObj.battery_soc ?? null,
            selfReserve: this.getReserveForMode(2),
            touReserve:  this.getReserveForMode(1),
            minDischarge: gwObj.min_discharge_soc ?? 30,
            maxCharge:   gwObj.max_charge_soc ?? 95,
            batteryKw:   gwObj.battery_kw ?? 0,
            capacity:    cap,
            currentKwh:  cap.available != null ? parseFloat(cap.available) : null,
            maxKwh:      cap.total     != null ? parseFloat(cap.total)     : null,
          };
          this.dispatchChargeSoc = this.batteryStatus.maxCharge;
          this.dispatchStopSoc   = this.batteryStatus.minDischarge;
          this.editMinDischarge  = this.batteryStatus.minDischarge;
          this.editMaxCharge     = this.batteryStatus.maxCharge;
          if (gwObj.tou_active) this.activeTOU = gwObj.tou_active;
        }
      } catch (err) { console.error('Control fetch error:', err); this.fetchError = true; }
    },


    async executeMode() {
      if (!this.pendingMode || !this.$store.app.selectedGateway) return;
      this.busy = true;
      const gw    = this.$store.app.selectedGateway;
      const modeId = this.pendingMode.id;
      const soc   = parseInt(this.modalSoc, 10);
      try {
        let payload = { work_mode: modeId, soc };
        if (this.pendingMode.name === 'Emergency Backup') {
          payload.soc      = null;
          payload.forever  = this.backupForever ? 1 : 2;
          payload.next_mode = this.backupForever ? null : this.backupNextMode;
          payload.duration = this.backupForever ? null : (this.backupDays*1440)+(this.backupHours*60)+this.backupMinutes;
        }
        const url    = (this.currentModeInfo?.workMode === modeId && modeId !== 3)
          ? `api/gateways/${gw}/mode/reserve` : `api/gateways/${gw}/mode/set`;
        const method = (this.currentModeInfo?.workMode === modeId && modeId !== 3) ? 'PATCH' : 'POST';
        const resp = await fetch(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${resp.status}`); }
        this.$store.app.addToast('Mode successfully updated', 'info');
        // Same propagation race as sendDispatch (GH #6): a single delayed
        // refresh often lands before the new mode has travelled back through
        // the cloud, leaving the top nav on the old mode after a "successfully
        // updated" toast.
        this._settleAfterDispatch().catch(() => {});
      } catch (err) {
        this.$store.app.addToast('Failed to execute mode change: ' + err.message, 'error');
      } finally { this.busy = false; this.pendingMode = null; }
    },

    // Change 2: saves both reserve SOCs; closes modal on success
    async saveReserveSocs() {
      if (!this.$store.app.selectedGateway) return;
      this.socSaving = true;
      const gw = this.$store.app.selectedGateway;
      const patch = (work_mode, soc) => fetch(`api/gateways/${gw}/mode/reserve`, {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ work_mode, soc: parseInt(soc, 10) }),
      });
      try {
        const [r1, r2] = await Promise.all([
          patch(2, this.editSoc),
          patch(1, this.editTouSoc),
        ]);
        if (!r1.ok || !r2.ok) throw new Error('Server rejected one or both reserve SOC values');
        this.socSaveResult = 'ok';
        if (this.batteryStatus) {
          this.batteryStatus.selfReserve = this.editSoc;
          this.batteryStatus.touReserve  = this.editTouSoc;
        }
        setTimeout(() => {
          this.socSaveResult = null;
          this.showReserveSocModal = false;
        }, 1200);
        setTimeout(() => this.refreshData(), 1500);
      } catch (err) {
        this.socSaveResult = 'error';
        this.$store.app.addToast('Failed to save reserves: ' + err.message, 'error');
        setTimeout(() => { this.socSaveResult = null; }, 4000);
      }
      finally { this.socSaving = false; }
    },

    async saveSocLimits() {
      if (!this.$store.app.selectedGateway) return;
      this.socLimitsSaving = true;
      const gw = this.$store.app.selectedGateway;
      const post = (cmd, val) => fetch(`api/gateways/${gw}/control`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ command: cmd, value: String(val) }),
      });
      try {
        const [r1, r2] = await Promise.all([
          post('min_discharge_soc', this.editMinDischarge),
          post('max_charge_soc',    this.editMaxCharge),
        ]);
        if (!r1.ok || !r2.ok) throw new Error('Server rejected SOC limits');
        this.socLimitsSaveResult = 'ok';
        if (this.batteryStatus) {
          this.batteryStatus.minDischarge = this.editMinDischarge;
          this.batteryStatus.maxCharge    = this.editMaxCharge;
        }
        setTimeout(() => { this.socLimitsSaveResult = null; }, 3000);
        setTimeout(() => this.refreshData(), 800);
      } catch (err) {
        this.socLimitsSaveResult = 'error';
        this.$store.app.addToast('Failed to save SOC limits: ' + err.message, 'error');
        setTimeout(() => { this.socLimitsSaveResult = null; }, 4000);
      }
      finally { this.socLimitsSaving = false; }
    },

    async sendDispatch(force = false) {
      if (!this.$store.app.selectedGateway) return;

      // System Orchestrator Guard: Check for active Smart Dispatch
      const strategy = this.smartDispatchConfig?.strategy_mode || 'disabled';
      const isSdActive = ['auto', 'passive', 'active', 'proactive'].includes(strategy);
      
      if (isSdActive && this.dispatchAction !== 'Stop' && !force) {
        this.showSdOverrideModal = true;
        return;
      }
      this.showSdOverrideModal = false;

      this.busy = true;
      const gw = this.$store.app.selectedGateway;
      const post = (cmd, val) => fetch(`api/gateways/${gw}/control${force ? '?force=true' : ''}`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ command: cmd, value: String(val) }),
      });
      try {
        if (this.dispatchAction !== 'Stop') {
          await post('dispatch_power',    this.dispatchPowerKw);
          await post('dispatch_duration', this.dispatchDurationMin);
          if (this.dispatchAction === 'Charge')
            await post('dispatch_target_soc', this.dispatchChargeSoc);
          if (this.dispatchAction === 'Discharge')
            await post('dispatch_stop_soc', this.dispatchStopSoc);
        }
        const r = await post('dispatch_action', this.dispatchAction);
        if (!r.ok) { 
          const e = await r.json().catch(() => ({})); 
          this.$store.app.addToast('Dispatch failed: '+(e.detail||'Server error'), 'error'); 
        }
        else if (this.dispatchAction === 'Stop') {
          this.dispatchAction = null;
          this.stopDispatchPoll();
          // Stop had no refresh at all (GH #6) — the card and top nav kept the
          // pre-Stop mode and flow until some unrelated poll happened to fire.
          // Releasing control needs the same settle treatment as issuing it.
          this._settleAfterDispatch().catch(() => {});
        } else {
          this.startDispatchPoll();
          this._settleAfterDispatch().catch(() => {});
        }
      } catch { this.$store.app.addToast('Network error executing dispatch.', 'error'); }
      finally  { this.busy = false; }
    },

    async saveStormHedge() {
      this.busy = true;
      const gw = this.$store.app.selectedGateway;
      try {
        const payload = { stormEn: this.stormEn?1:0, advanceTime: parseFloat(this.stormAdvanceTimeH), setAdvanceBackupTime: this.stormDecisionStrategy };
        const res = await fetch(`api/gateways/${gw}/control`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ slug:'storm_hedge_config', value: JSON.stringify(payload) }),
        });
        if (!res.ok) throw new Error(await res.text());
        this.$store.app.addToast('Storm Hedge settings saved', 'ok');
        this.showStormHedgeModal = false;
      } catch (err) { this.$store.app.addToast('Failed to save Storm Hedge: '+err.message, 'error'); }
      finally { this.busy = false; }
    },

    async savePcsControl() {
      this.busy = true;
      const gw = this.$store.app.selectedGateway;
      const post = (cmd, val) => fetch(`api/gateways/${gw}/control`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ command: cmd, value: String(val) }),
      });
      try {
        let impVal = -1;
        if (this.pcsImportMode==='disabled') impVal = 0;
        else if (this.pcsImportMode==='custom') impVal = (this.pcsImportMaxKw===''||this.pcsImportMaxKw==null)?-1:parseFloat(this.pcsImportMaxKw);
        let expVal = -1;
        if (this.pcsExportMode==='disabled') expVal = 0;
        else if (this.pcsExportMode==='custom') expVal = (this.pcsExportMaxKw===''||this.pcsExportMaxKw==null)?-1:parseFloat(this.pcsExportMaxKw);
        const impSlug = impVal>0?'grid_import_limit':'grid_import_unlimited';
        const impSend = impVal>0?impVal:(impVal===-1?'true':'false');
        const expSlug = expVal>0?'grid_export_limit':'grid_export_unlimited';
        const expSend = expVal>0?expVal:(expVal===-1?'true':'false');
        const [ir,er] = await Promise.all([post(impSlug,impSend), post(expSlug,expSend)]);
        if (!ir.ok||!er.ok) throw new Error('One or more grid control commands failed');
        this.$store.app.addToast('Grid settings updated', 'ok');
        await this.$store.app.refresh();
        this.showPcsControlModal = false;
      } catch (err) { this.$store.app.addToast('Failed: '+err.message, 'error'); }
      finally { this.busy = false; }
    },
  };
}
