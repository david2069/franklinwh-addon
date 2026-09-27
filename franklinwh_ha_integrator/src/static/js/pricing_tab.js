// pricing_tab.js — Services (renamed 2026-08-08 BD-02; was "Services Setup" v0.6.0)
// Architecture: 3 sub-tabs — Overview | Sites/Gateways | Electricity Utility

function pricingTab() {
  return {
    // ── Sub-tab navigation ────────────────────────────────
    activeSubTab: 'overview',   // 'overview' | 'sites' | 'utility'

    // ── Overview state ────────────────────────────────────
    currentPrice: null,
    forecast: [],
    refreshing: false,

    // ── Sites/Gateways state ──────────────────────────────
    sites: [],        // [{site_id, site_name, gateways:[...]}]  (2-level, no group)
    sitesLoading: false,
    showSiteTopologyModal: false,
    siteEdit: { site_id: '', site_name: '', grid_classification: 'on_grid', grid_type: 'single_phase', service_voltage: 230, service_amps: 63, gateways: [] },

    // ── Electricity Utility state ─────────────────────────
    utilityServices: [],
    pricingModels: [],

    // ── Editor Modal ──────────────────────────────────────
    showEditorModal: false,
    editingService: {},

    // ── Real-world tariff model ───────────────────────────────────────────
    // The FranklinWH TOU profile is the Cloud API's shape — four wave types
    // with a buy and a sell rate — which is what the battery is told. It
    // cannot express a demand charge, a two-way export charge or a standing
    // charge, so none of those ever reached a bill. These drive the section
    // that can.
    standingCharges: [],

    /** Warn when the plan's zone and the browser's disagree.
     *
     * The windows are wall-clock. If the plan is written in Australia/Sydney
     * and read somewhere else, a seasonal peak fires at the wrong hour and
     * nothing else says so. Advisory only — the schedule runs on the
     * gateway's clock, not this one. */
    get planTzMismatch() {
      const planTz = (this.editingService?.plan_timezone || '').trim();
      if (!planTz) return '';
      let localTz = '';
      try { localTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
      catch (e) { return ''; }
      if (!localTz || localTz === planTz) return '';
      return `This browser is in ${localTz}. The windows above are read as ${planTz}.`;
    },

    /** How many windows of a type exist, for the sections whose rules govern
     *  them. Configuring a demand interval and basis for a charge that has no
     *  window is silently inert, and nothing else says so. */
    windowSummary(type) {
      const rows = (this.serviceWindows || []).filter(w => w.window_type === type);
      if (!rows.length) {
        return { count: 0, text: `No ${type} window defined — these settings do nothing yet.` };
      }
      const described = rows.slice(0, 2).map(w => {
        const rate = w.rate == null ? '' :
          ` @ ${Math.abs(w.rate)}¢${(w.rate_kind === 'charge' || w.rate < 0) ? ' charge' : ''}`;
        return `${w.start_time}–${w.end_time}${rate}`;
      }).join(', ');
      const more = rows.length > 2 ? ` +${rows.length - 2} more` : '';
      return { count: rows.length, text: `${rows.length} ${type} window(s): ${described}${more}` };
    },

    async loadStandingCharges() {
      const id = this.editingService?.id;
      if (!id) { this.standingCharges = []; return; }
      try {
        const r = await fetchJSON(`api/utility-services/${id}/standing-charges`);
        this.standingCharges = r?.charges || [];
      } catch (e) {
        this.standingCharges = [];
      }
    },

    async addStandingCharge() {
      const id = this.editingService?.id;
      if (!id) {
        Alpine.store('app').addToast('Save the service first, then add charges to it.', 'warn');
        return;
      }
      const label = (this._newChargeLabel || '').trim() || 'Fixed charge';
      const r = await fetchJSON(`api/utility-services/${id}/standing-charges`, {
        method: 'POST',
        body: JSON.stringify({
          label,
          amount_c: Number(this._newChargeAmount) || 0,
          basis: this._newChargeBasis || 'per_day',
        }),
      });
      if (r?.ok) {
        this._newChargeLabel = ''; this._newChargeAmount = 0;
        await this.loadStandingCharges();
      } else {
        Alpine.store('app').addToast(r?.detail || 'Could not add the charge', 'error');
      }
    },

    async removeStandingCharge(chargeId) {
      const id = this.editingService?.id;
      if (!id) return;
      if (!await Alpine.store('app').confirm({
        title: 'Remove this standing charge?',
        detail: 'It stops being counted in the net total from now on.',
        confirmLabel: 'Remove', danger: true,
      })) return;
      const r = await fetchJSON(`api/utility-services/${id}/standing-charges/${chargeId}`,
                                { method: 'DELETE' });
      if (r?.ok) await this.loadStandingCharges();
    },

    _newChargeLabel: '',
    _newChargeAmount: 0,
    _newChargeBasis: 'per_day',

    editingGateways: [],
    editorSaving: false,
    editorTesting: false,
    editorTestResult: null,
    showDeleteModal: false,

    // "Same entity" checkbox: retailer_name === service name
    retailerIsSameAsUtility: false,

    // ── Section: Tariff Profile ───────────────────────────
    tariffProfile: null,
    tariffProfileLoading: false,

    // ── Section: VPP ─────────────────────────────────────
    vppStatus: null,
    vppError: null,
    vppChecking: false,

    // ── Section: Billing override lock ───────────────────
    billingManualOverride: false,

    // ── Section: Time Windows ─────────────────────────────
    serviceWindows: [],
    windowSaving: false,
    newWindow: { window_type: 'demand', label: '', start_time: '', end_time: '',
      day_type: 'weekdays', months: '', rate: null,
      // Direction is a field, never a sign — see migration v60.
      rate_kind: 'credit' },

    // ── Section: Audit Trail ──────────────────────────────
    auditLog: [],

    // ── Section: Solar Sources (service-direct only) ──────
    solarSources: [],         // all solar sources from API
    solarAssigned: [],        // src_id[] linked to THIS service and no gateway
    solarLoading: false,

    // ────────────────────────────────────────────────────────────────────────

    get gateways() {
      return Alpine.store('app').gateways || [];
    },

    get isAutoOverrideProvider() {
      return ['amber', 'localvolts'].includes(this.editingService?.pricing_provider);
    },

    get providerConnected() {
      return this.editorTestResult?.ok === true;
    },

    get defaultUtil() {
      return {
        id: '', name: '',
        site_id: '', site_name: '',
        retailer_name: '', account_number: '', account_name: '',
        nmi_id: '', meter_serial: '', meter_type: '',
        service_type: 'residential',
        // New v23 fields
        ac_wiring_type: 1,
        service_voltage_v: 230,
        service_amps: null,
        // Billing
        bill_frequency: 'quarterly', bill_start_day: 1,
        supply_charge_day: 0, metering_fee: 0, network_fixed_fee: 0,
        demand_charge_kw: 0, demand_window_start: '', demand_window_end: '',
        demand_window_days: 'weekdays',
        fit_rate_c_kwh: null, fit_provider: '',
        network_area: '', notes: '',
        // Must agree with pricing_model_id below: the two are the same fact,
        // and a save path reading the stale one silently retargets the service.
        pricing_provider: 'franklinwh_tou', pricing_credentials: '{}', pricing_settings: '{}',
        pricing_model_id: 'franklinwh_tou'
      };
    },

    // ────────────────────────────────────────────────────────────────────────
    // Lifecycle
    // ────────────────────────────────────────────────────────────────────────

    async init() {
      await Promise.all([
        this.loadUtilityServices(),
        this.loadPricingModels(),
        this.loadSites(),
        this.loadPrice(),
      ]);
      setInterval(() => { this.loadPrice(); }, 60000);
    },

    switchTab(tab) {
      this.activeSubTab = tab;
      if (tab === 'sites' && this.sites.length === 0) this.loadSites();
    },

    // ────────────────────────────────────────────────────────────────────────
    // Overview
    // ────────────────────────────────────────────────────────────────────────

    async loadPrice() {
      const r = await fetchJSON('api/pricing/current');
      if (r?.ok) { this.currentPrice = r.data; this.forecast = r.data.forecast || []; }
    },

    async forceRefresh() {
      this.refreshing = true;
      const r = await fetchJSON('api/pricing/refresh', { method: 'POST' });
      if (r?.ok) { this.currentPrice = r.data; this.forecast = r.data.forecast || []; }
      this.refreshing = false;
    },

    tariffColor(t) {
      return { OFFPEAK: 'var(--ok)', SHOULDER: 'var(--warn)', PEAK: '#f97316', SPIKE: 'var(--danger)' }[t] || 'var(--text-muted)';
    },

    tariffBg(t) {
      return { OFFPEAK: 'rgba(16,185,129,.1)', SHOULDER: 'rgba(245,158,11,.1)', PEAK: 'rgba(249,115,22,.1)', SPIKE: 'rgba(239,68,68,.1)' }[t] || 'var(--surface-1)';
    },

    providerLabel(p) {
      return {
        amber: 'Amber Electric',
        localvolts: 'LocalVolts (P2P)',
        aemo: 'AEMO NEM Wholesale',
        franklinwh_tou: 'FranklinWH TOU (Built-in)',
        comed: 'ComEd Hourly',
        flat: 'Flat Rate',
      }[p] || p || '—';
    },

    // Derive FWH site name from linked gateways when utility service has no site set
    _siteNameForService(svc) {
      if (svc.site_name) return svc.site_name;
      if (svc.site_id) return svc.site_id;
      // Walk loaded sites to find matching gateway
      const gwIds = svc.gateways || [];
      for (const site of (this.sites || [])) {
        if ((site.gateways || []).some(g => gwIds.includes(g.short_id))) {
          return site.site_name || ('Site ' + site.site_id);
        }
      }
      return null;
    },

    // Connection topology for overview diagram
    get overviewTopology() {
      const services = this.utilityServices;
      const allGateways = this.gateways;
      return services.map(svc => ({
        ...svc,
        _derivedSiteName: this._siteNameForService(svc),
        linkedGateways: allGateways.filter(g => (svc.gateways || []).includes(g.short_id)),
        isStandalone: !svc.gateways || svc.gateways.length === 0,
        wiringLabel: { 1: 'Single Phase', 2: 'Split Phase (2W)', 3: 'Three Phase (3W)' }[svc.ac_wiring_type] || 'Single Phase',
      }));
    },

    // Return enriched services with derived site names for the service list
    get enrichedUtilityServices() {
      return this.utilityServices.map(svc => ({
        ...svc,
        _derivedSiteName: this._siteNameForService(svc),
      }));
    },

    // ────────────────────────────────────────────────────────────────────────
    // Sites / Gateways
    // ────────────────────────────────────────────────────────────────────────

    async loadSites() {
      this.sitesLoading = true;
      try {
        const r = await fetchJSON('api/gateway-sites');
        if (r?.ok) {
          // Flatten groups into the site level (no group layer in UI)
          this.sites = (r.sites || []).map(site => {
            const gateways = [];
            for (const grp of (site.groups || [])) {
              for (const gw of (grp.gateways || [])) {
                gateways.push({ ...gw, _group: grp.group_name });
              }
            }
            return { ...site, gateways };
          });
        }
      } catch (e) {
        console.warn('[pricingTab] loadSites failed:', e);
      } finally {
        this.sitesLoading = false;
      }
    },

    // Gateway capability chips for Sites panel
    gwCapabilityChips(gw) {
      const chips = [];
      if (gw.has_solar)          chips.push({ label: 'Solar PV',      color: 'amber' });
      if (gw.has_ahub)           chips.push({ label: 'aHub',          color: 'sky' });
      if (gw.has_apbox)          chips.push({ label: 'aPBox',         color: 'violet' });
      if (gw.has_smart_circuits) chips.push({ label: 'Smart Circuits', color: 'teal' });
      if (gw.has_ct_split_grid)  chips.push({ label: 'Split Grid CT', color: 'orange' });
      if (gw.has_ct_split_pv)    chips.push({ label: 'Split Solar CT', color: 'lime' });
      if (gw.has_three_phase)    chips.push({ label: '3-Phase',       color: 'indigo' });
      if (gw.has_v2l)            chips.push({ label: 'V2L',           color: 'pink' });
      if (gw.has_generator)      chips.push({ label: 'Generator',     color: 'rose' });
      if (gw.vpp_enrolled)       chips.push({ label: 'VPP',           color: 'purple' });
      
      // Grid Topology specific chips
      if (gw.grid_type && gw.grid_type !== 'off_grid') {
        const types = {
          single_phase: '1-Phase',
          split_phase_240: 'Split-Phase 240V',
          split_phase_208: 'Split-Phase 208V',
          three_phase_230: '3-Phase 230V',
          three_phase_415: '3-Phase 415V'
        };
        chips.push({ label: types[gw.grid_type] || gw.grid_type, color: 'sky' });
      }
      if (gw.gateway_phase) {
        chips.push({ label: `Phase: ${gw.gateway_phase.toUpperCase()}`, color: 'indigo' });
      }
      if (gw.three_phase_group_id) {
        chips.push({ label: `Cluster: ${gw.three_phase_group_id}`, color: 'teal' });
      }
      if (gw.service_amps) {
        chips.push({ label: `${gw.service_amps}A Limit`, color: 'orange' });
      }
      return chips;
    },

    chipClass(color) {
      const map = {
        amber:  'bg-amber-500/15 text-amber-400 border-amber-500/25',
        sky:    'bg-sky-500/15 text-sky-400 border-sky-500/25',
        violet: 'bg-violet-500/15 text-violet-400 border-violet-500/25',
        teal:   'bg-teal-500/15 text-teal-400 border-teal-500/25',
        orange: 'bg-orange-500/15 text-orange-400 border-orange-500/25',
        lime:   'bg-lime-500/15 text-lime-400 border-lime-500/25',
        indigo: 'bg-indigo-500/15 text-indigo-400 border-indigo-500/25',
        pink:   'bg-pink-500/15 text-pink-400 border-pink-500/25',
        rose:   'bg-rose-500/15 text-rose-400 border-rose-500/25',
        purple: 'bg-purple-500/15 text-purple-400 border-purple-500/25',
        green:  'bg-green-500/15 text-green-400 border-green-500/25',
        gray:   'bg-gray-700 text-gray-400 border-gray-600',
      };
      return (map[color] || map.gray) + ' border text-[9px] font-black uppercase tracking-widest px-2 py-0.5 rounded-full';
    },

    gwGridStatus(gw) {
      if (gw.grid_type === 'off_grid') {
        return { label: 'Off-Grid (Island Microgrid)', cls: 'text-rose-400 font-bold' };
      }
      const s = gw.grid_connection_state;
      if (!s) return { label: 'Unknown', cls: 'text-gray-500' };
      if (s.toLowerCase().includes('connected'))   return { label: 'Grid Connected',    cls: 'text-green-400' };
      if (s.toLowerCase().includes('off'))         return { label: 'Off-Grid (Backup Active)', cls: 'text-amber-400' };
      return { label: s, cls: 'text-gray-400' };
    },

    openSiteTopologyEditor(site) {
      const sample = site.gateways.find(g => g.grid_type);
      const isOffGrid = site.gateways.some(g => g.grid_type === 'off_grid');
      
      this.siteEdit = {
        site_id: site.site_id,
        site_name: site.site_name,
        grid_classification: isOffGrid ? 'off_grid' : 'on_grid',
        grid_type: (sample && sample.grid_type !== 'off_grid') ? sample.grid_type : 'single_phase',
        service_voltage: (sample && sample.service_voltage) || 230,
        service_amps: (sample && sample.service_amps) || 63,
        gateways: site.gateways.map(g => ({
          short_id: g.short_id,
          name: g.name,
          gateway_phase: g.gateway_phase || '',
          three_phase_group_id: g.three_phase_group_id || '',
          service_amps: g.service_amps || ''
        }))
      };
      this.showSiteTopologyModal = true;
    },

    async saveSiteTopology() {
      const isOff = this.siteEdit.grid_classification === 'off_grid';
      const promises = this.siteEdit.gateways.map(gw => {
        const payload = {
          grid_type: isOff ? 'off_grid' : this.siteEdit.grid_type,
          service_amps: gw.service_amps ? parseInt(gw.service_amps) : parseInt(this.siteEdit.service_amps),
          gateway_phase: isOff ? null : (gw.gateway_phase || null),
          three_phase_group_id: isOff ? null : (gw.three_phase_group_id || null)
        };
        // /api/solar — api_solar.router is mounted with that prefix, so the
        // bare api/gateways/... path 404s. Save Topology has never worked: the
        // failure toast used the 'warning' level, which nothing styled, so it
        // rendered invisible and the dialog simply appeared to do nothing.
        return fetchJSON(`api/solar/gateways/${gw.short_id}/topology`, {
          method: 'PATCH',
          body: JSON.stringify(payload),
          headers: { 'Content-Type': 'application/json' }
        });
      });

      try {
        const results = await Promise.all(promises);
        if (results.every(r => r?.ok)) {
          Alpine.store('app').addToast('Site grid topology saved successfully!', 'success');
          this.showSiteTopologyModal = false;
          await this.loadSites();
        } else {
          Alpine.store('app').addToast('Some gateways failed to save topology.', 'warn');
          await this.loadSites();
        }
      } catch (e) {
        console.error('Failed to save site topology:', e);
        Alpine.store('app').addToast('Error saving topology.', 'error');
      }
    },

    openEditorFromSites(svc_id) {
      // Find the service and open editor directly on Electricity Utility tab
      const svc = this.utilityServices.find(s => s.id === svc_id);
      this.activeSubTab = 'utility';
      this.$nextTick(() => { if (svc) this.openEditor(svc); });
    },

    // ────────────────────────────────────────────────────────────────────────
    // Electricity Utility (service list + editor)
    // ────────────────────────────────────────────────────────────────────────

    async loadUtilityServices() {
      const r = await fetchJSON('api/utility-services');
      if (r?.ok && r.data) this.utilityServices = r.data;
    },

    async loadPricingModels() {
      const r = await fetchJSON('api/pricing/models');
      if (r?.ok && r.models) {
        this.pricingModels = r.models;
      }
    },

    openEditor(service = null) {
      this.loadPricingModels();
      this.billingManualOverride = false;
      this.editorTestResult = null;
      this.tariffProfile = null;
      this.vppStatus = null;
      this.vppError = null;
      this.serviceWindows = [];
      this.auditLog = [];
      this.solarSources = [];
      this.solarAssigned = [];
      this.newWindow = { window_type: 'demand', label: '', start_time: '', end_time: '',
      day_type: 'weekdays', months: '', rate: null,
      // Direction is a field, never a sign — see migration v60.
      rate_kind: 'credit' };

      if (service) {
        this.editingService = JSON.parse(JSON.stringify(service));
        // Keep the server's version to merge against on save — see saveService.
        this._loadedService = JSON.parse(JSON.stringify(service));
        try { this.editingService._credentials = JSON.parse(this.editingService.pricing_credentials || '{}'); } catch (e) { this.editingService._credentials = {}; }
        try { this.editingService._settings = JSON.parse(this.editingService.pricing_settings || '{}'); } catch (e) { this.editingService._settings = {}; }
        this.editingGateways = [...(service.gateways || [])];
        
        // Auto-detect associated_site_id
        const firstGwId = this.editingGateways[0];
        const matchingSite = this.sites.find(site => site.gateways.some(g => g.short_id === firstGwId));
        this.editingService.associated_site_id = matchingSite ? matchingSite.site_id : '';

        // Detect "same entity" checkbox state
        this.retailerIsSameAsUtility = !this.editingService.retailer_name
          || this.editingService.retailer_name === this.editingService.name;
        // Auto-load panels
        const gwId = (service.gateways || [])[0];
        if (gwId) this.refreshTariffProfile(gwId);
        this.loadWindows(service.id);
        this.loadAudit();
        this.loadSolarSources(service.id);
        this.loadStandingCharges();
      } else {
        this.editingService = { ...this.defaultUtil };
        this._loadedService = null;   // a new service has nothing to preserve
        this.standingCharges = [];    // and no charges of its own yet
        this.editingService._credentials = {};
        this.editingService._settings = {};
        this.editingGateways = [];
        this.editingService.associated_site_id = '';
        this.retailerIsSameAsUtility = true;
      }
      this.showEditorModal = true;
    },

    onSiteChange() {
      const siteId = this.editingService.associated_site_id;
      if (!siteId) {
        this.editingGateways = [];
        return;
      }
      const site = this.sites.find(s => s.site_id === siteId);
      if (site) {
        this.editingGateways = site.gateways.map(g => g.short_id);
      }
    },

    onRetailerSameChange() {
      if (this.retailerIsSameAsUtility) {
        this.editingService.retailer_name = '';
      }
    },

    get effectiveRetailerName() {
      if (this.retailerIsSameAsUtility) return this.editingService.name || '(same as service name)';
      return this.editingService.retailer_name;
    },

    async saveService() {
      // Validate: name required
      if (!this.editingService.name?.trim()) {
        Alpine.store('app').addToast('Service name is required', 'warn');
        return;
      }
      this.editorSaving = true;
      try {
        // If "same entity" checked, clear retailer_name (service name IS the retailer)
        if (this.retailerIsSameAsUtility) {
          this.editingService.retailer_name = '';
        }

        // Sync pricing_provider with selected pricing_model_id for backward compatibility
        this.editingService.pricing_provider = this.editingService.pricing_model_id || 'franklinwh_tou';

        // Repack JSON fields
        this.editingService.pricing_credentials = JSON.stringify(this.editingService._credentials || {});
        this.editingService.pricing_settings    = JSON.stringify(this.editingService._settings || {});

        // Merge over what the server last gave us for this service.
        //
        // editingService is a full object and the PUT writes every key it
        // carries. If it has been reset to defaultUtil while still holding an
        // id — or simply never populated for some field — those defaults are
        // written over the stored record, and COALESCE cannot save it because
        // 0 and 'flat' are values, not nulls. That is how a configured
        // 158.631 c/day supply charge became 0 and a franklinwh_tou service
        // became flat.
        const base = (this.editingService.id && this._loadedService
                      && this._loadedService.id === this.editingService.id)
                   ? this._loadedService : {};
        const payload = { ...base, ...this.editingService };
        delete payload._credentials;
        delete payload._settings;
        delete payload.gateways;

        let r;
        if (payload.id) {
          r = await fetchJSON(`api/utility-services/${payload.id}`, {
            method: 'PUT',
            body: JSON.stringify(payload),
            headers: { 'Content-Type': 'application/json' },
          });
          if (r?.ok) {
            const gwResp = await fetchJSON(`api/utility-services/${payload.id}/gateways`, {
              method: 'PUT',
              body: JSON.stringify(this.editingGateways),
              headers: { 'Content-Type': 'application/json' },
            });
            if (!gwResp?.ok) {
              Alpine.store('app').addToast(gwResp?.error || 'Failed to assign gateways', 'error');
              this.editorSaving = false;
              return;
            }
          }
        } else {
          payload.gateway_ids = this.editingGateways;
          r = await fetchJSON('api/utility-services', {
            method: 'POST',
            body: JSON.stringify(payload),
            headers: { 'Content-Type': 'application/json' },
          });
          if (r?.ok && r.id) payload.id = r.id;
        }

        if (r?.ok) {
          // Persist solar source links
          await this.saveSolarLinks(payload.id);
          Alpine.store('app').addToast('Utility service saved', 'success');
          this.showEditorModal = false;
          await Promise.all([this.loadUtilityServices(), this.loadSites(), this.loadPrice()]);
        } else {
          Alpine.store('app').addToast(r?.error || 'Save failed', 'error');
        }
      } finally {
        this.editorSaving = false;
      }
    },

    confirmDeleteService() {
      this.showDeleteModal = true;
    },

    async deleteService(id) {
      this.showDeleteModal = false;
      const r = await fetchJSON(`api/utility-services/${id}`, { method: 'DELETE' });
      if (r?.ok) {
        Alpine.store('app').addToast('Utility service deleted', 'success');
        this.showEditorModal = false;
        await Promise.all([this.loadUtilityServices(), this.loadSites(), this.loadPrice()]);
      } else {
        Alpine.store('app').addToast(r?.error || 'Delete failed', 'error');
      }
    },

    async testProviderConnection() {
      this.editorTesting = true;
      this.editorTestResult = null;
      try {
        const payload = {
          provider: this.editingService.pricing_provider,
          credentials: this.editingService._credentials,
          settings: this.editingService._settings,
          enabled: true
        };
        const r = await fetchJSON('api/pricing/test', {
          method: 'POST',
          body: JSON.stringify(payload),
          headers: { 'Content-Type': 'application/json' },
        });
        this.editorTestResult = r?.data || { ok: false, message: r?.error || 'Test failed' };
        if (this.editorTestResult.ok) {
          if (this.editorTestResult.nmi && !this.editingService.nmi_id)
            this.editingService.nmi_id = this.editorTestResult.nmi;
          if (this.editorTestResult.network && !this.editingService.network_area)
            this.editingService.network_area = this.editorTestResult.network;
        }
      } finally {
        this.editorTesting = false;
      }
    },

    // ── Tariff Profile ────────────────────────────────────

    async refreshTariffProfile(gwId = null) {
      const id = gwId || this.editingGateways[0] || null;
      if (!id) return;
      this.tariffProfileLoading = true;
      try {
        const r = await fetchJSON(`api/gateways/${id}/tariff-profile`);
        if (r?.ok) this.tariffProfile = r;
      } finally {
        this.tariffProfileLoading = false;
      }
    },

    async syncTariffProfile() {
      if (!this.tariffProfile || !this.editingService.id) return;
      const r = await fetchJSON(`api/utility-services/${this.editingService.id}/tariff-profile`, {
        method: 'POST',
        body: JSON.stringify(this.tariffProfile),
        headers: { 'Content-Type': 'application/json' },
      });
      if (r?.ok) {
        Alpine.store('app').addToast('Tariff profile saved to service record', 'success');
        this.editingService.tariff_validated_at = new Date().toISOString();
        this.editingService.tariff_type = this.tariffProfile.tariff_type;
        this.editingService.tariff_company_name = this.tariffProfile.tariff_company;
      } else {
        Alpine.store('app').addToast(r?.error || 'Sync failed', 'error');
      }
    },

    // ── VPP ──────────────────────────────────────────────

    async checkVppStatus() {
      const gwId = this.editingGateways[0] || null;
      if (!gwId) { this.vppError = 'No gateway assigned — cannot check VPP status.'; return; }
      this.vppChecking = true;
      this.vppError = null;
      try {
        const r = await fetchJSON(`api/gateways/${gwId}/vpp-status`);
        if (r?.ok) {
          this.vppStatus = r;
          const enrolled = !!(r.program_id || (r.flag && r.flag !== 0));
          const programName = r.program_name || r.partner_name || null;
          if (this.editingService.id) {
            await fetchJSON(`api/utility-services/${this.editingService.id}`, {
              method: 'PUT',
              body: JSON.stringify({ id: this.editingService.id, vpp_enrolled: enrolled ? 1 : 0, vpp_provider: programName }),
              headers: { 'Content-Type': 'application/json' },
            });
            this.editingService.vpp_enrolled = enrolled;
            this.editingService.vpp_provider = programName;
          }
        } else {
          this.vppError = r?.error || 'VPP status check failed.';
        }
      } catch (e) {
        this.vppError = 'Network error checking VPP status.';
      } finally {
        this.vppChecking = false;
      }
    },

    // ── Time Windows ─────────────────────────────────────

    async loadWindows(serviceId = null) {
      const id = serviceId || this.editingService.id;
      if (!id) return;
      const r = await fetchJSON(`api/utility-services/${id}/windows`);
      if (r?.ok) this.serviceWindows = r.data || [];
    },

    async addWindow() {
      const id = this.editingService.id;
      if (!id) { Alpine.store('app').addToast('Save the service first, then add windows.', 'warn'); return; }
      if (!this.newWindow.start_time || !this.newWindow.end_time) {
        Alpine.store('app').addToast('Start and end time are required.', 'warn'); return;
      }
      this.windowSaving = true;
      try {
        const r = await fetchJSON(`api/utility-services/${id}/windows`, {
          method: 'POST',
          body: JSON.stringify(this.newWindow),
          headers: { 'Content-Type': 'application/json' },
        });
        if (r?.ok) {
          Alpine.store('app').addToast('Window added', 'success');
          this.newWindow = { window_type: 'demand', label: '', start_time: '', end_time: '',
      day_type: 'weekdays', months: '', rate: null,
      // Direction is a field, never a sign — see migration v60.
      rate_kind: 'credit' };
          await this.loadWindows(id);
        } else {
          Alpine.store('app').addToast(r?.error || 'Failed to add window', 'error');
        }
      } finally {
        this.windowSaving = false;
      }
    },

    async deleteWindow(windowId) {
      const id = this.editingService.id;
      if (!confirm('Delete this time window?')) return;
      const r = await fetchJSON(`api/utility-services/${id}/windows/${windowId}`, { method: 'DELETE' });
      if (r?.ok) {
        Alpine.store('app').addToast('Window deleted', 'success');
        this.serviceWindows = this.serviceWindows.filter(w => w.id !== windowId);
      } else {
        Alpine.store('app').addToast(r?.error || 'Delete failed', 'error');
      }
    },

    // ── Audit Trail ──────────────────────────────────────

    async loadAudit() {
      const id = this.editingService.id;
      if (!id) return;
      const r = await fetchJSON(`api/utility-services/${id}/audit?limit=30`);
      if (r?.ok) this.auditLog = r.data || [];
    },

    // ── Gateway Model Auto-fill (Fix B) ──────────────────
    // When a gateway is selected and wiring fields are empty, fetch the device
    // model and pre-fill ac_wiring_type, service_voltage_v, service_amps.
    async gwModelAutoFill(gwShortId) {
      // Only auto-fill if the fields are at defaults/blank
      if (this.editingGateways.length !== 1) return; // only on first gateway selected
      const gw = this.gateways.find(g => g.short_id === gwShortId);
      if (!gw || !gw.hw_version_int) return;
      try {
        const r = await fetchJSON(`api/models/devices/${gw.hw_version_int}`);
        if (!r || r.detail) return;
        // ac_wiring_type: default single phase; for aGate we stay 1
        if (!this.editingService.ac_wiring_type || this.editingService.ac_wiring_type === 1) {
          // aGate is always single or split phase — leave as 1 by default
          // but override voltage from ac_hz
          if (r.ac_hz === 60) {
            this.editingService.service_voltage_v = 240;
          } else if (r.ac_hz === 50) {
            this.editingService.service_voltage_v = 230;
          }
        }
        if (r.max_service_amps && !this.editingService.service_amps) {
          this.editingService.service_amps = r.max_service_amps;
        }
        this._modelAutoFillHint = `Auto-filled from device model: ${r.model || r.name} (HW ${r.hw_version_int})`;
      } catch (e) {
        console.warn('[pricingTab] gwModelAutoFill failed:', e);
      }
    },

    _modelAutoFillHint: null,

    // ── Solar Sources (service-direct) ───────────────────
    // Only shows sources where gateway_id is null OR source already links to this service
    // Sources linked to a different service are shown greyed/disabled

    async loadSolarSources(serviceId) {
      this.solarLoading = true;
      try {
        const r = await fetchJSON('api/solar/sources');
        const sources = r?.sources || [];
        // Show:
        //   a) sources with no gateway AND no service (unassigned)
        //   b) sources already linked to THIS service
        //   c) sources linked to a different service (disabled, show warning)
        this.solarSources = sources.filter(s =>
          !s.gateway_id ||                          // no gateway — can link to service
          s.utility_service_id === serviceId        // already on this service
        );
        this.solarAssigned = sources
          .filter(s => s.utility_service_id === serviceId)
          .map(s => s.id);
      } catch (e) {
        console.warn('[pricingTab] solar sources load failed:', e);
      } finally {
        this.solarLoading = false;
      }
    },

    async saveSolarLinks(serviceId) {
      if (!serviceId || this.solarSources.length === 0) return;
      for (const src of this.solarSources) {
        const shouldLink  = this.solarAssigned.includes(src.id);
        const isLinked    = src.utility_service_id === serviceId;
        const linkedOther = src.utility_service_id && src.utility_service_id !== serviceId;
        if (shouldLink && !isLinked && !linkedOther) {
          await fetchJSON(`api/solar/sources/${src.id}/link`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ utility_service_id: serviceId }),
          });
        } else if (!shouldLink && isLinked) {
          await fetchJSON(`api/solar/sources/${src.id}/link`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ utility_service_id: null }),
          });
        }
      }
    },

    // Bill start day helper: null = N/A
    get billStartDayDisplay() {
      const d = this.editingService.bill_start_day;
      if (!d || d === 0) return 'N/A';
      const s = ['', 'st', 'nd', 'rd'];
      const v = d % 100;
      return d + (s[(v - 20) % 10] || s[v] || 'th') + ' of month';
    },

    toggleBillStartNA() {
      if (this.editingService.bill_start_day) {
        this.editingService.bill_start_day = null;
      } else {
        this.editingService.bill_start_day = 1;
      }
    },
  };
}
