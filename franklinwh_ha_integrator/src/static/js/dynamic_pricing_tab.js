// dynamic_pricing_tab.js — Alpine component for Dynamic Pricing tab
function dynamicPricingTab() {
  return {
    loading: false,
    error: null,
    pricingData: null,
    pricingView: 'import',
    expandedGroups: [],
    dynamicPricingView: 'overview',

    showPcsControlModal: false,
    pcsImportMode: 'unlimited',
    pcsImportMaxKw: '',
    pcsExportMode: 'unlimited',
    pcsExportMaxKw: '',
    savingPcs: false,

    showOffGridModal: false,
    ackOffGridRisk: false,
    pendingTargetState: null,
    pendingIsGridConnected: false,

    services: [],
    activeServiceId: null,
    selectedServiceId: null,
    pricingModels: [],
    expandedModelSetup: null,
    setupSaving: {},
    setupTesting: {},
    setupTestResult: {},
    setupStatusMsg: {},
    setupActivating: {},

    init() {
      this.$watch('showPcsControlModal', val => {
        if (val) {
          const gwObj = this.$store.app.selectedGatewayObj;
          if (!gwObj) return;
          const imp = gwObj.grid_import_limit;
          const exp = gwObj.grid_export_limit;
          if (gwObj.grid_import_unlimited === true || imp === -1 || imp == null) this.pcsImportMode = 'unlimited';
          else if (imp === 0 || imp === '0') this.pcsImportMode = 'disabled';
          else { this.pcsImportMode = 'custom'; this.pcsImportMaxKw = String(imp); }

          if (gwObj.grid_export_unlimited === true || exp === -1 || exp == null) this.pcsExportMode = 'unlimited';
          else if (exp === 0 || exp === '0') this.pcsExportMode = 'disabled';
          else { this.pcsExportMode = 'custom'; this.pcsExportMaxKw = String(exp); }
        }
      });
      this.$watch('$store.app.selectedGateway', async () => {
        this.selectedServiceId = null;
        await this.refresh();
      });
      this.loadServices();
      this.loadPricingModels();
      this.refresh();
      // Batch R.1 (2026-08-02): timer id captured for destroy() clearance
      // under R.2 x-if lazy mount. document.hidden guard is retained.
      this._refresh300Timer = setInterval(() => { if (!document.hidden) this.refresh(); }, 300000);
    },

    async loadServices() {
      try {
        const res = await fetchJSON('api/utility-services');
        if (res?.ok) {
          this.services = res.data || [];
        }
      } catch (e) {
        console.warn('Failed to load utility services:', e);
      }
    },

    async loadPricingModels() {
      try {
        const res = await fetchJSON('api/pricing/models');
        if (res?.ok && res.models) {
          this.pricingModels = res.models;
          // Pre-initialize per-model setup state to falsy primitives so
          // Alpine's :disabled binding evaluates cleanly. Without this,
          // setupSaving[model.id] is `undefined` for any model the user
          // hasn't interacted with yet — and Alpine's :disabled treats
          // undefined as truthy in some contexts, leaving the Save Config
          // / Test Connection buttons stuck disabled on first page load.
          // Fixed 2026-06-17 after user reported "buttons don't work even
          // after hard refresh" — root cause was this reactive-undefined
          // gotcha, not a missing handler or backend issue.
          for (const m of res.models) {
            if (this.setupSaving[m.id]     === undefined) this.setupSaving[m.id]     = false;
            if (this.setupTesting[m.id]    === undefined) this.setupTesting[m.id]    = false;
            if (this.setupStatusMsg[m.id]  === undefined) this.setupStatusMsg[m.id]  = '';
            if (this.setupTestResult[m.id] === undefined) this.setupTestResult[m.id] = null;
            if (this.setupActivating[m.id] === undefined) this.setupActivating[m.id] = false;
          }
        }
      } catch (e) {
        console.warn('Failed to load pricing models:', e);
      }
    },

    toggleModelSetup(modelId) {
      this.expandedModelSetup = this.expandedModelSetup === modelId ? null : modelId;
    },

    async saveModelSetup(model) {
      if (!model || !model.id) {
        // Defensive: if a future template change passes an unset model, we
        // want a console trace rather than silently no-oping (the original
        // "user clicks button, nothing happens" defect class).
        console.warn('[saveModelSetup] called with no model.id — aborting', model);
        return;
      }
      this.setupSaving[model.id] = true;
      this.setupStatusMsg[model.id] = 'Saving...';
      try {
        const res = await fetchJSON(`api/pricing/models/${model.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            credentials: model.credentials || {},
            settings: model.settings || {}
          })
        });
        if (res?.ok) {
          this.setupStatusMsg[model.id] = 'Saved successfully';
          Alpine.store('app').addToast(`${model.name} configuration saved.`, 'success');
          await this.loadPricingModels();
        } else {
          this.setupStatusMsg[model.id] = 'Save failed';
          Alpine.store('app').addToast(res?.error || 'Save failed.', 'error');
        }
      } catch (e) {
        this.setupStatusMsg[model.id] = 'Error saving';
        Alpine.store('app').addToast('Error: ' + e.message, 'error');
      } finally {
        this.setupSaving[model.id] = false;
        setTimeout(() => { this.setupStatusMsg[model.id] = ''; }, 4000);
      }
    },

    async testModelSetup(model) {
      if (!model || !model.id) {
        console.warn('[testModelSetup] called with no model.id — aborting', model);
        return;
      }
      this.setupTesting[model.id] = true;
      this.setupTestResult[model.id] = null;
      try {
        const res = await fetchJSON(`api/pricing/models/${model.id}/test`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            credentials: model.credentials || {},
            settings: model.settings || {}
          })
        });
        this.setupTestResult[model.id] = res?.data || { ok: false, message: res?.error || 'Test failed' };
      } catch (e) {
        this.setupTestResult[model.id] = { ok: false, message: 'Connection error: ' + e.message };
      } finally {
        this.setupTesting[model.id] = false;
      }
    },

    // ── Use For Gateway helpers ────────────────────────────────────────────
    currentGatewayId() {
      return this.$store?.app?.selectedGateway || null;
    },

    activeGatewayLabel() {
      const gw = this.$store?.app?.selectedGatewayObj;
      return gw?.label || gw?.name || this.currentGatewayId() || '';
    },

    isActiveModelForGateway(modelId) {
      // True iff the gateway's currently-linked utility service uses this pricing model.
      const gw = this.currentGatewayId();
      if (!gw || !this.services?.length || !this.activeServiceId) return false;
      const svc = this.services.find(s => s.id === this.activeServiceId);
      if (!svc) return false;
      const svcModel = svc.pricing_model_id || svc.pricing_provider || '';
      return svcModel === modelId;
    },

    async useModelForCurrentGateway(model) {
      if (!model?.id) return;
      const gw = this.currentGatewayId();
      if (!gw) {
        Alpine.store('app').addToast('No gateway selected.', 'error');
        return;
      }
      this.setupActivating[model.id] = true;
      try {
        const res = await fetchJSON(
          `api/pricing/models/${model.id}/use-for-gateway/${gw}`,
          { method: 'POST' }
        );
        if (res?.ok) {
          const msg = res.created
            ? `Created '${res.service_name}' and linked to ${gw}`
            : (res.no_change
                ? `${res.service_name} already uses ${model.name}`
                : `${res.service_name} now uses ${model.name}`);
          Alpine.store('app').addToast(msg, 'success');
          // Refresh services + active service id so the UI pill updates
          await this.loadServices();
          await this.refresh();
        } else {
          Alpine.store('app').addToast(res?.error || 'Activation failed.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast('Error: ' + e.message, 'error');
      } finally {
        this.setupActivating[model.id] = false;
      }
    },

    async selectService(id) {
      this.selectedServiceId = id;
      await this.refresh();
    },

    async activateServiceForGateway() {
      if (!this.selectedServiceId) return;
      const gw = this.$store.app.selectedGateway;
      if (!gw) return;

      this.loading = true;
      try {
        const res = await fetchJSON(`api/utility-services/${this.selectedServiceId}/gateways`, {
          method: 'PUT',
          body: JSON.stringify([gw])
        });
        if (res?.ok) {
          Alpine.store('app').addToast('Pricing model activated successfully.', 'success');
          this.activeServiceId = this.selectedServiceId;
          await this.loadServices(); // reload gateway links
          await this.refresh();
        } else {
          Alpine.store('app').addToast(res?.error || 'Failed to activate service.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast('Error activating service: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    async refresh() {
      if (this.loading) return;
      this.loading = true; this.error = null;
      try {
        let url = 'api/pricing/current';
        if (this.selectedServiceId) {
          url += `?utility_service_id=${this.selectedServiceId}`;
        }
        const r = await fetchJSON(url);
        if (!r?.ok) throw new Error(r?.error || 'Failed to load pricing dataset.');
        this.pricingData = r.data;
        if (this.pricingData && this.pricingData.forecast) {
          const forecasts = this.pricingData.forecast;
          const imports = forecasts.map(f => f.import_c_kwh).filter(v => v != null);
          const exports = forecasts.map(f => f.export_c_kwh).filter(v => v != null);
          this.pricingData.max_export = exports.length ? Math.max(...exports) : 0;
          this.pricingData.min_import = imports.length ? Math.min(...imports) : 0;
          this.pricingData.max_import = imports.length ? Math.max(...imports) : 0;

          // Auto-expand groups containing any interval with a SPIKE tariff type or active spike status
          const groups = this.getGroupedForecast();
          const autoExpanded = [];
          for (const g of groups) {
            const hasSpike = g.spike_status && g.spike_status.toLowerCase() !== 'none' ||
                             g.items.some(item => {
                               const itemSpike = (item.spike_status || '').toLowerCase();
                               const itemTariff = (item.tariff_type || '').toLowerCase();
                               return itemSpike === 'spike' || itemSpike === 'potential' || itemTariff === 'spike';
                             });
            if (hasSpike) {
              autoExpanded.push(g.key);
            }
          }
          this.expandedGroups = Array.from(new Set([...this.expandedGroups, ...autoExpanded]));
        }
        if (r.utility_service_id) {
          this.activeServiceId = r.utility_service_id;
          if (!this.selectedServiceId) {
            this.selectedServiceId = r.utility_service_id;
          }
        }
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    async toggleGrid() {
      const gwObj = this.$store.app.selectedGatewayObj;
      if (!gwObj) return;
      const isGridConnected = gwObj.grid_status === 'Connected';
      this.pendingIsGridConnected = isGridConnected;
      this.pendingTargetState = isGridConnected ? 'Off-Grid' : 'On-Grid';
      if (isGridConnected) { this.showOffGridModal = true; this.ackOffGridRisk = false; }
      else { if (!confirm('Reconnect to grid?')) return; await this.executeToggleGrid(); }
    },

    async executeToggleGrid() {
      const gwObj = this.$store.app.selectedGatewayObj;
      this.loading = true;
      const gw = this.$store.app.selectedGateway;
      const res = await fetchJSON(`api/gateways/${gw}/profile/islanding`, {
        method: 'POST',
        body: JSON.stringify({ offgrid: this.pendingIsGridConnected, soc: gwObj?.backup_reserve_soc || 20 }),
      });
      this.loading = false;
      this.showOffGridModal = false;
      if (res?.ok) Alpine.store('app').addToast(`${this.pendingTargetState} command queued.`, 'success');
      else Alpine.store('app').addToast(res?.error || 'Command failed', 'error');
    },

    async savePcsControl() {
      this.savingPcs = true;
      try {
        const gw = this.$store.app.selectedGateway;
        let importVal = -1;
        if (this.pcsImportMode === 'disabled') importVal = 0;
        else if (this.pcsImportMode === 'custom') importVal = parseFloat(this.pcsImportMaxKw) || -1;

        let exportVal = -1;
        if (this.pcsExportMode === 'disabled') exportVal = 0;
        else if (this.pcsExportMode === 'custom') exportVal = parseFloat(this.pcsExportMaxKw) || -1;

        const post = (command, value) => fetch(`api/gateways/${gw}/control`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command, value: String(value) }),
        });
        const impSlug = importVal > 0 ? 'grid_import_limit'     : 'grid_import_unlimited';
        const impVal  = importVal > 0 ? importVal                : (importVal === -1 ? 'true' : 'false');
        const expSlug = exportVal > 0 ? 'grid_export_limit'     : 'grid_export_unlimited';
        const expVal  = exportVal > 0 ? exportVal                : (exportVal === -1 ? 'true' : 'false');

        const [ir, er] = await Promise.all([post(impSlug, impVal), post(expSlug, expVal)]);
        if (!ir.ok || !er.ok) throw new Error('One or more grid control commands failed');
        this.showPcsControlModal = false;
        Alpine.store('app').addToast('Grid limits updated.', 'success');
      } catch (e) {
        Alpine.store('app').addToast('Failed: ' + e.message, 'error');
      } finally {
        this.savingPcs = false;
      }
    },

    formatTime(isoString) {
      if (!isoString) return '--:--';
      const d = new Date(isoString);
      return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
    },

    toggleGroup(key) {
      if (this.expandedGroups.includes(key)) this.expandedGroups = this.expandedGroups.filter(k => k !== key);
      else this.expandedGroups.push(key);
    },

    getGroupedForecast() {
      const rawForecast = this.pricingData?.forecast || [];
      if (!rawForecast?.length) return [];
      const groups = [];
      let current = null;
      for (const item of rawForecast) {
        const start_time = item.start;
        const mins = new Date(start_time).getMinutes();
        if (!current || mins === 0 || mins === 30) {
          if (current) groups.push(current);
          current = {
            key: start_time,
            start_time: start_time,
            items: [],
            sum_import: 0,
            sum_export: 0,
            sum_ren: 0,
            ren_count: 0,
            is_demand: false,
            spike_status: null
          };
        }
        current.items.push(item);
        current.sum_import += (item.import_c_kwh || 0);
        current.sum_export += (item.export_c_kwh || 0);
        if (item.renewables_pct != null) {
          current.sum_ren += item.renewables_pct;
          current.ren_count++;
        }
        if (item.demand_window) current.is_demand = true;
        if (item.spike_status && item.spike_status.toLowerCase() !== 'none') {
          current.spike_status = item.spike_status;
        }
      }
      if (current?.items.length) groups.push(current);
      return groups.map(g => {
        let durationMins = 30;
        if (g.items.length > 0) {
          const first = new Date(g.items[0].start).getTime();
          const last = new Date(g.items[g.items.length - 1].end || g.items[g.items.length - 1].start).getTime();
          const endVal = g.items[g.items.length - 1].end ? last : last + 30 * 60 * 1000;
          durationMins = Math.round((endVal - first) / 60000);
        }
        return {
          ...g,
          avg_import:      g.sum_import / g.items.length,
          avg_export:      g.sum_export / g.items.length,
          avg_renewables: g.ren_count > 0 ? g.sum_ren / g.ren_count : null,
          duration_mins:  durationMins
        };
      });
    },

    getCurrentPeriodLabel() {
      if (!this.pricingData?.current_period) return 'NO DATA';
      const now = new Date();
      const future = new Date(now.getTime() + (this.pricingData.interval_min || 30) * 60000);
      const fmt = d => String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
      return `${fmt(now)} - ${fmt(future)}`;
    },

    getPricingBgColor(price, isDemand, viewType) {
      if (isDemand) return '#9333ea';
      if (viewType === 'export') {
        if (price < 0) return '#ef4444'; // Negative feed-in is a penalty (red)
        return '#facc15'; // Positive feed-in is credit (yellow)
      }
      if (price <= 0) return '#22c55e';
      if (price < 15) return '#f59e0b';
      if (price < 30) return '#f97316';
      return '#ef4444';
    },

    getRecommendation(impVal, expVal, spikeStatus, descriptor) {
      if (!this.pricingData) return 'HOLD';
      
      const maxExport = this.pricingData.max_export || 0;
      const minImport = this.pricingData.min_import || 0;
      const maxImport = this.pricingData.max_import || 0;
      
      const sStatus = (spikeStatus || '').toLowerCase();
      const desc = (descriptor || '').toLowerCase();

      // EXPORT recommendation:
      // If export price is high (credit) and positive
      if (expVal != null && expVal > 0) {
        const isMaxExport = expVal >= maxExport * 0.9 && expVal >= 20.0;
        const isSpike = sStatus === 'spike' || sStatus === 'potential' || desc === 'spike' || expVal >= 50.0;
        if (isMaxExport || isSpike) {
          return 'EXPORT';
        }
      }
      
      // IMPORT recommendation:
      // If import price is negative (getting paid to charge) or very cheap
      if (impVal != null) {
        const isMinImport = impVal <= minImport + 2.0 || impVal <= minImport * 1.1;
        const isCheap = desc === 'negative' || desc === 'extremelylow' || impVal <= 12.0;
        if (isCheap || (isMinImport && impVal <= 15.0)) {
          return 'IMPORT';
        }
      }
      
      // AVOID IMPORT recommendation:
      // If import price is high positive price
      if (impVal != null) {
        const isMaxImport = impVal >= maxImport * 0.85 && impVal >= 30.0;
        const isSpike = sStatus === 'spike' || sStatus === 'potential' || desc === 'spike' || impVal >= 50.0;
        if (isMaxImport || isSpike) {
          return 'AVOID IMPORT';
        }
      }
      
      return 'HOLD';
    },

    getRecommendationText(group) {
      if (!group || !group.items || !group.items.length) return 'HOLD';
      const rec = this.getRecommendation(
        group.avg_import,
        group.avg_export,
        group.items[0]?.spike_status,
        group.items[0]?.descriptor
      );
      return rec;
    },

    getRecommendationStyle(group) {
      const rec = this.getRecommendationText(group);
      if (rec === 'IMPORT') return 'background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)';
      if (rec === 'EXPORT') return 'background:rgba(234,179,8,0.15);color:#eab308;border:1px solid rgba(234,179,8,0.3)';
      if (rec === 'AVOID IMPORT') return 'background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.3)';
      return 'background:rgba(100,116,139,0.15);color:#94a3b8;border:1px solid rgba(100,116,139,0.2)';
    },

    getItemRecommendationText(item) {
      if (!item) return 'HOLD';
      return this.getRecommendation(
        item.import_c_kwh,
        item.export_c_kwh,
        item.spike_status,
        item.descriptor
      );
    },

    getItemRecommendationStyle(item) {
      const rec = this.getItemRecommendationText(item);
      if (rec === 'IMPORT') return 'background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid rgba(34,197,94,0.3)';
      if (rec === 'EXPORT') return 'background:rgba(234,179,8,0.15);color:#eab308;border:1px solid rgba(234,179,8,0.3)';
      if (rec === 'AVOID IMPORT') return 'background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.3)';
      return 'background:rgba(100,116,139,0.15);color:#94a3b8;border:1px solid rgba(100,116,139,0.2)';
    },

    get summary() {
      if (!this.pricingData) return '';
      const descriptor = (this.pricingData.tariff_type || '').toLowerCase();
      const descMap = {
        offpeak:  'Energy is cheap right now. Great time to charge your battery.',
        shoulder: 'Energy prices are moderate right now.',
        peak:     "Prices are elevated — consider reducing grid import.",
        spike:    '⚠ Price spike in progress — avoid high-load appliances.',
        unknown:  'Current price data available from your provider.',
      };
      const renewables = this.pricingData.renewables_pct;
      let base = descMap[descriptor] || descMap.unknown;
      if (renewables !== null && renewables < 15) {
        base += ` The grid's supply is mainly coal and gas (${renewables}% renewables).`;
      } else if (renewables !== null && renewables > 60) {
        base += ` Grid is very green right now — ${renewables}% renewables!`;
      }
      return base;
    },
  };
}

function sdPricesPanel() {
  return {
    // ── State ──────────────────────────────────────────────────────────────
    loading: true,
    error: null,
    emulation: false,

    // Live price (from /api/pricing/current)
    livePrice: null,
    feedInPrice: null,

    // Site metadata (from /api/pricing/amber/site)
    siteInfo: null,

    // Price strip (forecast intervals from /api/pricing/current)
    priceStrip: [],
    stripResolution: 5,   // always 5-min from Amber API
    expandedWindow: null, // index of the expanded 30-min window
    forecastChannel: 'import',  // 'import' | 'export' — controls which price the forecast strip shows

    // Usage history section
    usageRange: '7d',      // 'today' | '7d' | '30d'
    usageData: null,       // { general: [], feed_in: [] }
    priceHistory: null,    // { general: [], feed_in: [] }
    selectedDay: null,     // YYYY-MM-DD
    selectedMeter: 'all',  // 'all' | 'general' | 'solar'
    loadingUsage: false,

    // Raw API debug panel
    showRawPanel: false,
    rawPanelTab: 'live',  // 'live' | 'usage' | 'prices'

    // ── Lifecycle ──────────────────────────────────────────────────────────
    async init() {
      await Promise.all([
        this.loadLivePrice(),
        this.loadSiteInfo(),
      ]);
      this.loading = false;
      await this.loadUsage();
      // Scroll price strip to current window
      await this.$nextTick();
      this._scrollStripToCurrent();
      // Live price poll every 60s
      if (this._pricePollTimer) clearInterval(this._pricePollTimer);
      this._pricePollTimer = setInterval(() => this.loadLivePrice(), 60000);
    },

    // ── Data loading ───────────────────────────────────────────────────────
    async loadLivePrice() {
      try {
        const r = await fetchJSON('api/pricing/current');
        if (!r?.ok) { this.error = r?.error || 'Failed to load price'; return; }
        this.emulation = !!r.emulation;
        const d = r.data;
        this.livePrice = d;
        this.feedInPrice = d.export_c_kwh;
        // Build price strip from forecast array; add actuals if present
        this.priceStrip = (d.forecast || []).map(p => ({
          start_time:    p.start,
          end_time:      p.end,
          per_kwh:       p.import_c_kwh,
          export_kwh:    p.export_c_kwh,
          renewables:    p.renewables_pct,
          tariff_type:   p.tariff_type,
          demand_window: !!p.demand_window,   // per-interval flag from TariffInformation
          spike_status:  p.spike_status  || 'none',    // A1: SpikeStatus
          descriptor:    p.descriptor    || 'neutral', // A2: raw PriceDescriptor
          tariff_period: p.tariff_period  || null,     // A3: offPeak | shoulder | solarSponge | peak
          tariff_season: p.tariff_season  || null,     // A3: season string
          type:          'ForecastInterval',
          is_current:    false,
        }));
        // Mark current interval
        const now = Date.now();
        const valid = d.valid_until ? new Date(d.valid_until).getTime() : null;
        if (valid && now < valid) {
          const startMs = valid - (d.interval_min || 5) * 60000;
          this.priceStrip.unshift({
            start_time:    new Date(startMs).toISOString(),
            end_time:      d.valid_until,
            per_kwh:       d.import_c_kwh,
            export_kwh:    d.export_c_kwh,
            renewables:    d.renewables_pct,
            tariff_type:   d.tariff_type,
            demand_window: !!d.demand_window, // current interval's own flag
            spike_status:  d.spike_status  || 'none',
            descriptor:    'neutral', // not available at snapshot level
            tariff_period: d.current_period?.tariff_info?.period || null,
            tariff_season: null,
            type:          'CurrentInterval',
            is_current:    true,
          });
        }
        // Reset accordion on data update
        this.expandedWindow = null;
      } catch(e) {
        this.error = 'Live price unavailable';
      }
    },

    async loadSiteInfo() {
      try {
        const r = await fetchJSON('api/pricing/amber/site');
        if (r?.ok) {
          this.siteInfo = r.data;
          this.emulation = this.emulation || !!r.emulation;
        }
      } catch(e) { /* non-critical */ }
    },

    async loadUsage() {
      this.loadingUsage = true;
      const { start, end } = this._dateRange();
      try {
        const [usageR, priceR] = await Promise.all([
          fetchJSON(`api/pricing/amber/usage?start_date=${start}&end_date=${end}`),
          fetchJSON(`api/pricing/amber/prices?start_date=${start}&end_date=${end}`),
        ]);
        if (usageR?.ok)  this.usageData    = usageR.data;
        if (priceR?.ok)  this.priceHistory = priceR.data;
      } finally {
        // IMPORTANT: set false BEFORE rebuilding charts so x-if canvases exist in DOM
        this.loadingUsage = false;
      }
      // Let Alpine render the unblocked x-if sections first
      await this.$nextTick();
      this._rebuildUsageChart();
      // Auto-select the end date (today/period end) on first load
      if (!this.selectedDay) this.selectedDay = end;
      // Another tick so x-if="selectedDay" block renders its canvases
      await this.$nextTick();
      this._rebuildSubChart();
    },

    // ── Date range helpers ─────────────────────────────────────────────────
    _dateRange() {
      const today = new Date();
      const fmt = d => d.toISOString().slice(0, 10);
      if (this.usageRange === 'today') {
        return { start: fmt(today), end: fmt(today) };
      }
      const days = this.usageRange === '30d' ? 29 : 6;
      const start = new Date(today); start.setDate(today.getDate() - days);
      return { start: fmt(start), end: fmt(today) };
    },

    // ── Chart builders ────────────────────────────────────────────────────
    _rebuildUsageChart() {
      const canvas = document.getElementById('amber-usage-chart');
      if (!canvas || !this.usageData) return;
      const existingChart = Chart.getChart(canvas);
      if (existingChart) existingChart.destroy();

      // Aggregate by day
      const days = {};
      (this.usageData.general || []).forEach(r => {
        const day = (r.start_time || '').slice(0, 10);
        if (!days[day]) days[day] = { import: 0, export: 0, cost: 0, exportCost: 0, renewSamples: [] };
        days[day].import += r.kwh || 0;
        days[day].cost   += r.cost || 0;
        if (r.renewables != null) days[day].renewSamples.push(r.renewables);
      });
      (this.usageData.feed_in || []).forEach(r => {
        const day = (r.start_time || '').slice(0, 10);
        if (!days[day]) days[day] = { import: 0, export: 0, cost: 0, exportCost: 0, renewSamples: [] };
        days[day].export     += Math.abs(r.kwh  || 0);
        days[day].exportCost += (r.cost || 0);
      });

      const labels  = Object.keys(days).sort();
      const imports = labels.map(d => +(days[d].import.toFixed(2)));
      const exports = labels.map(d => -(+(days[d].export.toFixed(2)))); // negative for solar chart

      new Chart(canvas, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              label: 'General Usage (kWh)',
              data: imports,
              backgroundColor: 'rgba(59,130,246,0.7)',
              borderColor:     'rgba(59,130,246,1)',
              borderWidth: 1,
              borderRadius: 3,
            },
            {
              label: 'Solar Exports (kWh)',
              data: exports,
              backgroundColor: 'rgba(245,158,11,0.7)',
              borderColor:     'rgba(245,158,11,1)',
              borderWidth: 1,
              borderRadius: 3,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
            tooltip: { mode: 'index', intersect: false },
          },
          scales: {
            x: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
            y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.06)' } },
          },
          onClick: (e, elements) => {
            if (elements.length) {
              this.selectedDay = labels[elements[0].index];
              this.$nextTick(() => this._rebuildSubChart());
            }
          },
        },
      });

      // Store day totals for KPI display
      this._dayTotals = days;
    },

    _rebuildSubChart() {
      if (!this.selectedDay || !this.usageData) return;
      const canvas = document.getElementById('amber-sub-chart');
      if (!canvas) return;
      const existingChart = Chart.getChart(canvas);
      if (existingChart) existingChart.destroy();

      const filtered = source => (source || []).filter(r => (r.start_time || '').startsWith(this.selectedDay));
      const genData  = filtered(this.usageData.general);
      const fiData   = filtered(this.usageData.feed_in);

      const times  = genData.map(r => r.start_time.slice(11, 16));
      const kwh    = genData.map(r => r.kwh || 0);
      const solar  = fiData.map(r  => -(Math.abs(r.kwh || 0)));
      // Pad solar to same length if meters differ
      while (solar.length < kwh.length) solar.push(0);

      new Chart(canvas, {
        type: 'bar',
        data: {
          labels: times,
          datasets: [
            { label: 'General Usage', data: kwh,   backgroundColor: 'rgba(59,130,246,0.6)',  borderRadius: 2 },
            { label: 'Solar Exports', data: solar,  backgroundColor: 'rgba(245,158,11,0.6)',  borderRadius: 2 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { labels: { color: '#94a3b8', font: { size: 10 } } } },
          scales: {
            x: { ticks: { color: '#64748b', font: { size: 9 }, maxRotation: 50 }, grid: { display: false } },
            y: { ticks: { color: '#64748b', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });

      // Rebuild price history chart
      this._rebuildPriceHistChart();
    },

    _rebuildPriceHistChart() {
      if (!this.selectedDay || !this.priceHistory) return;
      const canvas = document.getElementById('amber-price-hist-chart');
      if (!canvas) return;
      const existingChart = Chart.getChart(canvas);
      if (existingChart) existingChart.destroy();

      const dayPrices = (this.priceHistory.general || []).filter(r => (r.start_time || '').startsWith(this.selectedDay));
      const times  = dayPrices.map(r => r.start_time.slice(11, 16));
      const prices = dayPrices.map(r => +(r.per_kwh || 0).toFixed(1));

      new Chart(canvas, {
        type: 'line',
        data: {
          labels: times,
          datasets: [{
            label: 'Price ¢/kWh',
            data: prices,
            borderColor:     'rgba(99,102,241,0.9)',
            backgroundColor: 'rgba(99,102,241,0.08)',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            borderWidth: 1.5,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#64748b', font: { size: 9 }, maxRotation: 50 }, grid: { display: false } },
            y: { ticks: { color: '#64748b', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
          },
        },
      });
    },

    // ── 30-min window strip: 24 hours aligned grid (12h past → 12h future) ──
    expandedWindows: [],

    // ── 30-min window strip: 24 hours aligned grid (12h past → 12h future) ──
    get priceWindows() {
      const now     = Date.now();
      const WIN_MS  = 30 * 60 * 1000; // 30 min

      // Align to nearest :00/:30 boundary
      const curWinStart = Math.floor(now / WIN_MS) * WIN_MS;
      const rangeStart  = curWinStart - 12 * 60 * 60 * 1000; // 12h before
      const rangeEnd    = curWinStart + 12 * 60 * 60 * 1000; // 12h after

      const seenImport = new Set();
      const seenExport = new Set();
      const importIntervals = [];
      const exportIntervals = [];

      (this.priceStrip || []).forEach(p => {
        if (!seenImport.has(p.start_time)) {
          seenImport.add(p.start_time);
          importIntervals.push(p);
        }
        if (!seenExport.has(p.start_time)) {
          seenExport.add(p.start_time);
          exportIntervals.push({ ...p, per_kwh: p.export_kwh ?? p.per_kwh });
        }
      });

      if (this.priceHistory?.general) {
        this.priceHistory.general.forEach(p => {
          if (!seenImport.has(p.start_time)) {
            seenImport.add(p.start_time);
            importIntervals.push(p);
          }
        });
      }
      if (this.priceHistory?.feed_in) {
        this.priceHistory.feed_in.forEach(p => {
          if (!seenExport.has(p.start_time)) {
            seenExport.add(p.start_time);
            exportIntervals.push({ ...p, per_kwh: p.per_kwh ?? 0 });
          }
        });
      }

      const windows = [];
      const autoExpandIdxs = [];
      let idx = 0;
      for (let winMs = rangeStart; winMs < rangeEnd; winMs += WIN_MS, idx++) {
        const winEnd    = winMs + WIN_MS;
        const isCurrent = winMs <= now && now < winEnd;
        const isPast    = winEnd <= now;

        const importBucket = importIntervals.filter(p => {
          const ps = Date.parse(p.start_time);
          return ps >= winMs && ps < winEnd;
        });
        const exportBucket = exportIntervals.filter(p => {
          const ps = Date.parse(p.start_time);
          return ps >= winMs && ps < winEnd;
        });

        if (!importBucket.length && !exportBucket.length) {
          windows.push({
            idx, is_current: isCurrent, is_past: isPast,
            start_time:  new Date(winMs).toISOString(),
            end_time:    new Date(winEnd).toISOString(),
            avg_import: null, avg_export: null, renewables: null,
            tariff_type: isPast ? 'PAST' : 'FUTURE', intervals: [],
          });
          continue;
        }

        const avgImport = importBucket.length ? (importBucket.reduce((s,b) => s+(b.per_kwh||0), 0) / importBucket.length) : null;
        const avgExport = exportBucket.length ? (exportBucket.reduce((s,b) => s+(b.per_kwh||0), 0) / exportBucket.length) : null;

        const renews = [...importBucket, ...exportBucket].filter(b => b.renewables != null);
        const avgRenew = renews.length
          ? Math.round(renews.reduce((s,b) => s+b.renewables, 0) / renews.length)
          : null;

        const tCounts = {};
        importBucket.forEach(b => { tCounts[b.tariff_type] = (tCounts[b.tariff_type]||0)+1; });
        const tariff = Object.entries(tCounts).sort((a,b)=>b[1]-a[1])[0]?.[0] || 'UNKNOWN';

        const hasDemand = importBucket.some(b => b.demand_window) || exportBucket.some(b => b.demand_window);

        const spikeStatus = importBucket.some(b => b.spike_status === 'spike') || exportBucket.some(b => b.spike_status === 'spike') ? 'spike'
                          : importBucket.some(b => b.spike_status === 'potential') || exportBucket.some(b => b.spike_status === 'potential') ? 'potential'
                          : 'none';

        const descriptor = (() => {
          const ORDER = ['spike','high','neutral','low','veryLow','extremelyLow','negative'];
          for (const d of ORDER) {
            if (importBucket.some(b => b.descriptor === d) || exportBucket.some(b => b.descriptor === d)) return d;
          }
          return 'neutral';
        })();

        const tariffPeriod = importBucket.find(b => b.tariff_period)?.tariff_period || exportBucket.find(b => b.tariff_period)?.tariff_period || null;
        const tariffSeason = importBucket.find(b => b.tariff_season)?.tariff_season || exportBucket.find(b => b.tariff_season)?.tariff_season || null;
        const isForceCharge = importBucket.some(b => b.descriptor === 'negative' || b.descriptor === 'extremelyLow');

        // Compile sub-intervals with both Import & Export values
        const intervals = [];
        const times = Array.from(new Set([
          ...importBucket.map(p => p.start_time),
          ...exportBucket.map(p => p.start_time)
        ])).sort();

        for (const t of times) {
          const impItem = importBucket.find(p => p.start_time === t);
          const expItem = exportBucket.find(p => p.start_time === t);
          const baseItem = impItem || expItem;
          intervals.push({
            start_time: t,
            import_c: impItem ? impItem.per_kwh : null,
            export_c: expItem ? expItem.per_kwh : null,
            is_current: baseItem.is_current,
            tariff_type: baseItem.tariff_type,
            renewables: baseItem.renewables,
            spike_status: baseItem.spike_status,
            tariff_period: baseItem.tariff_period
          });
        }

        const isSpikeActive = spikeStatus === 'spike' || spikeStatus === 'potential' || tariff === 'SPIKE';
        if (isSpikeActive) {
          autoExpandIdxs.push(idx);
        }

        windows.push({
          idx, is_current: isCurrent, is_past: isPast,
          start_time:      new Date(winMs).toISOString(),
          end_time:        new Date(winEnd).toISOString(),
          avg_import:    avgImport,
          avg_export:    avgExport,
          renewables:    avgRenew,
          tariff_type:   tariff,
          intervals:     intervals,
          has_demand_window: hasDemand,
          spike_status:  spikeStatus,
          descriptor:    descriptor,
          tariff_period: tariffPeriod,
          tariff_season: tariffSeason,
          is_force_charge: isForceCharge,
        });
      }

      if (autoExpandIdxs.length > 0) {
        this.expandedWindows = Array.from(new Set([...this.expandedWindows, ...autoExpandIdxs]));
      }

      return windows;
    },

    toggleWindow(idx) {
      if (this.expandedWindows.includes(idx)) {
        this.expandedWindows = this.expandedWindows.filter(i => i !== idx);
      } else {
        this.expandedWindows.push(idx);
      }
    },

    // ── Period-level KPIs (full loaded date range) ─────────────────────
    get periodKpis() {
      if (!this._dayTotals) return null;
      let totalCost = 0, totalKwh = 0, totalExport = 0, totalExportCost = 0;
      const renewSamples = [];
      Object.values(this._dayTotals).forEach(d => {
        totalCost       += d.cost || 0;
        totalKwh        += d.import || 0;
        totalExport     += d.export || 0;
        totalExportCost += d.exportCost || 0;
        (d.renewSamples || []).forEach(r => renewSamples.push(r));
      });
      const avgRenew = renewSamples.length
        ? Math.round(renewSamples.reduce((a,b) => a+b, 0) / renewSamples.length)
        : null;
      return {
        cost:        totalCost.toFixed(2),
        kwh:         totalKwh.toFixed(1),
        export_kwh:  totalExport.toFixed(1),
        export_cost: totalExportCost.toFixed(2),
        renewables:  avgRenew,
        label:       this.usageRange === 'today' ? 'Today' : (this.usageRange === '7d' ? 'Last 7 Days' : 'Last 30 Days'),
      };
    },

    // ── Computed helpers ───────────────────────────────────────────────────
    get summary() {
      if (!this.livePrice) return '';
      const descriptor = (this.livePrice.tariff_type || '').toLowerCase();
      const descMap = {
        offpeak:  'Energy is cheap right now. Great time to charge your battery.',
        shoulder: 'Energy prices are moderate right now.',
        peak:     "Prices are elevated — consider reducing grid import.",
        spike:    '⚠ Price spike in progress — avoid high-load appliances.',
        unknown:  'Current price data available from your provider.',
      };
      const renewables = this.livePrice.renewables_pct;
      let base = descMap[descriptor] || descMap.unknown;
      if (renewables !== null && renewables < 15) {
        base += ` The grid's supply is mainly coal and gas (${renewables}% renewables).`;
      } else if (renewables !== null && renewables > 60) {
        base += ` Grid is very green right now — ${renewables}% renewables!`;
      }
      return base;
    },

    livePriceBgColor(tariff) {
      return {
        OFFPEAK:  '#22c55e',
        SHOULDER: '#f59e0b',
        PEAK:     '#ef4444',
        SPIKE:    '#dc2626',
      }[tariff] || '#6366f1';
    },

    // Returns the primary CSS colour for a 30-min window based on descriptor + spike
    windowColor(win) {
      if (win.spike_status === 'spike')                                   return '#ef4444'; // red
      if (win.spike_status === 'potential')                               return '#f97316'; // orange
      if (win.descriptor === 'negative' || win.descriptor === 'extremelyLow') return '#a855f7'; // purple — force-charge signal
      if (win.tariff_period === 'solarSponge')                            return '#f59e0b'; // gold
      if (win.descriptor === 'veryLow')                                   return '#22c55e'; // green
      if (win.descriptor === 'low')                                       return '#84cc16'; // lime
      if (win.descriptor === 'high')                                      return '#f97316'; // orange
      if (win.descriptor === 'spike')                                     return '#ef4444'; // red
      return this.livePriceBgColor(win.tariff_type);                      // existing fallback
    },

    // Returns a short label badge string for a window, or null if no signal worth showing
    periodLabel(win) {
      if (win.spike_status === 'spike')                                       return '⚡ SPIKE';
      if (win.spike_status === 'potential')                                   return '⚠ SPIKE?';
      if (win.tariff_period === 'solarSponge')                               return '☀ EXPORT';
      if (win.descriptor === 'negative' || win.descriptor === 'extremelyLow') return '💜 FREE';
      if (win.tariff_period === 'peak')                                       return '🔴 PEAK';
      if (win.tariff_period === 'shoulder')                                   return '🟠 SHOULDER';
      return null;
    },

    intervalColor(item) {
      if (item.is_current) return '#22c55e';
      if (item.type === 'ForecastInterval') return '#3b82f6';
      return '#64748b';
    },

    spikeIcon(item) {
      const d = (item.tariff_type || '').toUpperCase();
      if (d === 'SPIKE') return '🔺';
      if (d === 'PEAK')  return '⚠';
      return '●';
    },

    get dayKpis() {
      if (!this.selectedDay || !this._dayTotals?.[this.selectedDay]) return null;
      const d = this._dayTotals[this.selectedDay];
      const avgRenew = d.renewSamples.length
        ? Math.round(d.renewSamples.reduce((a,b) => a+b, 0) / d.renewSamples.length)
        : null;
      const exportTotal = (this.usageData?.feed_in || [])
        .filter(r => (r.start_time || '').startsWith(this.selectedDay))
        .reduce((s, r) => s + Math.abs(r.kwh || 0), 0);
      const exportCost = (this.usageData?.feed_in || [])
        .filter(r => (r.start_time || '').startsWith(this.selectedDay))
        .reduce((s, r) => s + (r.cost || 0), 0);
      return {
        cost:       d.cost.toFixed(2),
        renewables: avgRenew,
        kwh:        d.import.toFixed(2),
        export_kwh: exportTotal.toFixed(2),
        export_cost: exportCost.toFixed(2),
      };
    },

    // Raw JSON for debug panel — truncated to avoid massive renders
    get rawLive() {
      return JSON.stringify(this.livePrice, null, 2);
    },
    get rawUsage() {
      if (!this.usageData) return 'null';
      const trunc = {
        general:  (this.usageData.general  || []).slice(0, 20),
        feed_in:  (this.usageData.feed_in  || []).slice(0, 20),
        _note:    `Showing first 20 records per channel. Total: general=${(this.usageData.general||[]).length}, feed_in=${(this.usageData.feed_in||[]).length}`,
      };
      return JSON.stringify(trunc, null, 2);
    },
    get rawPrices() {
      if (!this.priceHistory) return 'null';
      const trunc = {
        general:  (this.priceHistory.general  || []).slice(0, 20),
        feed_in:  (this.priceHistory.feed_in  || []).slice(0, 20),
        _note:    `Showing first 20 records per channel. Total: general=${(this.priceHistory.general||[]).length}, feed_in=${(this.priceHistory.feed_in||[]).length}`,
      };
      return JSON.stringify(trunc, null, 2);
    },

    formatDay(dateStr) {
      if (!dateStr) return '—';
      const d = new Date(dateStr + 'T12:00:00');
      return d.toLocaleDateString('en-AU', { weekday:'short', day:'numeric', month:'short', year:'numeric' }).toUpperCase();
    },

    formatTime(isoStr) {
      if (!isoStr) return '';
      return new Date(isoStr).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', hour12:false });
    },

    // Scroll the price strip so the current 30-min window is visible in the centre
    _scrollStripToCurrent() {
      const container = document.getElementById('amber-price-strip');
      const current   = document.getElementById('amber-strip-current');
      if (!container || !current) return;
      const cLeft = current.offsetLeft;
      const cW    = current.offsetWidth;
      const ctW   = container.offsetWidth;
      container.scrollLeft = cLeft - ctW / 2 + cW / 2;
    },

    async changeRange(r) {
      this.usageRange  = r;
      this.selectedDay = null;
      // Destroy old chart instances so they rebuild cleanly on new canvas elements
      if (this.usageChart)     { this.usageChart.destroy();     this.usageChart = null; }
      if (this.subChart)       { this.subChart.destroy();       this.subChart = null; }
      if (this.priceHistChart) { this.priceHistChart.destroy(); this.priceHistChart = null; }
      await this.loadUsage();
    },
  };
}

function sdNotificationsPanel() {
  return {
    settings: { enabled: false, ha_target: '', triggers: [], actionable: false, actionable_ttl: 1800 },
    availableTriggers: [],
    detectedTargets: [],   // notify services discovered from live HA
    isDetecting: false,
    haConnected: false,
    haHost: '',
    haToken: '',
    haTokenSet: false,
    haYaml: '',
    saving: false,
    testing: false,
    testResult: null,
    testResultMsg: null,
    testingConn: false,
    showToken: false,
    yamlCopied: false,

    async init() {
      await this.loadSettings();
      if (this.haConnected) await this.discoverTargets();
      // Refresh when HA credentials are saved from the global Settings ⚙ panel
      window.addEventListener('ha:connection-updated', async () => {
        await this.loadSettings();
        if (this.haConnected) await this.discoverTargets();
      });
    },

    async loadSettings() {
      try {
        const r = await fetch('api/automation/notifications/settings');
        if (!r.ok) return;
        const d = await r.json();
        this.haConnected  = d.ha_connected  || false;
        this.haHost       = d.ha_host       || '';
        this.haTokenSet   = d.ha_token_set  || false;
        this.haYaml       = d.ha_automation_yaml || '';
        this.availableTriggers = d.available_triggers || [];
        this.settings = {
          enabled:        !!d.enabled,
          ha_target:      d.ha_target || '',
          triggers:       d.triggers || [],
          actionable:     !!d.actionable,
          actionable_ttl: d.actionable_ttl || 1800,
        };
      } catch(e) { console.warn('notifications: load failed', e); }
    },

    toggleTrigger(id) {
      const idx = this.settings.triggers.indexOf(id);
      if (idx >= 0) this.settings.triggers.splice(idx, 1);
      else this.settings.triggers.push(id);
    },

    async discoverTargets() {
      this.isDetecting = true;
      try {
        const r = await fetch('api/automation/notifications/targets');
        if (!r.ok) return;
        const d = await r.json();
        if (d.ok && d.targets?.length) {
          this.detectedTargets = d.targets;
          // Auto-select the first mobile_app target if ha_target is currently blank
          if (!this.settings.ha_target) {
            const first = d.targets.find(t => t.service.startsWith('mobile_app'));
            if (first) this.settings.ha_target = first.service;
          }
        }
      } catch(e) { console.warn('notifications: target discovery failed', e); }
      finally { this.isDetecting = false; }
    },

    async saveSettings() {
      this.saving = true;
      try {
        // Only the fields this screen edits. Spreading the whole loaded
        // settings object wrote back every field it had read, so a value
        // another screen changed in the meantime was silently reverted — and
        // any field added to the store later would be reverted too, without
        // this screen ever mentioning it. The endpoint carries omitted fields
        // forward, so naming them is both safe and honest.
        const body = {
          enabled:    !!this.settings.enabled,
          ha_target:  this.settings.ha_target || '',
          triggers:   this.settings.triggers || [],
          actionable: !!this.settings.actionable,
        };
        if (this.settings.actionable_ttl != null) {
          body.actionable_ttl = this.settings.actionable_ttl;
        }
        const r = await fetch('api/automation/notifications/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (r.ok) {
          this.haToken = '';          // clear the field after save (security hygiene)
          await this.loadSettings(); // refresh yaml + haConnected state
          this.$store?.app?.addToast?.('Notification settings saved', 'ok');
        }
      } catch(e) { console.warn('notifications: save failed', e); }
      finally { this.saving = false; }
    },

    async sendTest() {
      this.testing = true;
      this.testResult = null;
      try {
        const r = await fetch('api/automation/notifications/test', { method: 'POST' });
        this.testResult = r.ok ? 'ok' : 'error';
        setTimeout(() => { this.testResult = null; }, 4000);
      } catch(e) {
        this.testResult = 'error';
        setTimeout(() => { this.testResult = null; }, 4000);
      } finally {
        this.testing = false;
      }
    },

    async testConnection() {
      this.testingConn = true;
      this.testResultMsg = null;
      try {
        const res = await fetch('api/ha/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ha_host: this.haHost,
            ha_token: this.haToken
          })
        });
        const d = await res.json();
        if (d.ok) {
          this.testResultMsg = { ok: true, msg: '✓ Connected to ' + (d.ha_name || 'HA') };
        } else {
          this.testResultMsg = { ok: false, msg: '✗ ' + (d.error || 'Failed') };
        }
      } catch (e) {
        this.testResultMsg = { ok: false, msg: '✗ Error' };
      } finally {
        this.testingConn = false;
        setTimeout(() => { this.testResultMsg = null; }, 5000);
      }
    },

    async copyYaml() {
      try {
        await navigator.clipboard.writeText(this.haYaml);
        this.yamlCopied = true;
        setTimeout(() => { this.yamlCopied = false; }, 2500);
      } catch(e) { console.warn('copy failed', e); }
    },

    // Batch R.1 (2026-08-02): clear both timers on tab unmount. Dead code
    // under x-show; load-bearing under R.2 x-if lazy mount.
    destroy() {
      if (this._refresh300Timer) { clearInterval(this._refresh300Timer); this._refresh300Timer = null; }
      if (this._pricePollTimer)  { clearInterval(this._pricePollTimer);  this._pricePollTimer  = null; }
    },
  };
}
