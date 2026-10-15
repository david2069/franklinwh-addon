// amber_tab.js v6 — Alpine component for Amber Electric Hub (sub-tabbed)

// ── smartDispatchTab — root tab component (view state owner) ─────────────────
function smartDispatchTab() {
  return {
    view: 'overview',
    init() {
      // Listen for view-change requests from nested x-data scopes
      // (e.g. the "Configure →" checklist button inside sdOverviewPanel)
      window.addEventListener('sd:setview', e => { this.setView(e.detail.view); });
    },
    setView(newView) {
      if (this.view === newView) return;
      // If a mixer subtab has registered a guard (e.g. Strategy Mixer with unsaved changes)
      // call it first — it can abort navigation and show a confirmation modal.
      if (typeof window._mixerNavGuard === 'function') {
        const proceed = window._mixerNavGuard(null, newView);
        if (!proceed) return;  // guard blocked navigation
      }
      this.view = newView;
    }
  };
}

// sdPricesPanel migrated to dynamic_pricing_tab.js

// ── sdOverviewPanel — Setup Checklist + compact signal + compact history ──
function sdOverviewPanel() {
  return {
    checklist: [],
    overviewSignal: null,
    overviewHistory: [],
    engineMode: 'signal_only',

    async init() {
      await Promise.all([this.loadSignal(), this.loadChecklist(), this.loadHistory()]);
    },

    async loadSignal() {
      try {
        const r = await fetch('api/automation/signal');
        if (!r.ok) return;
        const d = await r.json();
        this.overviewSignal = d;
        this.engineMode = d.engine_mode || 'signal_only';
        // Keep global store in sync so statusBadge reads correct state
        if (window.Alpine) Alpine.store('app').sdEngineMode = this.engineMode;
      } catch(e) { console.warn('overview: signal fetch failed', e); }
    },


    navigateTo(sdView) {
      if (window.Alpine) Alpine.store('app').setSdView(sdView);
    },

    async loadHistory() {
      try {
        const r = await fetch('api/automation/history?source=amber&limit=8');
        if (!r.ok) return;
        const d = await r.json();
        this.overviewHistory = d.history || [];
      } catch(e) { console.warn('overview: history fetch failed', e); }
    },

    async loadChecklist() {
      const steps = [];

      // Step 1: Pricing Connected
      try {
        // Just check if we get any pricing status without error
        const r = await fetch('api/pricing/status');
        if (r.ok) {
          const d = await r.json();
          const lastFetch = d.last_fetch_seconds_ago;
          steps.push({
            id: 'amber_api',
            label: 'Utility Pricing Connected',
            detail: lastFetch !== null ? `Last fetch ${lastFetch}s ago` : 'No data fetched yet',
            status: lastFetch !== null && lastFetch < 600 ? 'ok' : 'warn',
            action: null,
          });
        }
      } catch(e) {
        steps.push({ id: 'amber_api', label: 'Utility Pricing Connected', detail: 'Status unavailable', status: 'error', action: null });
      }

      // Step 2: Engine mode — distinguish "running but dispatch disabled" from "active"
      const mode = this.engineMode;
      const strategy = Alpine.store('app')?.sdDispatchStrategy || null;
      const dispatchDisabled = strategy === 'disabled' || strategy === 'off' || strategy === null;
      let engineDetail, engineStatus;
      if (mode === 'active' && !dispatchDisabled) {
        engineDetail = 'Active — executing strategy rules';
        engineStatus = 'ok';
      } else if (dispatchDisabled || mode === 'paused') {
        engineDetail = 'Running — dispatch disabled (no pricing events will fire)';
        engineStatus = 'warn';
      } else {
        engineDetail = 'Signal Only — monitoring, not dispatching';
        engineStatus = 'ok';
      }
      steps.push({
        id: 'engine',
        label: 'Smart Dispatch Engine',
        detail: engineDetail,
        status: engineStatus,
        action: 'Configure →',
        actionAmberView: 'dispatch',
      });

      // Step 3: HA notifications
      try {
        const r = await fetch('api/automation/notifications/settings');
        if (r.ok) {
          const d = await r.json();
          const notifOk = d.ha_connected && d.enabled;
          steps.push({
            id: 'notifications',
            label: 'HA Notifications',
            detail: !d.ha_connected ? 'Home Assistant not connected — optional' : d.enabled ? `Enabled → ${d.ha_target || 'no target set'}` : 'Disabled',
            status: notifOk ? 'ok' : 'skip',
            action: 'Set up →',
            actionAmberView: 'notifications',  // navigates to notifications sub-tab
          });
        }
      } catch(e) {}

      this.checklist = steps;
    },

    signalBg(s) {
      if (!s) return '';
      const a = s.action || '';
      if (a === 'PAUSED') return 'bg-gray-800/40 border-gray-700';
      if (a === 'APPLY_PRESET') {
        const p = s.preset_name || '';
        if (p.includes('Force Charge')) return 'bg-teal-950/30 border-teal-800/50';
        if (p.includes('Peak Discharge')) return 'bg-orange-950/30 border-orange-800/50';
        if (p.includes('Solar Sponge')) return 'bg-yellow-950/20 border-yellow-800/40';
        if (p.includes('Spike Hold')) return 'bg-red-950/30 border-red-800/50';
      }
      return 'bg-[var(--surface-1)] border-[var(--border)]';
    },
    signalIcon(s) {
      if (!s) return 'fa-circle-question';
      const a = s.action || '';
      if (a === 'PAUSED') return 'fa-pause';
      if (a === 'APPLY_PRESET') {
        const p = s.preset_name || '';
        if (p.includes('Force Charge')) return 'fa-bolt';
        if (p.includes('Peak Discharge')) return 'fa-arrow-up';
        if (p.includes('Solar Sponge')) return 'fa-sun';
        if (p.includes('Spike Hold')) return 'fa-shield-halved';
      }
      return 'fa-rotate-left';
    },
    signalColor(s) {
      if (!s) return 'text-gray-500';
      const a = s.action || '';
      if (a === 'PAUSED') return 'text-gray-500';
      if (a === 'APPLY_PRESET') {
        const p = s.preset_name || '';
        if (p.includes('Force Charge')) return 'text-teal-400';
        if (p.includes('Peak Discharge')) return 'text-orange-400';
        if (p.includes('Solar Sponge')) return 'text-yellow-400';
        if (p.includes('Spike Hold')) return 'text-red-400';
      }
      return 'text-gray-300';
    },
    formatPriceCompact(v) {
      if (v === null || v === undefined) return '—';
      return `${v.toFixed(1)}¢`;
    },
    histTimeAgo(ts) {
      if (!ts) return '—';
      const diff = Math.floor((Date.now() - new Date(ts + 'Z').getTime()) / 1000);
      if (diff < 60) return `${diff}s`;
      if (diff < 3600) return `${Math.floor(diff/60)}m`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h`;
      return `${Math.floor(diff/86400)}d`;
    },
  };
}

// sdNotificationsPanel migrated to dynamic_pricing_tab.js

// ── sdEnginePanel — Live Engine Panel (Overview sub-tab) ─────────────────
// Replaces the legacy "Current Signal" card. Polls /api/amber/engine/eval every 15s.
function sdEnginePanel() {
  return {
    decision:        null,
    forecastPrice:   null,
    forecastList:    [],
    fleet:           [],
    log:             [],
    earnings:        null,
    loading:         true,
    forceLoading:    false,
    mockMode:        false,
    showSimulateModal: false,
    simulateLoading: false,
    simulateData:    null,
    simulateError:   null,
    countdown:       15,
    _timer:          null,
    _countdownTimer: null,

    async init() {
      this._stopPolling();
      await this.refresh();
      // Auto-refresh is OFF by default (backlog Issue B → 2026-07-11 user
      // directive). Timer only runs when BOTH:
      //   1) Alpine.store('app').sdAutoRefresh === true (user opt-in via
      //      the header toggle, persisted to localStorage), AND
      //   2) document.visibilityState === 'visible' (don't churn on hidden
      //      tabs — prior 60s tick was still firing even with the browser
      //      tab backgrounded, wasting network + CPU).
      // Each tick still fires Promise.all of 5 fetches
      // (eval/log/earnings/signal/pricing) so 60s is the floor. Countdown
      // timer is a UI-only 1s tick; harmless whether polling is on or off.
      this._countdownTimer = setInterval(() => {
        this.countdown = Math.max(0, this.countdown - 1);
      }, 1000);
      if (!this._visHandler) {
        this._visHandler = () => this._syncPolling();
        document.addEventListener('visibilitychange', this._visHandler);
      }
      this._syncPolling();
    },

    _syncPolling() {
      const on = !!(window.Alpine?.store('app')?.sdAutoRefresh);
      const visible = (typeof document === 'undefined') || document.visibilityState === 'visible';
      if (on && visible) this._startPolling();
      else this._stopPolling();
    },

    _startPolling() {
      if (this._timer) return;
      // Reset the visible countdown so the user immediately sees a live
      // "Next refresh in 60s" the moment they flip the toggle ON or the
      // tab regains visibility (rather than a stale "0s" until the first
      // tick fires 60s later).
      this.countdown = 60;
      this._timer = setInterval(() => this.refresh(), 60000);
    },

    _stopPolling() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },

    async refresh() {
      this.countdown = 60;
      try {
        const gw = Alpine.store('app').selectedGateway;
        const [evalR, logR, earnR, signalR, priceR] = await Promise.all([
          fetch(`api/smart_dispatch/eval?gateway_id=${gw}&mock_multi_gateway=${this.mockMode ? 1 : 0}`).then(r => r.json()),
          fetch(`api/smart_dispatch/log?gateway_id=${gw}&limit=8`).then(r => r.json()),
          fetch(`api/smart_dispatch/earnings?gateway_id=${gw}`).then(r => r.json()),
          // Pass short_id so /api/automation/signal reads per-gateway config
          // for dispatch_strategy (else it falls back to global and the
          // DISPATCH MODE pill diverges from what the engine actually uses).
          // Root cause of P1 cluster #2, fixed 2026-07-08.
          fetch(`api/automation/signal?short_id=${gw}`).then(r => r.json()).catch(() => null),
          fetch('api/pricing/current').then(r => r.json()).catch(() => null),
        ]);
        if (evalR?.ok) {
          this.decision = evalR.decision;
          this.fleet    = evalR.fleet || [];
        }
        if (logR?.ok)  this.log      = logR.log || [];
        if (earnR?.ok) this.earnings = earnR;
        if (priceR?.ok && priceR.data) {
          // EvalDecision.to_dict() doesn't expose import_c_kwh / export_c_kwh,
          // so the "Live Pricing" widget would always render —. Merge the
          // live snapshot from /api/pricing/current into decision so the
          // widget reads real values without touching the Python dataclass.
          if (this.decision && priceR.data.import_c_kwh != null) {
            this.decision.import_c_kwh = priceR.data.import_c_kwh;
            this.decision.export_c_kwh = priceR.data.export_c_kwh;
          }
          if (priceR.data.forecast?.length > 0) {
            this.forecastPrice = priceR.data.forecast[0];
            this.forecastList  = priceR.data.forecast || [];
          }
        }

        if (signalR?.engine_mode) {
          Alpine.store('app').sdEngineMode = signalR.engine_mode;
        }
        if (signalR?.dispatch_strategy) {
          Alpine.store('app').sdDispatchStrategy = signalR.dispatch_strategy;
          Alpine.store('app')._updateHaImpairment();
        }
      } catch(e) {
        console.warn('sdEnginePanel: refresh failed', e);
      } finally {
        this.loading = false;
      }
    },

    async forceEval() {
      this.forceLoading = true;
      try {
        const r = await fetch(`api/smart_dispatch/eval/force?gateway_id=${Alpine.store('app').selectedGateway}`, { method: 'POST' });
        const d = await r.json();
        if (d?.ok && d.decision) this.decision = d.decision;
        await this.refresh();
      } catch(e) {
        console.warn('sdEnginePanel: force eval failed', e);
      } finally {
        this.forceLoading = false;
      }
    },

    async simulateDispatch() {
      this.showSimulateModal = true;
      this.simulateLoading = true;
      this.simulateData = null;
      this.simulateError = null;
      try {
        const res = await fetch(`api/smart_dispatch/simulate?gateway_id=${Alpine.store('app').selectedGateway}`);
        const data = await res.json();
        if (data.ok) {
          this.simulateData = data;
        } else {
          console.error('Simulation failed:', data.error);
          this.simulateError = data.error || 'Engine dependencies not initialized';
        }
      } catch (e) {
        console.error('Simulation network error:', e);
        this.simulateError = 'Network error — could not reach simulation endpoint';
      } finally {
        this.simulateLoading = false;
      }
    },

    get categoryLabel() {
      const m = {
        demand_charge:   '⚡ Demand Charge',
        negative_export: '📉 Negative Export',
        price_spike:     '🔺 Price Spike',
        export_bonus:    '☀ Export Bonus',
        earnings_target: '💰 Earnings Target',
        force_charge:    '🔋 Force Charge',
        legacy_rule:     '📋 Custom Rule',
        fallback:        '🔄 Self-Consumption',
        paused:          '⏸ Paused',
      };
      return m[this.decision?.trigger_category] || '—';
    },

    get categoryColor() {
      const m = {
        demand_charge:   'text-purple-600 dark:text-purple-400',
        negative_export: 'text-red-600 dark:text-red-400',
        price_spike:     'text-red-600 dark:text-red-400',
        export_bonus:    'text-amber-600 dark:text-amber-400',
        earnings_target: 'text-green-600 dark:text-green-400',
        force_charge:    'text-teal-600 dark:text-teal-400',
        legacy_rule:     'text-blue-600 dark:text-blue-400',
        fallback:        'text-gray-600 dark:text-gray-400',
        paused:          'text-gray-500 dark:text-gray-500',
      };
      return m[this.decision?.trigger_category] || 'text-gray-600 dark:text-gray-400';
    },

    get categoryBg() {
      // Border-only — no tinted background, matches design system glass style
      const m = {
        demand_charge:   'border-purple-500/30 dark:border-purple-700/60',
        negative_export: 'border-red-500/30 dark:border-red-700/60',
        price_spike:     'border-red-500/30 dark:border-red-700/60',
        export_bonus:    'border-amber-500/30 dark:border-amber-700/50',
        earnings_target: 'border-green-500/30 dark:border-green-700/50',
        force_charge:    'border-teal-500/30 dark:border-teal-700/50',
        legacy_rule:     'border-blue-500/30 dark:border-blue-700/50',
        fallback:        'border-[var(--border)]',
        paused:          'border-gray-500/30 dark:border-gray-700/50',
      };
      return m[this.decision?.trigger_category] || 'border-[var(--border)]';
    },

    get statusBadge() {
      const d = this.decision;
      if (!d) return { label: '…', cls: 'bg-gray-500/10 text-gray-600 dark:text-gray-300' };
      if (d.trigger_category === 'paused')
        return { label: '⏸ Paused', cls: 'bg-gray-500/20 text-gray-600 dark:text-gray-300 border border-gray-500/20' };

      const strategy = Alpine.store('app').sdDispatchStrategy || 'auto';

      // User approval mode — show pending state
      if (strategy === 'user_approval') {
        if (d.requires_approval || (d.can_execute && d.action !== 'RESUME_NATIVE'))
          return { label: '👤 Approval Req’d', cls: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30 font-bold' };
        return { label: '👤 User Approval', cls: 'bg-amber-500/10 text-amber-600 dark:text-amber-300 border border-amber-500/20' };
      }

      // Info-only mode
      if (strategy === 'info')
        return { label: '📊 Info Only', cls: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300 border border-indigo-500/20' };

      // Auto mode
      if (strategy === 'auto') {
        if (d.can_execute && d.action !== 'RESUME_NATIVE')
          return { label: '⚡ Auto-Execute', cls: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border border-emerald-500/20 font-bold' };
        return { label: '⚡ Auto (Idle)', cls: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 border border-emerald-500/20' };
      }

      return { label: '📊 Info Only', cls: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300 border border-indigo-500/20' };
    },

    // Fleet Helpers for Compact View
    fleetActionBg(d) {
      if (!d) return 'border-[var(--border)] bg-[var(--surface-1)]';
      const a = d.action || '';
      if (a === 'GRID_CHARGE') return 'border-teal-500/30 dark:border-teal-700/50 bg-teal-500/5';
      if (a === 'GRID_EXPORT') return 'border-orange-500/30 dark:border-orange-700/50 bg-orange-500/5';
      if (a === 'HOLD')        return 'border-red-500/30 dark:border-red-700/60 bg-red-500/5';
      return 'border-[var(--border)] bg-[var(--surface-1)]';
    },
    fleetActionIcon(d) {
      if (!d) return 'fa-circle-question';
      const a = d.action || '';
      if (a === 'GRID_CHARGE') return 'fa-bolt';
      if (a === 'GRID_EXPORT') return 'fa-arrow-up';
      if (a === 'HOLD')        return 'fa-shield-halved';
      return 'fa-rotate-left';
    },
    fleetActionColor(d) {
      if (!d) return 'text-gray-500';
      const a = d.action || '';
      if (a === 'GRID_CHARGE') return 'text-teal-600 dark:text-teal-400';
      if (a === 'GRID_EXPORT') return 'text-orange-600 dark:text-orange-400';
      if (a === 'HOLD')        return 'text-red-600 dark:text-red-400';
      return 'text-gray-500 dark:text-gray-400';
    },

    get executionLabel() {
      const d = this.decision;
      if (!d) return '—';
      if (d.action === 'APPLY_PRESET')       return d.preset_name || 'Apply Preset';
      if (d.action === 'HA_ENTITY_CONTROL')  return `Control: ${d.ha_entity_action || '—'}`;
      if (d.action === 'RESUME_SC')          return 'Self-Consumption';
      if (d.action === 'PAUSED')             return 'Engine Paused';
      if (d.action === 'NOTIFY_OFFGRID')     return 'Notify: Off-Grid Approval';
      return d.action || '—';
    },

    logCategoryBadge(cat) {
      const m = {
        demand_charge:   'bg-purple-500/15 text-purple-600 dark:text-purple-400',
        negative_export: 'bg-red-500/15 text-red-600 dark:text-red-400',
        price_spike:     'bg-red-500/15 text-red-600 dark:text-red-400',
        export_bonus:    'bg-amber-500/15 text-amber-600 dark:text-amber-400',
        earnings_target: 'bg-green-500/15 text-green-600 dark:text-green-400',
        force_charge:    'bg-teal-500/15 text-teal-600 dark:text-teal-400',
        legacy_rule:     'bg-blue-500/15 text-blue-600 dark:text-blue-400',
        fallback:        'bg-gray-500/15 text-gray-600 dark:text-gray-400',
        paused:          'bg-gray-500/15 text-gray-500 dark:text-gray-500',
        time_schedule:   'bg-cyan-500/15 text-cyan-600 dark:text-cyan-400',
      };
      return m[cat] || 'bg-gray-500/15 text-gray-600 dark:text-gray-400';
    },

    logCategoryShort(cat) {
      const m = {
        demand_charge: 'DEMAND', negative_export: 'NEG-EXP',
        price_spike: 'SPIKE', export_bonus: 'BONUS',
        earnings_target: 'EARN', force_charge: 'CHARGE',
        legacy_rule: 'RULE', fallback: 'SC', paused: 'PAUSE',
        time_schedule: 'SCHED',
      };
      return m[cat] || (cat || '—').toUpperCase();
    },

    timeAgo(ts) {
      if (!ts) return '—';
      const diff = Math.floor((Date.now() - new Date(ts + (ts.includes('Z') ? '' : 'Z')).getTime()) / 1000);
      if (diff < 60)    return `${diff}s`;
      if (diff < 3600)  return `${Math.floor(diff/60)}m`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h`;
      return `${Math.floor(diff/86400)}d`;
    },

    get predictiveSocData() {
      const gw = Alpine.store('app').currentGateway;
      if (!gw) return null;
      
      const socCurr = gw.battery_soc || 0;
      const socTarget = this.decision?.target_soc || this.decision?.min_soc_limit || this.decision?.max_soc_limit || 100;
      
      const solarKw = gw.solar_kw || 0;
      const loadKw = gw.home_kw || 0;
      const batteryKw = gw.battery_kw || 0;
      const gridKw = gw.grid_kw || 0;
      
      const batteryCount = gw.battery_count || 1;
      const nomCapacity = batteryCount * 13.6;
      
      const energyGap = Math.abs(socTarget - socCurr) * nomCapacity / 100.0;
      
      let estHours = null;
      let estCompletion = '—';
      
      if (batteryKw > 0.05) {
        const eff = 0.95;
        estHours = energyGap / (batteryKw * eff);
      } else if (batteryKw < -0.05) {
        const eff = 0.95;
        estHours = energyGap / (Math.abs(batteryKw) / eff);
      }
      
      if (estHours !== null && isFinite(estHours) && estHours > 0) {
        if (estHours > 24) {
          estCompletion = '> 24 hours';
        } else {
          const totalMs = estHours * 3600000;
          const completionTime = new Date(Date.now() + totalMs);
          const hh = String(completionTime.getHours()).padStart(2, '0');
          const mm = String(completionTime.getMinutes()).padStart(2, '0');
          estCompletion = `${estHours.toFixed(1)}h (at ${hh}:${mm})`;
        }
      } else if (Math.abs(socTarget - socCurr) < 0.5) {
        estCompletion = 'Target SOC Met';
      } else {
        estCompletion = 'Static / Balance';
      }
      
      // Factoring ambient temperature & battery heater
      const ambientTemp = gw.ambient_temp != null ? Number(gw.ambient_temp) : null;
      const heaterOn = !!gw.battery_heater_state;
      let performanceStatus = 'optimal'; // optimal | warning | critical
      let performanceReason = '';
      
      if (heaterOn) {
        performanceStatus = 'warning';
        performanceReason = 'Battery Heater Active (reduced efficiency)';
      } else if (ambientTemp !== null) {
        if (ambientTemp < 0 || ambientTemp > 50) {
          performanceStatus = 'critical';
          performanceReason = `Extreme Ambient Temp: ${ambientTemp.toFixed(1)}°C`;
        } else if (ambientTemp < 10 || ambientTemp > 40) {
          performanceStatus = 'warning';
          performanceReason = `Sub-optimal Ambient Temp: ${ambientTemp.toFixed(1)}°C`;
        }
      }

      return {
        socCurr,
        socTarget,
        solarKw: solarKw.toFixed(2),
        loadKw: loadKw.toFixed(2),
        batteryKw: batteryKw.toFixed(2),
        gridKw: gridKw.toFixed(2),
        estCompletion,
        energyGap: energyGap.toFixed(1),
        ambientTemp: ambientTemp !== null ? ambientTemp.toFixed(1) : null,
        heaterOn,
        performanceStatus,
        performanceReason
      };
    },

    get optimalActions() {
      const gw = Alpine.store('app').currentGateway;
      if (!gw) return [];
      
      const liveImport = this.decision?.import_c_kwh != null ? this.decision.import_c_kwh : (this.forecastPrice?.import_c_kwh || 0);
      const liveExport = this.decision?.export_c_kwh != null ? this.decision.export_c_kwh : (this.forecastPrice?.export_c_kwh || 0);
      
      const solarKw = gw.solar_kw || 0;
      const loadKw = gw.home_kw || 0;
      const batteryCount = gw.battery_count || 1;
      const invLimit = batteryCount * 5.0;
      
      const forecast = this.forecastList || [];
      let futurePeakExport = 0;
      let futureAvgImport = liveImport;
      
      if (forecast.length > 0) {
        const exports = forecast.map(p => p.export_c_kwh || 0);
        futurePeakExport = Math.max(...exports);
        
        const imports = forecast.map(p => p.import_c_kwh || 0);
        futureAvgImport = imports.reduce((a, b) => a + b, 0) / imports.length;
      }
      
      const rtEff = 0.90;
      
      const chargeMoi = (futurePeakExport * rtEff) - liveImport;
      const exportMoi = liveExport - (futureAvgImport / rtEff);
      const solarMoi = liveExport - (futurePeakExport * 0.95);
      
      const actions = [
        {
          name: 'Grid Charge',
          moi: chargeMoi,
          scoreLabel: chargeMoi > 0 ? `+${chargeMoi.toFixed(1)}¢` : `${chargeMoi.toFixed(1)}¢`,
          status: chargeMoi > 5 ? 'highlyOptimal' : (chargeMoi > 0 ? 'optimal' : 'avoid'),
          desc: chargeMoi > 0 ? 'Charging beats future peak export yield' : 'Grid price exceeds future peak yield',
          limit: `${invLimit.toFixed(1)} kW inverter bound`
        },
        {
          name: 'Battery Export',
          moi: exportMoi,
          scoreLabel: exportMoi > 0 ? `+${exportMoi.toFixed(1)}¢` : `${exportMoi.toFixed(1)}¢`,
          status: exportMoi > 10 ? 'highlyOptimal' : (exportMoi > 0 ? 'optimal' : 'avoid'),
          desc: exportMoi > 0 ? 'Dynamic premium rate: export to grid' : 'Hold energy: offsets future peak import',
          limit: `${Math.min(invLimit, Math.max(0, 15.0 - solarKw + loadKw)).toFixed(1)} kW thermal limit`
        },
        {
          name: 'Solar Direct Grid',
          moi: solarMoi,
          scoreLabel: solarMoi > 0 ? `+${solarMoi.toFixed(1)}¢` : `${solarMoi.toFixed(1)}¢`,
          status: solarMoi > 0 ? 'optimal' : 'avoid',
          desc: solarMoi > 0 ? 'Premium Feed-in active: export solar direct' : 'Charge battery: store for future load offset',
          limit: `${solarKw.toFixed(1)} kW solar output`
        }
      ];
      
      return actions.sort((a, b) => b.moi - a.moi);
    },

    get projectedRevenue() {
      const gw = Alpine.store('app').currentGateway;
      if (!gw) return null;
      
      const socCurr = gw.battery_soc || 0;
      const batteryCount = gw.battery_count || 1;
      const nomCapacity = batteryCount * 13.6;
      
      const minSoc = this.decision?.min_soc_limit || 20;
      const usableSoc = Math.max(0, socCurr - minSoc);
      const usableKwh = (usableSoc * nomCapacity) / 100.0;
      
      const invLimit = batteryCount * 5.0;
      
      const forecast = [...(this.forecastList || [])];
      if (forecast.length === 0) return { totalEarn: '0.00', blocks: [] };
      
      forecast.sort((a, b) => (b.export_c_kwh || 0) - (a.export_c_kwh || 0));
      
      let remainingKwh = usableKwh;
      let totalEarn = 0;
      const activeBlocks = [];
      
      for (const slot of forecast) {
        if (remainingKwh <= 0) break;
        
        const durationH = (slot.interval_min || 30) / 60.0;
        const maxExportInSlot = invLimit * durationH;
        const actualExport = Math.min(remainingKwh, maxExportInSlot);
        
        const price = slot.export_c_kwh || 0;
        const earn = (actualExport * price) / 100.0;
        
        totalEarn += earn;
        remainingKwh -= actualExport;
        
        const startD = new Date(slot.start);
        const endD = new Date(slot.end);
        const startStr = `${String(startD.getHours()).padStart(2, '0')}:${String(startD.getMinutes()).padStart(2, '0')}`;
        const endStr = `${String(endD.getHours()).padStart(2, '0')}:${String(endD.getMinutes()).padStart(2, '0')}`;
        
        activeBlocks.push({
          timeRange: `${startStr} - ${endStr}`,
          exportKwh: actualExport.toFixed(1),
          price: price.toFixed(1),
          earn: earn.toFixed(2)
        });
      }
      
      return {
        totalEarn: totalEarn.toFixed(2),
        socBefore: socCurr.toFixed(0),
        socAfter: minSoc.toFixed(0),
        blocks: activeBlocks.slice(0, 3)
      };
    },

    get chargingCostForecast() {
      const gw = Alpine.store('app').currentGateway;
      if (!gw) return null;
      
      const batteryCount = gw.battery_count || 1;
      const nomCapacity = batteryCount * 13.6;
      const incrementKwh = nomCapacity * 0.10;
      const eff = 0.95;
      
      const liveImport = this.decision?.import_c_kwh != null ? this.decision.import_c_kwh : (this.forecastPrice?.import_c_kwh || 0);
      const costImmediate = (incrementKwh / eff) * liveImport / 100.0;
      
      const forecast = this.forecastList || [];
      if (forecast.length === 0) {
        return {
          costImmediate: costImmediate.toFixed(2),
          immediatePrice: liveImport.toFixed(1),
          bestCost: '—',
          bestPrice: '—',
          bestTime: '—',
          worstCost: '—',
          worstPrice: '—',
          worstTime: '—'
        };
      }
      
      let bestSlot = forecast[0];
      let worstSlot = forecast[0];
      
      for (const slot of forecast) {
        if (slot.import_c_kwh < bestSlot.import_c_kwh) bestSlot = slot;
        if (slot.import_c_kwh > worstSlot.import_c_kwh) worstSlot = slot;
      }
      
      const bestCost = (incrementKwh / eff) * bestSlot.import_c_kwh / 100.0;
      const worstCost = (incrementKwh / eff) * worstSlot.import_c_kwh / 100.0;
      
      const formatTime = (isoStr) => {
        const d = new Date(isoStr);
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      };
      
      return {
        costImmediate: costImmediate.toFixed(2),
        immediatePrice: liveImport.toFixed(1),
        
        bestCost: bestCost.toFixed(2),
        bestPrice: bestSlot.import_c_kwh.toFixed(1),
        bestTime: `${formatTime(bestSlot.start)} - ${formatTime(bestSlot.end)}`,
        
        worstCost: worstCost.toFixed(2),
        worstPrice: worstSlot.import_c_kwh.toFixed(1),
        worstTime: `${formatTime(worstSlot.start)} - ${formatTime(worstSlot.end)}`
      };
    },

    // A price spike means importing is unusually expensive right now. Two
    // things were wrong here.
    //
    // The threshold was a hardcoded 20c, so any tariff whose *off-peak* rate
    // exceeds 20c — which is most Australian plans now — showed a permanent
    // red spike warning. An alarm that is always on is not an alarm.
    //
    // And it tested the export rate too, via Math.max(import, export). A high
    // feed-in tariff is an opportunity, not a hazard: this site's 28c evening
    // export was being announced as a wholesale price spike.
    //
    // A spike is now judged against the tariff's own range — materially above
    // the cheapest import it offers — so a flat tariff never spikes and a
    // peak/off-peak plan flags only its peak.
    SPIKE_MULTIPLE: 1.5,

    get _importRange() {
      const rates = (this.forecastList || [])
        .map(p => Number(p.import_c_kwh) || 0)
        .filter(v => v > 0);
      if (!rates.length) return null;
      return { min: Math.min(...rates), max: Math.max(...rates) };
    },

    get _liveImport() {
      return this.decision?.import_c_kwh != null
        ? Number(this.decision.import_c_kwh)
        : Number(this.forecastPrice?.import_c_kwh || 0);
    },

    get priceSpikeActive() {
      const range = this._importRange;
      if (!range) return false;
      const threshold = range.min * this.SPIKE_MULTIPLE;
      if (range.max < threshold) return false;          // flat tariff: no spike
      return this._liveImport >= threshold
          || (this.forecastList || []).some(p => (Number(p.import_c_kwh) || 0) >= threshold);
    },

    get priceSpikeInfo() {
      const range = this._importRange;
      if (!range) return '';
      const threshold = range.min * this.SPIKE_MULTIPLE;

      let peak = this._liveImport >= threshold ? this._liveImport : 0;
      let peakTime = peak ? 'now' : '';

      for (const slot of (this.forecastList || [])) {
        const rate = Number(slot.import_c_kwh) || 0;
        if (rate >= threshold && rate > peak) {
          peak = rate;
          // Local time. slot.start is UTC; getHours() renders it in the
          // browser's zone, which is what the reader's clock shows.
          const d = new Date(slot.start);
          peakTime = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        }
      }

      if (!peak) return '';
      return `Import reaches ${peak.toFixed(1)}¢/kWh at ${peakTime} — ${(peak / range.min).toFixed(1)}x your cheapest rate`;
    },

    get demandWindowActive() {
      // Import only, for the same reason as the spike above: a high export
      // price is not a demand event.
      if (this._liveImport > 50) return true;
      
      const forecast = this.forecastList || [];
      return forecast.some(p => (p.import_c_kwh || 0) > 50 || (p.export_c_kwh || 0) > 50);
    },

    get demandWindowInfo() {
      const liveImport = this.decision?.import_c_kwh != null ? this.decision.import_c_kwh : (this.forecastPrice?.import_c_kwh || 0);
      const liveExport = this.decision?.export_c_kwh != null ? this.decision.export_c_kwh : (this.forecastPrice?.export_c_kwh || 0);
      
      let maxRate = Math.max(liveImport, liveExport);
      let peakTime = 'Now';
      
      const forecast = this.forecastList || [];
      for (const slot of forecast) {
        const rate = Math.max(slot.import_c_kwh || 0, slot.export_c_kwh || 0);
        if (rate > maxRate) {
          maxRate = rate;
          const d = new Date(slot.start);
          peakTime = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        }
      }
      
      if (maxRate > 50) {
        return `High-tariff period of ${maxRate.toFixed(1)}¢/kWh at ${peakTime}`;
      }
      return '';
    },
  };
}

// ── sdEngineConfig — Engine Parameters (Smart Dispatch sub-tab) ──────────
function sdEngineConfig() {
  return {
    cfg: {
      strategy_mode:       'auto',
      min_soc:             20,
      max_soc:             90,
      max_charge_price          : 0,
      min_export_price          : 0,
      export_bonus_threshold    : 5.0,
      charge_power_mode         : 'default',
      charge_power_value        : 0.0,
      discharge_power_mode      : 'default',
      discharge_power_value     : 0.0,
      daily_earnings_target     : 0,
      monthly_earnings_target:   0,
      solar_curtail_entity:           '',
      allow_auto_offgrid:        0,
      notification_mode:         'ask',
      notify_on_demand_charge:   1,
      notify_on_negative_export: 1,
      notify_on_spike:           1,
      notify_on_export_bonus:    0,
      notify_on_earnings:        0,
      notify_on_force_charge:    1,
      info_notify_targets:       '',
      shadow_mode:               0,
      weather_extreme_impact:    0,
      site_has_high_loads:       0,
      multi_utility_service:     0,
      utility_export_limit_w:    0,
      has_apbox_excess_solar:    0,
      apower_s_mppt:             0,
      strategy_priorities_json:  '["self_consumption", "peak_shaving", "export_exception", "battery_topup"]',
      default_operating_mode:    'gateway_default',
      rampTime:                  99,
      maxChargeSoc:              100,
      minDischargeSoc:           0,
      chargePower:               5000,
      dischargePower:            5000,
    },
    prioritiesList: ['self_consumption', 'peak_shaving', 'export_exception', 'battery_topup'],
    movePriorityUp(idx) {
      if (idx <= 0) return;
      const arr = [...this.prioritiesList];
      const tmp = arr[idx];
      arr[idx] = arr[idx - 1];
      arr[idx - 1] = tmp;
      this.prioritiesList = arr;
      this.cfg.strategy_priorities_json = JSON.stringify(arr);
    },
    movePriorityDown(idx) {
      if (idx >= this.prioritiesList.length - 1) return;
      const arr = [...this.prioritiesList];
      const tmp = arr[idx];
      arr[idx] = arr[idx + 1];
      arr[idx + 1] = tmp;
      this.prioritiesList = arr;
      this.cfg.strategy_priorities_json = JSON.stringify(arr);
    },
    earnings:  null,
    loading:   true,
    saving:    false,
    saved:     false,
    saveError: null,
    showSettingsModal: false,
    siteSolarKwp: 0,
    
    configScope: 'gateway',
    schedules: [],
    scheduleLoading: false,
    newSchedule: {
      period: 'everyday',
      start_time: '01:00',
      end_time: '05:00',
      action: 'CHARGE',
      min_soc: 50,
      max_kw_percent: 100
    },

    async init() {
      const gws = Alpine.store('app').gateways || [];
      if (gws.length <= 1) {
        this.configScope = 'global';
      } else {
        this.configScope = 'gateway';
      }

      // Reactively set scope when gateways load asynchronously
      this.$watch('$store.app.gateways', (val) => {
        const list = val || [];
        if (list.length <= 1) {
          this.configScope = 'global';
        } else if (this.configScope === 'global') {
          this.configScope = 'gateway';
        }
      });

      // Reactively reload configs when selected gateway changes
      this.$watch('$store.app.selectedGateway', async (val) => {
        await Promise.all([this.loadConfig(), this.loadEarnings(), this.loadSchedules()]);
      });

      await Promise.all([this.loadConfig(), this.loadEarnings(), this.loadSchedules()]);
    },

    get activeTargetId() {
      return this.configScope === 'global' ? 'global' : Alpine.store('app').selectedGateway;
    },

    async setConfigScope(scope) {
      this.configScope = scope;
      await Promise.all([this.loadConfig(), this.loadSchedules()]);
    },

    async loadConfig() {
      if (!this.activeTargetId) return;
      this.loading = true;
      try {
        const r = await fetch(`api/smart_dispatch/config?gateway_id=${this.activeTargetId}`);
        const d = await r.json();
        if (d?.ok && d.config) {
          this.cfg = { ...this.cfg, ...d.config };
          if (this.cfg.strategy_priorities_json) {
            try {
              this.prioritiesList = JSON.parse(this.cfg.strategy_priorities_json);
            } catch(e) {
              this.prioritiesList = ['self_consumption', 'peak_shaving', 'export_exception', 'battery_topup'];
            }
          }
        }
        
        // Retrieve site solar capacity
        const solarR = await fetch('api/solar/config');
        const solarD = await solarR.json();
        if (solarD?.ok && solarD.config) {
          this.siteSolarKwp = solarD.config.kwp || 0.0;
        }
      } catch(e) { console.warn('sdEngineConfig: load failed', e); }
      finally { this.loading = false; }
    },

    async loadEarnings() {
      try {
        const r = await fetch(`api/smart_dispatch/earnings?gateway_id=${Alpine.store('app').selectedGateway}`);
        const d = await r.json();
        if (d?.ok) this.earnings = d;
      } catch(e) { /* non-critical */ }
    },
    
    async loadSchedules() {
      if (!this.activeTargetId) return;
      this.scheduleLoading = true;
      try {
        const r = await fetch(`api/smart_dispatch/schedules?gateway_id=${this.activeTargetId}`);
        const d = await r.json();
        if (d?.ok) this.schedules = d.schedules || [];
      } catch(e) { console.warn('sdEngineConfig: load schedules failed', e); }
      finally { this.scheduleLoading = false; }
    },
    
    async addSchedule() {
      this.scheduleLoading = true;
      let payloadPeriod = this.newSchedule.period;
      if (payloadPeriod === 'day_of_month' && this.newSchedule.dayOfMonth) {
        const d = this.newSchedule.dayOfMonth;
        const s = ["th", "st", "nd", "rd"];
        const v = d % 100;
        const suffix = (s[(v - 20) % 10] || s[v] || s[0]);
        payloadPeriod = `${d}${suffix} of month`;
      }
      
      try {
        const r = await fetch('api/smart_dispatch/schedules', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            gateway_id: this.activeTargetId,
            period: payloadPeriod,
            start_time: this.newSchedule.start_time,
            end_time: this.newSchedule.end_time,
            action: this.newSchedule.action,
            min_soc: this.newSchedule.min_soc,
            max_kw_percent: this.newSchedule.max_kw_percent
          })
        });
        if (r.ok) {
          await this.loadSchedules();
        }
      } catch(e) { console.warn('Add schedule failed', e); }
      finally { this.scheduleLoading = false; }
    },
    
    scheduleToDelete: null,
    
    confirmDeleteSchedule(id) {
      this.scheduleToDelete = id;
    },
    
    cancelDeleteSchedule() {
      this.scheduleToDelete = null;
    },
    
    async executeDeleteSchedule() {
      if (!this.scheduleToDelete) return;
      const id = this.scheduleToDelete;
      this.scheduleToDelete = null;
      try {
        await fetch(`api/smart_dispatch/schedules/${id}`, { method: 'DELETE' });
        await this.loadSchedules();
      } catch(e) { console.warn('Delete schedule failed', e); }
    },

    async save() {
      this.saving = true;
      this.saved = false;
      this.saveError = null;
      this.cfg.strategy_priorities_json = JSON.stringify(this.prioritiesList);
      try {
        const r = await fetch(`api/smart_dispatch/config?gateway_id=${this.activeTargetId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.cfg),
        });
        const d = await r.json();
        if (d?.ok) {
          this.saved = true;
          setTimeout(() => { this.saved = false; }, 3000);
          this.$store?.app?.addToast?.('Engine parameters saved', 'ok');
          
          // Sync shadow_mode to global store
          if (this.$store?.app && this.cfg.shadow_mode !== undefined) {
            this.$store.app.shadowModeActive = !!this.cfg.shadow_mode;
          }
        } else {
          // FastAPI validation errors return detail as an array [{loc, msg, type}]
          const raw = d?.detail ?? d?.error ?? 'Save failed';
          this.saveError = Array.isArray(raw)
            ? raw.map(e => e.msg || JSON.stringify(e)).join('; ')
            : (typeof raw === 'object' ? JSON.stringify(raw) : String(raw));
        }
      } catch(e) {
        this.saveError = 'Network error — check connection';
      } finally {
        this.saving = false;
      }
    },

    get billingConfigured() { return this.earnings?.is_billing_configured || false; },
    get dailyEarnings()  { return this.earnings?.daily_earnings_aud  != null ? `$${this.earnings.daily_earnings_aud.toFixed(2)}`  : '—'; },
    get monthlyEarnings(){ return this.earnings?.monthly_earnings_aud != null ? `$${this.earnings.monthly_earnings_aud.toFixed(2)}` : '—'; },
    get billingCycleLabel() {
      if (!this.billingConfigured) return 'Not configured — set in Pricing tab first';
      const s = this.earnings?.billing_cycle_start;
      if (!s) return 'Billing configured';
      const d = new Date(s + 'T00:00:00');
      return `Current cycle from ${d.toLocaleDateString('en-AU', { day:'numeric', month:'short' })}`;
    },
  };
}

// ── sdForecastMap — 24h Decision Timeline (Phase B) ─────────────────────
// Fetches /api/amber/engine/forecast and renders a 48-slot colour-coded strip
// showing which engine rule would fire for each 30-min Amber interval.
function sdForecastMap() {
  return {
    // ── 24h Engine tab state ──────────────────────────────────────────────
    slots:               [],
    epochSegments:       [],
    expandedEpochSegment: 0, // expand first segment by default
    toggleEpochSegment(idx) {
      this.expandedEpochSegment = this.expandedEpochSegment === idx ? null : idx;
    },
    siteSolarKwp:        0,    // nameplate solar kWp — used for Y-axis max scaling
    soc_pct:             null,
    min_soc:             20,
    max_soc:             90,
    backup_reserve_soc:  20,
    native_mode:         'Self-Consumption',
    default_operating_mode: 'gateway_default',
    gatewayCount:        1,
    inverterCapacityKw:  5.0,
    dcCoupledCount:      0,
    acCoupledCount:      0,
    remotePvCount:       0,
    soc_note:            null,
    solar_source:        null,
    utility_name:        null,
    tariff_plan:         null,
    soc_trajectory:      [],
    weatherCurrent:      null,
    weatherForecast:     [],
    loading:             true,
    error:               null,
    hoverSlot:           null,
    _timer:              null,
    _engineVersion:      null,
    _versionUpdated:     false,
    getSegmentWeather(segment) {
      if (!segment || !segment.slots || !segment.slots.length) return null;
      const firstSlot = segment.slots[0];
      const dateObj = new Date(firstSlot.start);
      const dayName = dateObj.toLocaleDateString('en-US', { weekday: 'long' });
      const fcItem = (this.weatherForecast || []).find(item => item.day_name === dayName || item.day === dayName) || null;
      return fcItem;
    },

    getConditionIcon(cond) {
      if (!cond) return 'fa-cloud';
      const c = cond.toLowerCase();
      if (c.includes('clear') || c.includes('sunny')) return 'fa-sun text-amber-400 animate-pulse';
      if (c.includes('rain') || c.includes('drizzle') || c.includes('showers')) return 'fa-cloud-showers-heavy text-blue-400';
      if (c.includes('storm') || c.includes('thunder')) return 'fa-cloud-bolt text-purple-400 animate-bounce';
      if (c.includes('snow') || c.includes('ice') || c.includes('freeze')) return 'fa-snowflake text-sky-200';
      if (c.includes('cloud') || c.includes('overcast')) return 'fa-cloud text-gray-400';
      return 'fa-cloud-sun text-amber-300';
    },

    // ── Capability Adaptability state ──────────────────────────────────────
    isSolarActive:           true,
    isDynamicPricing:        true,
    weatherInfluenceEnabled: true,
    siteProfile:             'hems',

    // ── Sub-tab state ─────────────────────────────────────────────────────
    forecastTab: 'engine',   // 'engine' | 'solar' | 'tou'
    showExplain: false,      // toggles markdown-like engine trace below charts

    // ── Solar Forecast sub-tab state ─────────────────────────────────────
    solarData:         null,
    solarLoading:      false,
    solarError:        null,
    _solarFetched:     false,  // lazy — only fetch once until force-refresh
    sdSolarViewMode:   'forecast',  // 'forecast' | 'centred'
    sdPastHours:       12,
    sdFutureHours:     24,
    sdActualsData:     null,
    sdActualsLoading:  false,

    // ── TOU Schedule sub-tab state ────────────────────────────────────────
    touPeriods:        [],
    touLoading:        false,
    touError:          null,
    _touFetched:       false,
    touIsMulti:        false,
    touActiveSeasonIdx: 0,
    touActiveDayType:  1,

    CATEGORY_COLOR: {
      demand_charge:            { bg: '#6d28d9', label: '⚡ Demand',   text: '#ddd6fe' },
      negative_export:          { bg: '#b91c1c', label: '📉 Neg Export', text: '#fca5a5' },
      negative_export_advisory: { bg: '#475569', label: '⚠️ Neg Exp',   text: '#94a3b8' },
      price_spike:              { bg: '#c2410c', label: '🔺 Spike',    text: '#fed7aa' },
      export_bonus:             { bg: '#92400e', label: '☀ Bonus',    text: '#fde68a' },
      earnings_target:          { bg: '#166534', label: '💰 Earn',     text: '#86efac' },
      force_charge:             { bg: '#0f766e', label: '🔋 Charge',   text: '#99f6e4' },
      legacy_rule:              { bg: '#1e40af', label: '📋 Rule',     text: '#bfdbfe' },
      fallback:                 { bg: '#134e4a', label: '🔄 SC',      text: '#5eead4' },
    },

    async init() {
      if (this._timer) clearInterval(this._timer);
      this.$watch('forecastTab', (val) => {
        this.$nextTick(() => {
          if (val === 'engine') this.renderChart();
          if (val === 'tou') this.renderTouChart();
        });
      });
      // Forecast + solar + weather are now ON-DEMAND only — fire once on
      // SD-tab open, then re-fire only on:
      //   - explicit user click of the Refresh button
      //   - selectedGateway change ($watch in the x-init)
      //   - tab switch to a different sub-tab (engine/solar/tou)
      // No more setInterval — the SD forecast endpoint takes ~20s with the
      // current per-period eval cost, so background polling every 5 min was
      // saturating the browser connection pool and triggering the
      // Connection-Lost banner. Per user directive (2026-06-17): forecasts
      // should be cached server-side and only re-evaluated on demand or once
      // per day. Tracked separately as task #2.
      this.loadSolarForecast().catch(() => {});
      await this.refresh();
    },

    async refresh(force = false) {
      const gwId = Alpine.store('app').selectedGateway;
      if (!gwId) return;
      this.loading = true;
      this.error = null;
      try {
        // Server caches the response 10 min by default; explicit Refresh
        // button click passes force=true to bypass and re-evaluate.
        const url = `api/smart_dispatch/forecast?gateway_id=${gwId}` + (force ? '&force=true' : '');
        const r = await fetch(url);
        const d = await r.json();
        if (!d.ok) { this.error = d.error || 'Forecast unavailable'; return; }
        
        const newVer = d.forecast_engine_version || null;
        if (newVer && this._engineVersion && newVer !== this._engineVersion) {
          console.info(`[AmberForecast] Engine version changed ${this._engineVersion} → ${newVer}. Colours updated.`);
          this._versionUpdated = true;
          setTimeout(() => { this._versionUpdated = false; }, 4000);
        }
        this._engineVersion  = newVer;
        this.slots          = d.slots || [];
        this.epochSegments  = d.epoch_segments || [];
        this.siteSolarKwp   = d.site_solar_kwp || 0;
        this.soc_pct        = d.soc_pct;
        this.min_soc        = d.min_soc !== undefined ? d.min_soc : 20;
        this.max_soc        = d.max_soc !== undefined ? d.max_soc : 90;
        this.backup_reserve_soc = d.backup_reserve_soc !== undefined ? d.backup_reserve_soc : 20;
        this.native_mode    = d.native_mode || 'Self-Consumption';
        this.default_operating_mode = d.default_operating_mode || 'gateway_default';
        this.grid_connected = d.grid_connected;
        this.grid_relay     = d.grid_relay;
        this.generator_relay= d.generator_relay;
        this.solar_relay_1  = d.solar_relay_1;
        this.solar_relay_2  = d.solar_relay_2;
        this.soc_note       = d.soc_note || null;
        this.solar_source   = d.solar_source || null;
        this.utility_name   = d.utility_name || null;
        this.tariff_plan    = d.tariff_plan || null;
        this.soc_trajectory = d.soc_trajectory || [];
        
        // Metadata Bindings
        this.gatewayCount       = d.gateway_count || 1;
        this.inverterCapacityKw = d.inverter_capacity_kw || 5.0;
        this.dcCoupledCount     = d.dc_coupled_count || 0;
        this.acCoupledCount     = d.ac_coupled_count || 0;
        this.remotePvCount      = d.remote_pv_count || 0;
        
        // Capability Bindings
        this.isSolarActive    = d.is_solar_active !== undefined ? d.is_solar_active : true;
        this.isDynamicPricing = d.is_dynamic_pricing !== undefined ? d.is_dynamic_pricing : true;
        this.weatherInfluenceEnabled = d.weather_influence_enabled !== undefined ? d.weather_influence_enabled : true;
        this.siteProfile      = d.site_profile || 'hems';

        if (window.Alpine) {
          const app = Alpine.store('app');
          app.isSolarActive = this.isSolarActive;
          app.isDynamicPricing = this.isDynamicPricing;
          app.weatherInfluenceEnabled = this.weatherInfluenceEnabled;
          app.siteProfile = this.siteProfile;
        }

        // Fetch weather forecast & current in background
        fetch('api/weather/forecast')
          .then(res => res.json())
          .then(wd => { if (wd.ok) this.weatherForecast = wd.forecast || []; })
          .catch(err => console.warn('Failed to load weather forecast', err));

        fetch('api/weather/current')
          .then(res => res.json())
          .then(wcd => { if (wcd.ok) this.weatherCurrent = wcd.data || null; })
          .catch(err => console.warn('Failed to load current weather', err));
        
        // Render chart next tick so canvas is available
        this.$nextTick(() => { 
          if (this.forecastTab === 'engine') this.renderChart(); 
          if (this.forecastTab === 'tou') this.renderTouChart();
        });
      } catch(e) {
        this.error = 'Could not load forecast';
      } finally {
        this.loading = false;
      }
    },

    renderChart() {
      if (!this.slots.length) return;
      
      const ctx = document.getElementById('forecastChart');
      if (!ctx) return;

      const existingChart = Chart.getChart(ctx);
      if (existingChart) {
        existingChart.destroy();
      }

      const labels = this.slots.map(s => {
        const d = new Date(s.start);
        return d.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
      });

      const gw = Alpine.store('app').currentGateway;
      const batteryKw  = (gw && gw.battery_count ? gw.battery_count : 1) * 5.0;
      const solarKw    = this.siteSolarKwp * 0.85;  // 85% of nameplate — realistic peak
      const maxFlow    = Math.ceil(Math.max(batteryKw, solarKw) / 5) * 5 || 5;

      // Background color for each mode block
      const bgColors = this.slots.map(s => {
        return (this.CATEGORY_COLOR[s.trigger_category] || this.CATEGORY_COLOR.fallback).bg + '33'; // 20% opacity
      });

      // Power Data
      const solarData = this.slots.map(s => s.pv_kw || 0);
      const chargeData = this.slots.map(s => s.charge_kw || 0);
      const dischargeData = this.slots.map(s => -(s.discharge_kw || 0));
      const homeLoadData = this.slots.map(s => -(s.home_load_kw || 0));
      
      // SOC Data
      const socData = this.slots.map(s => s.projected_soc != null ? s.projected_soc : null);
      const scheduledSocData = this.slots.map(s => s.scheduled_soc != null ? s.scheduled_soc : null);

      // Sunrise / sunset / peak dashed lines from solarData if available
      const annotations = {};
      const solarSlots = this.solarData?.slots || [];
      const todayStr   = new Date().toLocaleDateString('en-AU');
      const todaySlots = solarSlots.filter(s => new Date(s.timestamp).toLocaleDateString('en-AU') === todayStr);
      const srSlot     = todaySlots.find(s => (s.pv_kw || 0) > 0.05);
      const ssSlot     = [...todaySlots].reverse().find(s => (s.pv_kw || 0) > 0.05);
      if (srSlot) {
        const srTime = new Date(srSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const srIdx  = labels.indexOf(srTime);
        if (srIdx >= 0) {
          annotations.sunrise = {
            type: 'line', scaleID: 'x', value: srIdx,
            borderColor: 'rgba(251,191,36,0.5)', borderWidth: 1, borderDash: [4,3],
            label: { display: true, content: 'Sunrise', position: 'start',
                     color: '#fbbf24', font: { size: 8 }, padding: 3 },
          };
        }
      }
      if (ssSlot) {
        const ssTime = new Date(ssSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const ssIdx  = labels.indexOf(ssTime);
        if (ssIdx >= 0) {
          annotations.sunset = {
            type: 'line', scaleID: 'x', value: ssIdx,
            borderColor: 'rgba(251,146,60,0.5)', borderWidth: 1, borderDash: [4,3],
            label: { display: true, content: 'Sunset', position: 'start',
                     color: '#fb923c', font: { size: 8 }, padding: 3 },
          };
        }
      }
      const peakSlot = [...todaySlots].sort((a,b) => b.pv_kw - a.pv_kw)[0];
      if (peakSlot && peakSlot.pv_kw > 0.1) {
        const peakTime = new Date(peakSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const peakIdx  = labels.indexOf(peakTime);
        if (peakIdx >= 0) {
          annotations.peak = {
            type: 'line', scaleID: 'x', value: peakIdx,
            borderColor: 'rgba(251,191,36,0.8)', borderWidth: 1, borderDash: [2,2],
            label: { display: true, content: 'Solar Peak', position: 'start',
                     color: '#fcd34d', font: { size: 8 }, padding: 3 },
          };
        }
      }

      // Add Min/Max/Current SOC horizontal dotted annotations
      if (this.min_soc != null) {
        annotations.minSoc = {
          type: 'line',
          scaleID: 'ySoc',
          value: this.min_soc,
          borderColor: 'rgba(239, 68, 68, 0.7)',
          borderWidth: 1.5,
          borderDash: [4, 4],
          label: {
            display: true,
            content: `Min SOC (${this.min_soc}%)`,
            position: 'end',
            color: '#ef4444',
            font: { size: 9, weight: 'bold' },
            padding: 4,
            backgroundColor: 'rgba(15, 23, 42, 0.8)'
          }
        };
      }

      if (this.max_soc != null) {
        annotations.maxSoc = {
          type: 'line',
          scaleID: 'ySoc',
          value: this.max_soc,
          borderColor: 'rgba(16, 185, 129, 0.7)',
          borderWidth: 1.5,
          borderDash: [4, 4],
          label: {
            display: true,
            content: `Max SOC (${this.max_soc}%)`,
            position: 'end',
            color: '#10b981',
            font: { size: 9, weight: 'bold' },
            padding: 4,
            backgroundColor: 'rgba(15, 23, 42, 0.8)'
          }
        };
      }

      if (this.soc_pct != null) {
        annotations.currentSoc = {
          type: 'line',
          scaleID: 'ySoc',
          value: this.soc_pct,
          borderColor: 'rgba(245, 158, 11, 0.8)',
          borderWidth: 2,
          borderDash: [2, 3],
          label: {
            display: true,
            content: `Current SOC (${this.soc_pct}%)`,
            position: 'start',
            color: '#f59e0b',
            font: { size: 9, weight: 'bold' },
            padding: 4,
            backgroundColor: 'rgba(15, 23, 42, 0.8)'
          }
        };
      }

      const datasets = [
        {
          type: 'line',
          label: 'SOC %',
          data: socData,
          borderColor: '#60a5fa',
          backgroundColor: '#60a5fa',
          borderWidth: 3,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.4,
          yAxisID: 'ySoc',
          order: 0 // Draw on top
        },
        {
          type: 'line',
          label: 'Schedule SOC %',
          data: scheduledSocData,
          borderColor: '#f59e0b',
          backgroundColor: 'transparent',
          borderWidth: 2,
          borderDash: [6, 4],
          pointRadius: 0,
          pointHoverRadius: 4,
          stepped: 'before',
          yAxisID: 'ySoc',
          order: 0.5
        }
      ];

      if (this.isSolarActive) {
        datasets.push({
          type: 'bar',
          label: 'Solar PV',
          data: solarData,
          backgroundColor: '#fcd34d',
          yAxisID: 'yKw',
          stack: 'PowerStack',
          order: 1
        });
      }

      datasets.push(
        {
          type: 'bar',
          label: 'Battery Charge',
          data: chargeData,
          backgroundColor: '#34d399',
          yAxisID: 'yKw',
          stack: 'PowerStack',
          order: 2
        },
        {
          type: 'bar',
          label: 'Battery Discharge',
          data: dischargeData,
          backgroundColor: '#f87171',
          yAxisID: 'yKw',
          stack: 'PowerStack',
          order: 3
        },
        {
          type: 'bar',
          label: 'Home Load',
          data: homeLoadData,
          backgroundColor: '#8b5cf6', // Indigo/Purple for home to differentiate from blue SOC
          yAxisID: 'yKw',
          stack: 'PowerStack',
          order: 4
        }
      );

      new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: datasets
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false,
          },
          plugins: {
            annotation: { annotations },
            legend: {
              display: false
            },
            tooltip: {
              backgroundColor: 'rgba(15, 23, 42, 0.95)',
              titleColor: '#e2e8f0',
              bodyColor: '#cbd5e1',
              borderColor: '#334155',
              borderWidth: 1,
              padding: 12,
              callbacks: {
                title: (ctxs) => {
                  if (!ctxs.length) return '';
                  const idx = ctxs[0].dataIndex;
                  const slot = this.slots[idx];
                  if (!slot) return labels[idx];
                  
                  const d = new Date(slot.start);
                  const dateStr = d.toLocaleDateString('en-AU', { weekday: 'short', day: 'numeric', month: 'short' });
                  const d2 = new Date(slot.end);
                  const endStr = d2.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
                  
                  return `${labels[idx]} – ${endStr} (${dateStr})`;
                },
                beforeBody: (ctxs) => {
                  if (!ctxs.length) return '';
                  const slot = this.slots[ctxs[0].dataIndex];
                  if (!slot) return '';
                  const modeLabel = (this.CATEGORY_COLOR[slot.trigger_category] || this.CATEGORY_COLOR.fallback).label;
                  let body = `Rule: ${modeLabel} | Action: ${slot.action || '—'}`;
                  if (slot.dispatch_summary) body += `\nReason: ${slot.dispatch_summary}`;
                  return body;
                },
                label: (ctx) => {
                  if (ctx.dataset.label === 'SOC %') {
                    return `SOC: ${ctx.raw.toFixed(1)}%`;
                  }
                  if (ctx.dataset.label === 'Schedule SOC %') {
                    return `Schedule SOC: ${ctx.raw.toFixed(1)}%`;
                  }
                  return `${ctx.dataset.label}: ${Math.abs(ctx.raw).toFixed(2)} kW`;
                },
                afterBody: (ctxs) => {
                  if (!ctxs.length) return '';
                  const slot = this.slots[ctxs[0].dataIndex];
                  if (!slot) return '';
                  return `Import: ${(slot.import_c != null ? slot.import_c.toFixed(1) : '—')}¢ | Export: ${(slot.export_c != null ? slot.export_c.toFixed(1) : '—')}¢`;
                }
              }
            }
          },
          scales: {
            x: {
              stacked: true,
              grid: { display: false },
              ticks: { 
                color: '#94a3b8', 
                maxTicksLimit: 12,
                maxRotation: 0,
                autoSkip: true
              }
            },
            yKw: {
              type: 'linear',
              position: 'left',
              stacked: true,
              min: -maxFlow,
              max: maxFlow,
              grid: { display: false },
              ticks: {
                color: '#94a3b8',
                callback: function(value) { return (value > 0 ? '+' : '') + value + ' kW'; }
              }
            },
            ySoc: {
              type: 'linear',
              position: 'right',
              min: 0,
              max: 100,
              grid: { display: false },
              ticks: {
                color: '#60a5fa',
                callback: function(value) { return value + '%'; }
              }
            }
          }
        }
      });
    },

    get legend() {
      // FHP style legend
      const items = [];
      if (this.isSolarActive) {
        items.push({ label: 'SOLAR', bg: '#fcd34d', text: '#ffffff' });
      }
      items.push(
        { label: 'CHARGE', bg: '#34d399', text: '#ffffff' },
        { label: 'DISCHARGE', bg: '#f87171', text: '#ffffff' },
        { label: 'HOME', bg: '#8b5cf6', text: '#ffffff' },
        { label: 'SOC%', bg: '#60a5fa', text: '#ffffff', border: true },
        { label: 'SCHED SOC', bg: '#f59e0b', text: '#ffffff', border: true }
      );
      return items;
    },

    get engineTimelineBands() {
      if (!this.slots || !this.slots.length) return [];
      
      const bands = [];
      let currentBand = null;
      const N = this.slots.length;
      
      for (let i = 0; i < N; i++) {
        const s = this.slots[i];
        const category = s.trigger_category || 'fallback';
        const action = s.action || 'HOLD';
        const ruleName = s.rule_name || (action === 'HOLD' ? 'Native Mode' : 'Smart Dispatch');
        const start = new Date(s.start);
        const end = new Date(s.end);
        
        const catColor = this.CATEGORY_COLOR[category] || this.CATEGORY_COLOR.fallback;
        
        const bg = `${catColor.bg}1e`;
        const border = `${catColor.bg}55`;
        const text = catColor.bg;
        
        // Track dynamic metrics for list cards
        const socVal = s.projected_soc !== undefined && s.projected_soc !== null ? s.projected_soc : (s.soc_pct || 0);
        const buyVal = s.import_c !== undefined && s.import_c !== null ? s.import_c : 0;
        const sellVal = s.export_c !== undefined && s.export_c !== null ? s.export_c : 0;
        
        const pvVal = s.pv_kw !== undefined ? s.pv_kw : 0.0;
        const loadVal = s.home_load_kw !== undefined ? s.home_load_kw : 0.0;
        const touDisp = s.tou_dispatch_name || 'Self-Consumption';
        
        if (currentBand && currentBand.category === category && currentBand.action === action) {
          currentBand.endIndex = i;
          currentBand.end = end;
          currentBand.socs.push(socVal);
          currentBand.buyPrices.push(buyVal);
          currentBand.sellPrices.push(sellVal);
          currentBand.pvKws.push(pvVal);
          currentBand.homeLoads.push(loadVal);
          currentBand.touDispatches.push(touDisp);
        } else {
          if (currentBand) {
            bands.push(currentBand);
          }
          currentBand = {
            startIndex: i,
            endIndex: i,
            category,
            action,
            ruleName,
            start,
            end,
            bg,
            border,
            text,
            name: catColor.label,
            socs: [socVal],
            buyPrices: [buyVal],
            sellPrices: [sellVal],
            pvKws: [pvVal],
            homeLoads: [loadVal],
            touDispatches: [touDisp]
          };
        }
      }
      if (currentBand) {
        bands.push(currentBand);
      }
      
      const totalSlots = Math.max(N, 1);
      return bands.map(b => {
        const leftPct = (b.startIndex / totalSlots) * 100;
        const widthPct = ((b.endIndex - b.startIndex + 1) / totalSlots) * 100;
        
        const formatTime = (dt) => {
          return dt.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
        };
        
        const startSoc = Math.round(b.socs[0]);
        const endSoc = Math.round(b.socs[b.socs.length - 1]);
        const socRange = startSoc === endSoc ? `${startSoc}%` : `${startSoc}% → ${endSoc}%`;
        
        const avgBuy = b.buyPrices.reduce((s, x) => s + x, 0) / b.buyPrices.length;
        const avgSell = b.sellPrices.reduce((s, x) => s + x, 0) / b.sellPrices.length;
        
        const avgPv = b.pvKws.reduce((s, x) => s + x, 0) / b.pvKws.length;
        const avgLoad = b.homeLoads.reduce((s, x) => s + x, 0) / b.homeLoads.length;
        const touDisp = b.touDispatches[0] || 'Self-Consumption';
        
        // Resolve Weather Forecast
        const dateObj = new Date(b.start);
        const dayName = dateObj.toLocaleDateString('en-US', { weekday: 'long' });
        const fcItem = (this.weatherForecast || []).find(item => item.day_name === dayName || item.day === dayName) || null;
        
        const actionLabels = {
          GRID_CHARGE: 'Charging',
          GRID_EXPORT: 'Discharging',
          STANDBY: 'Standby',
          HOLD: 'Standby'
        };
        const batAction = actionLabels[b.action] || 'Standby';
        
        return {
          leftPct,
          widthPct,
          name: b.name,
          category: b.category,
          action: b.action,
          ruleName: b.ruleName,
          timeLabel: `${formatTime(b.start)} → ${formatTime(b.end)}`,
          timeStart: formatTime(b.start),
          timeEnd: formatTime(b.end),
          bg: b.bg,
          border: b.border,
          text: b.text,
          socRange,
          batAction,
          avgBuy: avgBuy.toFixed(1) + '¢',
          avgSell: avgSell.toFixed(1) + '¢',
          avgPv: avgPv.toFixed(1) + ' kW',
          avgLoad: avgLoad.toFixed(1) + ' kW',
          touDisp,
          weatherIcon: fcItem ? fcItem.icon : 'cloud',
          weatherCond: fcItem ? fcItem.condition : 'Cloudy',
          tempHigh: fcItem ? fcItem.temp_high : null,
          tempLow: fcItem ? fcItem.temp_low : null,
          rainProb: fcItem ? fcItem.rain_probability : 0,
        };
      });
    },

    get engineTimelineTicks() {
      if (!this.slots || !this.slots.length) return [];
      const N = this.slots.length;
      const ticks = [];
      
      for (let i = 0; i <= N; i += 6) {
        const slotIdx = Math.min(i, N - 1);
        const s = this.slots[slotIdx];
        if (!s) continue;
        
        let dt;
        if (i === N) {
          dt = new Date(this.slots[N - 1].end);
        } else {
          dt = new Date(s.start);
        }
        
        const label = dt.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
        const leftPct = (i / N) * 100;
        
        ticks.push({ leftPct, label });
      }
      return ticks;
    },

    // ── Solar Forecast sub-tab methods ────────────────────────────────────

    async loadSolarForecast(force = false) {
      if (this._solarFetched && !force) return;  // lazy — already loaded
      this.solarLoading = true;
      this.solarError = null;
      try {
        const r = await fetch('api/solar/forecast');
        const d = await r.json();
        if (!d.ok) {
          this.solarData = null;
          this.solarError = d.error || 'Solar forecast unavailable';
          return;
        }
        this.solarData = d;
        this._solarFetched = true;
      } catch (e) {
        this.solarError = 'Could not load solar forecast';
      } finally {
        this.solarLoading = false;
      }
    },

    async loadSolarActuals() {
      this.sdActualsLoading = true;
      try {
        const r = await fetch('api/solar/actuals');
        const d = await r.json();
        this.sdActualsData = d.ok ? d : null;
      } catch (e) {
        this.sdActualsData = null;
      } finally {
        this.sdActualsLoading = false;
      }
    },

    // ── Solar computed: day summary cards ────────────────────────────────
    get solarDays() {
      const slots = this.solarData?.slots;
      if (!slots?.length) return [];
      const byDay = {};
      slots.forEach(s => {
        const d   = new Date(s.timestamp);
        const key = d.toLocaleDateString('en-AU');
        const lbl = d.toLocaleDateString('en-AU', { weekday: 'short', day: 'numeric', month: 'short' });
        if (!byDay[key]) byDay[key] = { label: lbl, slots: [] };
        byDay[key].slots.push(s);
      });
      return Object.values(byDay).slice(0, 2).map(day => {
        const maxSlot = day.slots.reduce((m, s) => s.pv_kw > (m?.pv_kw || 0) ? s : m, null);
        return {
          label:      day.label,
          total_kwh:  day.slots.reduce((s, x) => s + (x.pv_wh !== undefined ? x.pv_wh / 1000 : x.pv_kw * 0.5), 0),
          peak_kw:    maxSlot?.pv_kw || 0,
          peak_time:  maxSlot ? new Date(maxSlot.timestamp).toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: true }) : '—',
          slot_count: day.slots.length,
        };
      });
    },

    // ── Solar computed: bar chart ─────────────────────────────────────────
    get sdForecastBars() {
      const slots = this.solarData?.slots;
      if (!slots?.length) return [];
      const maxKw = Math.max(...slots.map(s => s.pv_kw), 0.01);
      const today = new Date().toLocaleDateString('en-AU');
      return slots.map(s => {
        const wh      = s.pv_wh !== undefined ? s.pv_wh : s.pv_kw * 500;
        const slotDay = new Date(s.timestamp).toLocaleDateString('en-AU');
        const pct     = Math.round((s.pv_kw / maxKw) * 100);
        const color   = s.pv_kw === 0 ? '#374151' : slotDay === today ? '#10b981' : '#38bdf8';
        const localT  = new Date(s.timestamp).toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' });
        return { pct, color, isNow: false, label: `${localT} — ${s.pv_kw.toFixed(2)} kW · ${wh.toFixed(0)} Wh` };
      });
    },

    get sdCentredBars() {
      const slots = this.solarData?.slots;
      if (!slots?.length) return [];
      const nowMs   = Date.now();
      const pastMs  = this.sdPastHours  * 3600000;
      const futMs   = this.sdFutureHours * 3600000;
      const slotMs  = 30 * 60000;
      const winStart = nowMs - pastMs - slotMs;
      const winEnd   = nowMs + futMs;
      const windowed = slots.filter(s => {
        const ts = new Date(s.timestamp).getTime();
        return ts >= winStart && ts <= winEnd;
      });
      if (!windowed.length) return [];
      const buckets  = this.sdActualsData?.buckets || [];
      const allKw    = [...windowed.map(s => s.pv_kw), ...buckets.map(b => b.pv_kw || 0)];
      const maxKw    = Math.max(...allKw, 0.01);
      const today    = new Date().toLocaleDateString('en-AU');
      return windowed.map(s => {
        const tsMs    = new Date(s.timestamp).getTime();
        const isPast  = tsMs < nowMs;
        const isNow   = Math.abs(tsMs - nowMs) < slotMs;
        const slotDay = new Date(s.timestamp).toLocaleDateString('en-AU');
        const slotMin = new Date(s.timestamp).getHours() * 60 + new Date(s.timestamp).getMinutes();
        const actualBucket = buckets.find(b => {
          const bt = new Date(b.timestamp);
          return bt.getHours() * 60 + bt.getMinutes() === slotMin;
        });
        const displayKw = isPast && actualBucket ? (actualBucket.pv_kw || 0) : s.pv_kw;
        const pct   = Math.round((displayKw / maxKw) * 100);
        const color = displayKw === 0 ? '#374151'
                    : (isPast && actualBucket) ? '#f59e0b'
                    : isPast ? '#065f46'
                    : slotDay === today ? '#10b981' : '#38bdf8';
        const localT = new Date(s.timestamp).toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' });
        const src  = isPast && actualBucket ? 'cloud actual' : 'forecast';
        const wh   = s.pv_wh !== undefined ? s.pv_wh : s.pv_kw * 500;
        return { pct, color, isNow, label: `${localT} (${src}) — ${displayKw.toFixed(2)} kW · ${wh.toFixed(0)} Wh` };
      });
    },

    get sdActiveBars() {
      return this.sdSolarViewMode === 'centred' ? this.sdCentredBars : this.sdForecastBars;
    },

    get sdNowPct() {
      const bars = this.sdCentredBars;
      if (!bars.length) return 50;
      const idx = bars.findIndex(b => b.isNow);
      return idx >= 0 ? Math.round((idx / bars.length) * 100) : 50;
    },

    // ── TOU Schedule sub-tab methods ──────────────────────────────────────

    async loadTouSchedule(force = false) {
      if (this._touFetched && !force) return;
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) { this.touError = 'No gateway selected'; return; }
      this.touLoading = true;
      this.touError = null;
      try {
        const r = await fetch(`api/gateways/${gw}/schedule`);
        const d = await r.json();
        if (d.error) { this.touError = d.error; return; }
        this.touPeriods       = d.tou_periods || [];
        this.touIsMulti       = d.is_multi_season || false;
        this.touActiveSeasonIdx = d.active_season_idx || 0;
        this.touActiveDayType = d.active_day_type || 1;
        this._touFetched = true;
      } catch(e) {
        this.touError = 'Failed to load TOU schedule: ' + e.message;
      } finally {
        this.touLoading = false;
      }
    },

    // ── Dispatch-mode colour helpers (primary: band colour by what the gateway IS DOING) ──
    // Keyed by dispatchId — matches FHP Demo colour scheme
    touDispatchBg(dispatchId) {
      const m = {
        1: 'rgba(59,130,246,0.18)',    // Home Loads — blue
        2: 'rgba(107,114,128,0.18)',   // Standby — grey
        3: 'rgba(251,191,36,0.18)',    // Solar Only — yellow
        4: 'rgba(16,185,129,0.18)',    // Grid Charge (Force) — green
        5: 'rgba(75,85,99,0.18)',      // Off / Backup — dark grey
        6: 'rgba(6,182,212,0.18)',     // Self Consumption — teal
        7: 'rgba(249,115,22,0.18)',    // Grid Export — orange
        8: 'rgba(16,185,129,0.18)',    // Grid Charge (alt ID) — green
        9: 'rgba(249,115,22,0.22)',    // Force Export — bright orange
      };
      return m[dispatchId] ?? 'rgba(148,163,184,0.10)';
    },
    touDispatchText(dispatchId) {
      const m = {
        1: '#93c5fd',  // blue-300
        2: '#9ca3af',  // gray-400
        3: '#fcd34d',  // amber-300
        4: '#6ee7b7',  // emerald-300
        5: '#6b7280',  // gray-500
        6: '#67e8f9',  // cyan-300
        7: '#fb923c',  // orange-400
        8: '#6ee7b7',  // emerald-300
        9: '#f97316',  // orange-500
      };
      return m[dispatchId] ?? '#94a3b8';
    },
    touDispatchBorder(dispatchId) {
      const m = {
        1: 'rgba(59,130,246,0.35)',
        2: 'rgba(107,114,128,0.30)',
        3: 'rgba(251,191,36,0.30)',
        4: 'rgba(16,185,129,0.30)',
        5: 'rgba(75,85,99,0.25)',
        6: 'rgba(6,182,212,0.30)',
        7: 'rgba(249,115,22,0.35)',
        8: 'rgba(16,185,129,0.30)',
        9: 'rgba(249,115,22,0.40)',
      };
      return m[dispatchId] ?? 'rgba(148,163,184,0.15)';
    },
    // ── waveType helpers — kept for price-tier sub-labels inside bands ────
    touWaveText(wt) {
      const m = { 0: '#6ee7b7', 1: '#fbbf24', 2: '#f87171', 4: '#67e8f9' };
      return m[wt] ?? '#94a3b8';
    },
    touWaveBg(wt) {
      const m = {
        0: 'rgba(16,185,129,0.08)', 1: 'rgba(245,158,11,0.10)',
        2: 'rgba(239,68,68,0.10)',  4: 'rgba(6,182,212,0.08)',
      };
      return m[wt] ?? 'rgba(148,163,184,0.06)';
    },
    touWaveBorder(wt) {
      const m = {
        0: 'rgba(16,185,129,0.25)', 1: 'rgba(245,158,11,0.25)',
        2: 'rgba(239,68,68,0.25)',  4: 'rgba(6,182,212,0.25)',
      };
      return m[wt] ?? 'rgba(148,163,184,0.15)';
    },
    // Canonical enum label — waveType integer → display name
    // Never uses p.name (user free-text). Matches arch doc §6 waveType table.
    touWaveLabel(wt) {
      const m = { 0: 'Off-Peak', 1: 'Mid-Peak', 2: 'On-Peak', 4: 'Super Off-Peak' };
      return m[wt] ?? `Period ${wt}`;
    },
    touDispatchLabel(dispatchId) {
      const m = {
        1: 'Home Loads', 2: 'Standby', 3: 'Solar Charge',
        6: 'Self-Consumption', 7: 'Grid Export', 8: 'Grid Charge',
      };
      return m[dispatchId] ?? `Mode ${dispatchId}`;
    },

    // Parse "HH:MM" → minutes from midnight
    _parseMins(t) {
      if (!t) return 0;
      const [h, m] = t.split(':').map(Number);
      return (h === 24 ? 1440 : h * 60) + (m || 0);
    },

    // Build proportional timeline bands — coloured by dispatchId (operating mode)
    // waveType shown as price-tier sub-label inside each band
    get touTimelineBands() {
      if (!this.touPeriods.length) return [];
      const total = 1440;
      return this.touPeriods.map(p => {
        const startMin = this._parseMins(p.start);
        let endMin     = this._parseMins(p.end);
        if (endMin <= startMin) endMin = 1440;
        const leftPct  = (startMin / total) * 100;
        const widthPct = Math.max(((endMin - startMin) / total) * 100, 0.5);

        const sdDots = (this.slots || [])
          .filter(sl => {
            if (!sl.start) return false;
            try {
              const d   = new Date(sl.start);
              const slMin = d.getHours() * 60 + d.getMinutes();
              return slMin >= startMin && slMin < endMin;
            } catch { return false; }
          })
          .map(sl => {
            const d      = new Date(sl.start);
            const slMin  = d.getHours() * 60 + d.getMinutes();
            const pct    = ((slMin - startMin) / Math.max(endMin - startMin, 1)) * 100;
            const action = sl.action || sl.trigger_category || '';
            const isOverride = action && !['HOLD', 'fallback', '', 'self_consumption'].includes(action.toLowerCase());
            return {
              pct,
              color: isOverride ? '#f87171' : '#6ee7b7',
              label: `${d.toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit'})} — SD: ${action}`,
            };
          })
          .slice(0, 12);

        return {
          leftPct,
          widthPct,
          // Primary: dispatch operating mode (what the battery IS doing)
          name:       this.touDispatchLabel(p.dispatchId),
          // Sub-label: price tier (waveType enum — NOT p.name user text)
          waveName:   this.touWaveLabel(p.waveType),
          modeLabel:  null,   // no longer used — name IS the mode
          bg:         this.touDispatchBg(p.dispatchId),
          border:     this.touDispatchBorder(p.dispatchId),
          text:       this.touDispatchText(p.dispatchId),
          sdDots,
        };
      });
    },

    // ── TOU Energy Forecast chart (Solar bars + SOC% line + TOU zones) ─────
    renderTouChart() {
      const canvas = document.getElementById('touForecastChart');
      if (!canvas) return;

      // Destroy previous chart instance if exists
      const existing = Chart.getChart(canvas);
      if (existing) existing.destroy();

      // Need at least engine slots to draw anything useful
      const slots = (this.slots || []).filter(s => s.start);
      if (!slots.length) return;

      const labels = slots.map(s =>
        new Date(s.start).toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false })
      );

      // Solar kW: prefer slot.pv_kw from engine forecast (already solar-derated by D4)
      const solarData = slots.map(s => s.pv_kw || 0);

      const gw = Alpine.store('app').currentGateway;
      const batteryKw2 = (gw && gw.battery_count ? gw.battery_count : 1) * 5.0;
      
      // We must synthesize Charge, Discharge, and SOC trajectory by simulating
      // the native TOU periods instead of the SD Engine's forecast, so it aligns with
      // the FHP Demo TOU timeline.
      
      const chargeData = new Array(slots.length).fill(0);
      const dischargeData = new Array(slots.length).fill(0);
      const socData = new Array(slots.length).fill(0);
      const homeLoadData  = slots.map(s => -(s.home_load_kw || 0));

      const batteryCapacityKwh = (gw && gw.battery_count ? gw.battery_count : 1) * 13.6;
      let currentSoc = slots.length > 0 && slots[0].projected_soc != null ? slots[0].projected_soc : (gw?.battery_soc || 50);

      for (let i = 0; i < slots.length; i++) {
        socData[i] = currentSoc;

        let chargeKw = 0;
        let dischargeKw = 0;
        const s = slots[i];
        const slotMin = new Date(s.start).getHours() * 60 + new Date(s.start).getMinutes();
        
        // Find active TOU period
        const p = this.touPeriods.find(period => {
          const startMin = this._parseMins(period.start);
          let endMin     = this._parseMins(period.end);
          if (endMin <= startMin) endMin = 1440;
          return slotMin >= startMin && slotMin < endMin;
        });

        const dispatchId = p ? p.dispatchId : 4; // Default to Self-Consumption (4)
        const solarKw = s.pv_kw || 0;
        const loadKw = s.home_load_kw || 0;

        if (dispatchId === 6 || dispatchId === 8) { // Force Charge
          chargeKw = batteryKw2;
        } else if (dispatchId === 7) { // Force Discharge
          dischargeKw = batteryKw2;
        } else if (dispatchId === 4) { // Self-Consumption
          const netKw = solarKw - loadKw;
          if (netKw > 0) {
            chargeKw = Math.min(netKw, batteryKw2);
          } else {
            dischargeKw = Math.min(-netKw, batteryKw2);
          }
        } else if (dispatchId === 5) { // Standby
          chargeKw = 0;
          dischargeKw = 0;
        }

        // Apply battery limits (simulate 30-min interval)
        const energyToChargeKwh = chargeKw * 0.5;
        const energyToDischargeKwh = dischargeKw * 0.5;
        const currentEnergyKwh = (currentSoc / 100) * batteryCapacityKwh;
        let newEnergyKwh = currentEnergyKwh + energyToChargeKwh - energyToDischargeKwh;

        // Clamp to 100%
        if (newEnergyKwh > batteryCapacityKwh) {
          chargeKw = Math.max(0, (batteryCapacityKwh - currentEnergyKwh) / 0.5);
          newEnergyKwh = batteryCapacityKwh;
        }
        // Clamp to 10% reserve
        const minEnergyKwh = batteryCapacityKwh * 0.1;
        if (newEnergyKwh < minEnergyKwh) {
          dischargeKw = Math.max(0, (currentEnergyKwh - minEnergyKwh) / 0.5);
          newEnergyKwh = minEnergyKwh;
        }

        chargeData[i] = chargeKw;
        dischargeData[i] = -dischargeKw;
        currentSoc = (newEnergyKwh / batteryCapacityKwh) * 100;
      }

      const solarKw2   = this.siteSolarKwp * 0.85;
      const maxFlow    = Math.ceil(Math.max(batteryKw2, solarKw2) / 5) * 5 || 5;

      // TOU background annotation boxes
      const annotations = {};
      this.touPeriods.forEach((p, i) => {
        const startMin = this._parseMins(p.start);
        let endMin     = this._parseMins(p.end);
        if (endMin <= startMin) endMin = 1440;
        // Find closest slot indices
        const startIdx = slots.findIndex(s => {
          const m = new Date(s.start).getHours() * 60 + new Date(s.start).getMinutes();
          return m >= startMin;
        });
        const endIdx = slots.findLastIndex ? slots.findLastIndex(s => {
          const m = new Date(s.start).getHours() * 60 + new Date(s.start).getMinutes();
          return m < endMin;
        }) : slots.reduce((acc, s, idx) => {
          const m = new Date(s.start).getHours() * 60 + new Date(s.start).getMinutes();
          return m < endMin ? idx : acc;
        }, -1);
        if (startIdx < 0) return;
        
        // Use dispatch action colours for the chart background, just like the timeline
        const bgStr = this.touDispatchBg(p.dispatchId);
        // Dim the background a bit for the chart area
        const chartBg = bgStr.replace('0.18)', '0.08)').replace('0.22)', '0.12)');
        
        annotations[`tou${i}`] = {
          type:            'box',
          xMin:            startIdx === 0 ? -0.5 : startIdx - 0.5,
          xMax:            (endIdx < 0 ? slots.length - 1 : endIdx) + 0.5,
          yScaleID:        'yKw',
          backgroundColor: chartBg,
          borderColor:     this.touDispatchBorder(p.dispatchId) ?? 'rgba(148,163,184,0.15)',
          borderWidth:     1,
          label: {
            display:    true,
            content:    this.touDispatchLabel(p.dispatchId),
            position:   { x: 'center', y: 'start' },
            color:      this.touDispatchText(p.dispatchId),
            font:       { size: 9, weight: 'bold' },
            padding:    { top: 4, left: 4, right: 4, bottom: 0 },
          },
        };
      });

      // Sunrise / sunset dashed lines from solarData if available
      const solarSlots = this.solarData?.slots || [];
      const todayStr   = new Date().toLocaleDateString('en-AU');
      const todaySlots = solarSlots.filter(s => new Date(s.timestamp).toLocaleDateString('en-AU') === todayStr);
      const srSlot     = todaySlots.find(s => (s.pv_kw || 0) > 0.05);
      const ssSlot     = [...todaySlots].reverse().find(s => (s.pv_kw || 0) > 0.05);
      if (srSlot) {
        const srTime = new Date(srSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const srIdx  = labels.indexOf(srTime);
        if (srIdx >= 0) {
          annotations.sunrise = {
            type: 'line', scaleID: 'x', value: srIdx,
            borderColor: 'rgba(251,191,36,0.5)', borderWidth: 1, borderDash: [4,3],
            label: { display: true, content: 'Sunrise', position: 'start',
                     color: '#fbbf24', font: { size: 8 }, padding: 3 },
          };
        }
      }
      if (ssSlot) {
        const ssTime = new Date(ssSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const ssIdx  = labels.indexOf(ssTime);
        if (ssIdx >= 0) {
          annotations.sunset = {
            type: 'line', scaleID: 'x', value: ssIdx,
            borderColor: 'rgba(251,146,60,0.5)', borderWidth: 1, borderDash: [4,3],
            label: { display: true, content: 'Sunset', position: 'start',
                     color: '#fb923c', font: { size: 8 }, padding: 3 },
          };
        }
      }
      const peakSlot = [...todaySlots].sort((a,b) => b.pv_kw - a.pv_kw)[0];
      if (peakSlot && peakSlot.pv_kw > 0.1) {
        const peakTime = new Date(peakSlot.timestamp).toLocaleTimeString('en-AU', {hour:'2-digit',minute:'2-digit',hour12:false});
        const peakIdx  = labels.indexOf(peakTime);
        if (peakIdx >= 0) {
          annotations.peak = {
            type: 'line', scaleID: 'x', value: peakIdx,
            borderColor: 'rgba(251,191,36,0.8)', borderWidth: 1, borderDash: [2,2],
            label: { display: true, content: 'Solar Peak', position: 'start',
                     color: '#fcd34d', font: { size: 8 }, padding: 3 },
          };
        }
      }

      this._touChart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            {
              type: 'line', label: 'SOC %',
              data: socData,
              borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.08)',
              borderWidth: 2, pointRadius: 0, pointHoverRadius: 3,
              tension: 0.4, fill: false,
              yAxisID: 'ySoc', order: 0,
            },
            {
              type: 'bar', label: 'Solar PV',
              data: solarData,
              backgroundColor: '#fcd34d',
              yAxisID: 'yKw', stack: 'Power', order: 1,
            },
            {
              type: 'bar', label: 'Grid Charge',
              data: chargeData,
              backgroundColor: '#34d399',
              yAxisID: 'yKw', stack: 'Power', order: 2,
            },
            {
              type: 'bar', label: 'Discharge',
              data: dischargeData,
              backgroundColor: '#f87171',
              yAxisID: 'yKw', stack: 'Power', order: 3,
            },
            {
              type: 'bar', label: 'Home Load',
              data: homeLoadData,
              backgroundColor: '#818cf8',
              yAxisID: 'yKw', stack: 'Power', order: 4,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend:     { display: false },
            annotation: { annotations },
            tooltip: {
              backgroundColor: 'rgba(15,23,42,0.95)',
              titleColor: '#e2e8f0', bodyColor: '#cbd5e1',
              borderColor: '#334155', borderWidth: 1, padding: 10,
              callbacks: {
                title: ctxs => {
                  if (!ctxs.length) return '';
                  const s = slots[ctxs[0].dataIndex];
                  if (!s) return labels[ctxs[0].dataIndex];
                  const end = new Date(s.end || s.start);
                  return `${labels[ctxs[0].dataIndex]} – ${end.toLocaleTimeString('en-AU',{hour:'2-digit',minute:'2-digit',hour12:false})}`;
                },
                beforeBody: ctxs => {
                  if (!ctxs.length) return '';
                  const s = slots[ctxs[0].dataIndex];
                  if (!s) return '';
                  // Find which TOU period this slot falls in
                  const slMin = new Date(s.start).getHours() * 60 + new Date(s.start).getMinutes();
                  const period = this.touPeriods.find(p => {
                    const sm = this._parseMins(p.start);
                    let em = this._parseMins(p.end);
                    if (em <= sm) em = 1440;
                    return slMin >= sm && slMin < em;
                  });
                  return period ? `TOU: ${this.touWaveLabel(period.waveType)} • ${this.touDispatchLabel(period.dispatchId)}` : '';
                },
                label: ctx => {
                  if (ctx.dataset.label === 'SOC %') return ctx.raw != null ? `SOC: ${ctx.raw.toFixed(1)}%` : null;
                  const v = Math.abs(ctx.raw);
                  return v > 0 ? `${ctx.dataset.label}: ${v.toFixed(2)} kW` : null;
                },
              },
            },
          },
          scales: {
            x: {
              stacked: true,
              grid: { display: false },
              ticks: { color: '#94a3b8', maxTicksLimit: 12, maxRotation: 0, autoSkip: true },
            },
            yKw: {
              type: 'linear', position: 'left', stacked: true,
              min: -maxFlow, max: maxFlow,
              grid: { color: 'rgba(255,255,255,0.04)' },
              ticks: { color: '#94a3b8', callback: v => (v > 0 ? '+' : '') + v + ' kW' },
            },
            ySoc: {
              type: 'linear', position: 'right',
              min: 0, max: 100,
              grid: { display: false },
              ticks: { color: '#60a5fa', callback: v => v + '%' },
            },
          },
        },
      });
    },

    // Legend items — keyed by dispatchId (operating mode), not waveType
    // Only shows modes actually present in the current TOU schedule
    get touLegend() {
      const DISPATCH_MODES = [
        { id: 6, label: 'Self Consumption' },
        { id: 1, label: 'Home Loads' },
        { id: 2, label: 'Standby' },
        { id: 3, label: 'Solar Only' },
        { id: 4, label: 'Grid Charge' },
        { id: 8, label: 'Grid Charge' },
        { id: 5, label: 'Backup' },
        { id: 7, label: 'Grid Export' },
        { id: 9, label: 'Force Export' },
      ];
      const presentIds = new Set(this.touPeriods.map(p => p.dispatchId));
      // Deduplicate (ids 4 and 8 are both Grid Charge)
      const seen = new Set();
      return DISPATCH_MODES
        .filter(m => presentIds.has(m.id) && !seen.has(m.label) && seen.add(m.label))
        .map(m => ({
          label:  m.label,
          bg:     this.touDispatchBg(m.id),
          text:   this.touDispatchText(m.id),
          border: this.touDispatchBorder(m.id),
        }));
    },

    // Dominant SD action recommendation during a TOU period (for the detail table badge)
    touSdDecision(p) {
      if (!this.slots || !this.slots.length) return null;
      const startMin = this._parseMins(p.start);
      let endMin     = this._parseMins(p.end);
      if (endMin <= startMin) endMin = 1440;
      const matching = (this.slots || []).filter(sl => {
        if (!sl.start) return false;
        try {
          const d = new Date(sl.start);
          const slMin = d.getHours() * 60 + d.getMinutes();
          return slMin >= startMin && slMin < endMin;
        } catch { return false; }
      });
      if (!matching.length) return null;
      // Count actions and pick majority
      const counts = {};
      matching.forEach(sl => {
        const a = sl.action || 'fallback';
        counts[a] = (counts[a] || 0) + 1;
      });
      const dominant = Object.entries(counts).sort((a,b) => b[1]-a[1])[0]?.[0] || 'fallback';
      const isSame = ['HOLD', 'fallback', 'self_consumption'].includes(dominant.toLowerCase());
      const actionLabels = { GRID_CHARGE: 'SD: Charge', GRID_EXPORT: 'SD: Export', STANDBY: 'SD: Standby', HOLD: 'SD: Hold (native mode)' };
      return {
        label:  isSame ? 'SD: Agrees — No Override' : (actionLabels[dominant] || `SD: ${dominant}`),
        bg:     isSame ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)',
        text:   isSame ? '#6ee7b7' : '#f87171',
      };
    },

    get touSeasonLabel() {
      if (!this.touIsMulti) return 'Single season';
      return `Season ${this.touActiveSeasonIdx + 1}`;
    },

    get touDayTypeLabel() {
      return { 1: 'Weekday', 2: 'Weekend', 3: 'Every day' }[this.touActiveDayType] ?? 'Today';
    },

  };
}

// ── haEntityPicker — HA entity combobox (Phase C) ───────────────────────────
// Provides live autocomplete from /api/ha/entities.
// Usage: x-data="haEntityPicker({domains:'switch,input_boolean,automation,script'})"
//   bind value:  x-model="picker.value"
//   bind events: @picker-selected.window="cfg.solar_curtail_entity = $event.detail.entity_id"
function haEntityPicker(opts = {}) {
  return {
    value:       opts.initial || '',
    domains:     opts.domains || 'switch,input_boolean,automation,script',
    results:     [],
    open:        false,
    loading:     false,
    validated:   null,   // null | 'ok' | 'warn' | 'error'
    validLabel:  '',
    _debounce:   null,
    haConnected: false,

    async init() {
      // Check HA connection
      try {
        const r = await fetch('api/ha/status');
        const d = await r.json();
        this.haConnected = d.connected || false;
      } catch(e) { this.haConnected = false; }
      // Pre-validate if a value is already set
      if (this.value) await this.validate();
    },

    onInput() {
      this.validated = null;
      clearTimeout(this._debounce);
      this._debounce = setTimeout(() => this.search(), 300);
    },

    async search() {
      if (!this.haConnected) return;
      const q = this.value.trim();
      if (q.length < 2) { this.results = []; this.open = false; return; }
      this.loading = true;
      try {
        const domains = this.domains.split(',').map(d => d.trim()).filter(Boolean);
        // Fetch per domain and merge
        const all = [];
        for (const domain of domains) {
          const r = await fetch(`api/ha/entities?domain=${domain}&search=${encodeURIComponent(q)}&limit=20`);
          const d = await r.json();
          all.push(...(d.entities || []));
        }
        // Sort: exact match first, then alphabetical
        all.sort((a, b) => {
          const aExact = a.entity_id.toLowerCase() === q.toLowerCase() ? -1 : 0;
          const bExact = b.entity_id.toLowerCase() === q.toLowerCase() ? -1 : 0;
          return aExact - bExact || a.entity_id.localeCompare(b.entity_id);
        });
        this.results = all.slice(0, 25);
        this.open = this.results.length > 0;
      } catch(e) { this.results = []; }
      finally { this.loading = false; }
    },

    select(entity) {
      this.value = entity.entity_id;
      this.open  = false;
      this.validated = 'ok';
      this.validLabel = `${entity.name} — ${entity.domain} — state: ${entity.state}`;
      // Dispatch event so parent component can bind
      this.$dispatch('entity-selected', { entity_id: entity.entity_id, entity });
    },

    async validate() {
      if (!this.value.trim()) { this.validated = null; return; }
      // Shape first, and it is an error rather than a warning: a value with
      // no domain can never resolve, whether or not HA is reachable. This
      // check was missing, so "energipays" was accepted and then polled every
      // 30 seconds forever.
      if (!/^[a-z_]+\.[a-z0-9_]+$/i.test(this.value.trim())) {
        this.validated  = 'error';
        this.validLabel = 'Not an entity id — expected domain.object_id, e.g. sensor.hot_water_power';
        return;
      }
      if (!this.haConnected)  { this.validated = 'warn'; this.validLabel = 'HA not connected — shape looks right, saved as-is'; return; }
      try {
        const domain = this.value.split('.')[0];
        const r = await fetch(`api/ha/entities?domain=${domain}&search=${encodeURIComponent(this.value)}&limit=5`);
        const d = await r.json();
        const match = (d.entities || []).find(e => e.entity_id === this.value);
        if (match) {
          this.validated  = 'ok';
          this.validLabel = `${match.name} — state: ${match.state}`;
        } else {
          this.validated  = 'warn';
          this.validLabel = 'Entity not found in HA — double-check the ID';
        }
      } catch(e) {
        this.validated  = 'warn';
        this.validLabel = 'Could not validate — HA unreachable';
      }
    },

    close() { setTimeout(() => { this.open = false; }, 150); },
  };
}

// ── sdSolarConfig — Solar Forecast Source config panel ───────────────────
// Manages /api/solar/config and /api/solar/status
function sdSolarConfig() {
  return {
    cfg: {
      enabled: false,
      source: 'auto',
      ha_solar_actual_entity:   '',
      ha_solar_forecast_entity: '',
      ha_solar_curtail_entity:  '',
      lat: null, lng: null,
      azimuth: 180, tilt: 22.5, kwp: 5.0,
      forecast_solar_api_key: '',
      solcast_api_key: '',
      solcast_site_id: '',
      home_load_assumption_kw: 0.5,
    },
    status: null,
    haConnected: false,
    saving: false,
    testing: false,
    testResult: null,
    loading: true,

    async init() {
      try {
        // HA connection flag
        const hs = await fetch('api/ha/status');
        const hd = await hs.json();
        this.haConnected = hd.connected || false;
      } catch(e) { this.haConnected = false; }
      await this.load();
    },

    async load() {
      this.loading = true;
      try {
        const [cr, sr] = await Promise.all([
          fetch('api/solar/config').then(r => r.json()),
          fetch('api/solar/status').then(r => r.json()),
        ]);
        if (cr.config) {
          // Merge without overwriting empty strings with nulls
          Object.entries(cr.config).forEach(([k, v]) => {
            if (k in this.cfg) this.cfg[k] = v ?? this.cfg[k];
          });
        }
        this.status = sr;
      } catch(e) {
        console.warn('Solar config load error:', e);
      } finally {
        this.loading = false;
      }
    },

    async save() {
      this.saving = true;
      this.testResult = null;
      try {
        const payload = { ...this.cfg };
        // Don't send redacted values back
        if (payload.solcast_api_key === '***') delete payload.solcast_api_key;
        if (payload.forecast_solar_api_key === '***') delete payload.forecast_solar_api_key;
        const r = await fetch('api/solar/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const d = await r.json();
        if (d.ok) {
          this.testResult = { ok: true, msg: 'Saved ✓' };
          await this.load();
        } else {
          this.testResult = { ok: false, msg: 'Save failed' };
        }
      } catch(e) {
        this.testResult = { ok: false, msg: String(e) };
      } finally {
        this.saving = false;
      }
    },

    async test() {
      this.testing = true;
      this.testResult = null;
      try {
        const r = await fetch('api/solar/test', { method: 'POST' });
        const d = await r.json();
        this.testResult = {
          ok:  d.ok,
          msg: d.message || (d.ok ? 'Connected ✓' : 'Connection failed'),
        };
        if (d.ok) await this.load(); // refresh status
      } catch(e) {
        this.testResult = { ok: false, msg: String(e) };
      } finally {
        this.testing = false;
      }
    },

    get activeSourceLabel() {
      const s = this.status?.active_source || 'none';
      const map = {
        ha_entities:    '☀ HA Entities',
        forecast_solar: '☀ Forecast.Solar',
        solcast:        '☀ Solcast',
        none:           '— Not configured',
      };
      return map[s] || s;
    },

    get showStandaloneFields() {
      return this.cfg.source === 'forecast_solar' || this.cfg.source === 'solcast'
        || (this.cfg.source === 'auto' && !this.haConnected);
    },

    get showHAFields() {
      return this.cfg.source === 'ha_entities'
        || (this.cfg.source === 'auto' && this.haConnected);
    },
  };
}

// ── sdSolarStatus — lightweight read-only strip for Amber Smart Dispatch ──
// Full config lives in Admin → Solar tab (sdSolarConfig).
function sdSolarStatus() {
  return {
    activeSource: null,
    lastSync:     null,
    async init() {
      try {
        const r = await fetch('api/solar/status');
        const d = await r.json();
        if (d.ok) {
          this.activeSource = d.active_source || 'none';
          this.lastSync = d.last_sync
            ? new Date(d.last_sync).toLocaleTimeString('en-AU', {hour:'2-digit', minute:'2-digit'})
            : null;
        }
      } catch(e) { /* silent — strip is best-effort */ }
    },
    get sourceLabel() {
      const labels = {
        ha_entities:    'HA Entities',
        forecast_solar: 'Forecast.Solar',
        solcast:        'Solcast',
        auto:           'Auto',
        none:           'Not configured',
      };
      return labels[this.activeSource] || this.activeSource || 'Not configured';
    },
  };
}

// ── sdAuditLog — display historical engine decisions ──
function sdAuditLog() {
  return {
    log: [],
    loading: false,
    _timer: null,
    _visHandler: null,
    async init() {
      this._stopPolling();
      await this.refresh();
      // Same opt-in + visibility-gated policy as sdEnginePanel — see the
      // longer comment in that panel's init() for rationale.
      if (!this._visHandler) {
        this._visHandler = () => this._syncPolling();
        document.addEventListener('visibilitychange', this._visHandler);
      }
      this._syncPolling();
    },
    _syncPolling() {
      const on = !!(window.Alpine?.store('app')?.sdAutoRefresh);
      const visible = (typeof document === 'undefined') || document.visibilityState === 'visible';
      if (on && visible) this._startPolling();
      else this._stopPolling();
    },
    _startPolling() {
      if (this._timer) return;
      this._timer = setInterval(() => this.refresh(), 60000);
    },
    _stopPolling() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },
    async refresh() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      this.loading = true;
      try {
        const r = await fetch(`api/smart_dispatch/log?gateway_id=${gw}&limit=50`);
        const d = await r.json();
        if (d.ok) this.log = d.log || [];
      } catch(e) { console.warn('Audit log fetch failed', e); }
      finally { this.loading = false; }
    },
    timeAgo(ts) {
      if (!ts) return '—';
      const d = new Date(ts);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    },
    formatDate(ts) {
      if (!ts) return '—';
      const d = new Date(ts);
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    },
    logCategoryBadge(c) {
      const m = {
        demand_charge:   'bg-purple-500/10 text-purple-600 dark:bg-purple-500/20 dark:text-purple-400',
        negative_export: 'bg-red-500/10 text-red-600 dark:bg-red-500/20 dark:text-red-400',
        price_spike:     'bg-red-500/10 text-red-600 dark:bg-red-500/20 dark:text-red-400',
        export_bonus:    'bg-amber-500/10 text-amber-600 dark:bg-amber-500/20 dark:text-amber-400',
        earnings_target: 'bg-green-500/10 text-green-600 dark:bg-green-500/20 dark:text-green-400',
        force_charge:    'bg-teal-500/10 text-teal-600 dark:bg-teal-500/20 dark:text-teal-400',
        legacy_rule:     'bg-blue-500/10 text-blue-600 dark:bg-blue-500/20 dark:text-blue-400',
        fallback:        'bg-gray-500/10 text-gray-600 dark:bg-gray-500/20 dark:text-gray-400',
        paused:          'bg-gray-500/10 text-gray-500 dark:bg-gray-500/20 dark:text-gray-500',
        time_schedule:   'bg-cyan-500/10 text-cyan-600 dark:bg-cyan-500/20 dark:text-cyan-400',
      };
      return m[c] || 'bg-[var(--surface-2)] text-gray-500 dark:text-gray-400';
    },
    logCategoryShort(c) {
      const m = {
        demand_charge:   '⚡ DEMAND',
        negative_export: '📉 NEG EXP',
        price_spike:     '🔺 SPIKE',
        export_bonus:    '☀ BONUS',
        earnings_target: '💰 TARGET',
        force_charge:    '🔋 CHARGE',
        legacy_rule:     '📋 CUSTOM',
        fallback:        '🔄 NORMAL',
        paused:          '⏸ PAUSED',
        time_schedule:   '📅 SCHED',
      };
      return m[c] || c;
    },
    actionColor(a) {
      if (!a) return 'text-gray-500';
      if (a === 'GRID_CHARGE') return 'text-teal-600 dark:text-teal-400';
      if (a === 'GRID_EXPORT') return 'text-orange-600 dark:text-orange-400';
      if (a === 'HOLD') return 'text-red-600 dark:text-red-400';
      return 'text-blue-600 dark:text-blue-400';
    }
  };
}

// ── Forecast Loads Tab Component ─────────────────────────────────────────────
function forecastLoadsTab() {
  return {
    loads: [],
    loading: true,
    saving: false,
    editing: false,
    filterType: null,         // 'now' | 'forecast' | null
    currentSeason: '',
    showAllEntities: false,
    editForm: {
      id: null,
      name: '',
      category: 'general',
      measurement_type: 'forecast',
      avg_kw: 0.5,
      peak_kw: 1.0,
      ha_entity_id: '',
      ha_energy_entity_id: '',
      ha_switch_entity_id: '',
      ha_binary_entity_id: '',
      schedules: [],
      enabled: 1,
      gateway_id: 'global',
      dispatch_category: '2-Essential Load'
    },

    async init() {
      await this.fetchLoads();
    },

    async fetchLoads() {
      this.loading = true;
      try {
        const res = await fetch('api/smart_dispatch/forecast-loads');
        const d = await res.json();
        if (d.ok) {
          this.currentSeason = d.current_season || '';
          this.loads = d.data.map(l => {
            let sch = [];
            try { sch = JSON.parse(l.schedule_json || '[]'); } catch(e){}
            return { ...l, schedules: sch, collapsed: false, ha_entities_collapsed: true };
          });
        }
      } catch (e) {
        console.error('Failed to load forecast loads', e);
      } finally {
        this.loading = false;
      }
    },

    toggleFilter(type) {
      this.filterType = (this.filterType === type) ? null : type;
    },

    filteredLoads() {
      if (!this.filterType) return this.loads;
      return this.loads.filter(l => l.measurement_type === this.filterType);
    },

    isScheduleActiveNow(sch) {
      if (!sch || sch.is_active === false) return false;
      const schSeason = (sch.seasonName || '').toLowerCase();
      const cur = (this.currentSeason || '').toLowerCase();
      if (schSeason && cur && schSeason !== cur) return false;

      const now = new Date();
      const period = (sch.period || '').toLowerCase();
      const dayNames = ['sunday','monday','tuesday','wednesday','thursday','friday','saturday'];
      const dayName = dayNames[now.getDay()];
      const isWeekend = now.getDay() === 0 || now.getDay() === 6;
      const dom = now.getDate();
      const pad = n => String(n).padStart(2, '0');
      const curTime = pad(now.getHours()) + ':' + pad(now.getMinutes());

      let match = false;
      if (period === 'everyday') match = true;
      else if (period === 'weekdays' && !isWeekend) match = true;
      else if (period === 'weekends' && isWeekend) match = true;
      else if (period === dayName) match = true;
      else {
        const m = period.match(/^(\d+)(st|nd|rd|th)?\s+of\s+month$/);
        if (m && parseInt(m[1], 10) === dom) match = true;
      }
      if (!match) return false;

      const startT = sch.start_time || '00:00';
      const endT = sch.end_time || '23:59';
      if (startT <= endT) return curTime >= startT && curTime <= endT;
      return curTime >= startT || curTime <= endT;
    },

    // Convert any HA power reading to canonical kW. Returns null if the
    // unit is unknown so the caller can decide whether to fall back.
    normalizePowerToKw(value, unit) {
      const v = parseFloat(value);
      if (isNaN(v)) return null;
      const u = (unit || '').trim();
      // Common HA units for power
      if (u === 'kW')  return v;
      if (u === 'W')   return v / 1000;
      if (u === 'MW')  return v * 1000;
      if (u === 'mW')  return v / 1_000_000;
      // No unit reported — assume kW (legacy behavior) but flag via return
      if (u === '')    return v;
      return null;
    },

    // Convert any HA energy reading to canonical kWh.
    normalizeEnergyToKwh(value, unit) {
      const v = parseFloat(value);
      if (isNaN(v)) return null;
      const u = (unit || '').trim();
      if (u === 'kWh') return v;
      if (u === 'Wh')  return v / 1000;
      if (u === 'MWh') return v * 1000;
      if (u === '')    return v;
      return null;
    },

    // Format "<value> <unit>" using the HA-reported unit verbatim. Adds a
    // trailing "(?)" hint when the unit is missing so the user can spot it.
    formatLiveValue(value, unit) {
      if (value === undefined || value === null || value === '') return '';
      const u = (unit || '').trim();
      return u ? `${value} ${u}` : `${value} (?)`;
    },

    // Format a canonical kW reading with auto-scaling: shows W under 1 kW.
    fmtKw(kw) {
      if (kw === null || kw === undefined || isNaN(kw)) return '— kW';
      if (kw === 0) return '0 kW';
      if (Math.abs(kw) < 1) return `${(kw * 1000).toFixed(0)} W`;
      if (Math.abs(kw) < 10) return `${kw.toFixed(2)} kW`;
      return `${kw.toFixed(1)} kW`;
    },

    // Format a canonical kWh reading with auto-scaling.
    fmtKwh(kwh) {
      if (kwh === null || kwh === undefined || isNaN(kwh)) return '— kWh';
      if (kwh === 0) return '0 kWh';
      if (Math.abs(kwh) < 1) return `${(kwh * 1000).toFixed(0)} Wh`;
      if (Math.abs(kwh) < 100) return `${kwh.toFixed(2)} kWh`;
      return `${kwh.toFixed(1)} kWh`;
    },

    get liveStats() {
      const live = this.loads.filter(l => l.enabled && l.measurement_type === 'now');
      let power = 0, energy = 0, active = 0;
      for (const l of live) {
        const pKw  = this.normalizePowerToKw(l.ha_live_power_state, l.ha_live_power_unit);
        const eKwh = this.normalizeEnergyToKwh(l.ha_live_energy_state, l.ha_live_energy_unit);
        const switchOn = (l.ha_live_switch_state || '').toLowerCase() === 'on';
        const binaryOn = (l.ha_live_binary_state || '').toLowerCase() === 'on';
        if (pKw !== null) power += pKw;
        else if (switchOn || binaryOn) power += parseFloat(l.avg_kw) || 0;
        if (eKwh !== null) energy += eKwh;
        const numericActive = pKw !== null && pKw > 0.00001;
        if (switchOn || binaryOn || numericActive) active++;
      }
      return { power, energy, activeCount: active, totalCount: live.length };
    },

    get forecastStats() {
      const fc = this.loads.filter(l => l.enabled && l.measurement_type === 'forecast');
      let activePower = 0, active = 0;
      for (const l of fc) {
        const isActive = (l.schedules || []).some(s => this.isScheduleActiveNow(s));
        if (isActive) {
          activePower += parseFloat(l.avg_kw) || 0;
          active++;
        }
      }
      return { activePower, activeCount: active, totalCount: fc.length };
    },

    collapseAll() {
      this.loads.forEach(l => l.collapsed = true);
    },

    expandAll() {
      this.loads.forEach(l => l.collapsed = false);
    },

    openEditor(load = null) {
      if (load) {
        this.editForm = {
          id: load.id,
          name: load.name,
          measurement_type: load.measurement_type,
          avg_kw: load.avg_kw,
          peak_kw: load.peak_kw || 0.0,
          ha_entity_id: load.ha_entity_id || '',
          ha_energy_entity_id: load.ha_energy_entity_id || '',
          ha_switch_entity_id: load.ha_switch_entity_id || '',
          ha_binary_entity_id: load.ha_binary_entity_id || '',
          category: load.category || 'general',
          schedules: JSON.parse(JSON.stringify(load.schedules)), // deep copy
          enabled: load.enabled,
          gateway_id: load.gateway_id || 'global',
          dispatch_category: load.dispatch_category || '2-Essential Load'
        };
      } else {
        this.editForm = {
          id: null,
          name: '',
          category: 'general',
          measurement_type: 'forecast',
          avg_kw: 1.0,
          peak_kw: 1.0,
          ha_entity_id: '',
          ha_energy_entity_id: '',
          ha_switch_entity_id: '',
          ha_binary_entity_id: '',
          schedules: [{
            seasonName: '',
            period: 'everyday',
            start_time: '00:00',
            end_time: '23:59'
          }],
          enabled: 1,
          gateway_id: 'global',
          dispatch_category: '2-Essential Load'
        };
      }
      this.editing = true;
    },

    closeEditor() {
      this.editing = false;
    },

    addSchedulePeriod() {
      this.editForm.schedules.push({
        seasonName: '',
        period: 'everyday',
        start_time: '00:00',
        end_time: '23:59'
      });
    },

    async saveLoad() {
      if (!this.editForm.name) return alert('Name is required');
      this.saving = true;
      try {
        const payload = {
          ...this.editForm,
          schedule_json: this.editForm.schedules
        };
        const res = await fetch('api/smart_dispatch/forecast-loads', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.ok) {
          this.closeEditor();
          await this.fetchLoads();
        } else {
          alert('Failed to save load: ' + d.error);
        }
      } catch (e) {
        console.error(e);
        alert('Failed to save load');
      } finally {
        this.saving = false;
      }
    },

    async toggleEnabled(load) {
      load.enabled = load.enabled ? 0 : 1;
      try {
        const payload = { ...load, schedule_json: load.schedules };
        await fetch('api/smart_dispatch/forecast-loads', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        await this.fetchLoads();
      } catch (e) {
        console.error(e);
      }
    },

    async deleteLoad(id) {
      if (!confirm('Are you sure you want to delete this forecast load?')) return;
      try {
        const res = await fetch(`api/smart_dispatch/forecast-loads/${id}`, {
          method: 'DELETE'
        });
        const d = await res.json();
        if (d.ok) {
          await this.fetchLoads();
        }
      } catch (e) {
        console.error(e);
      }
    },
    
    get availableGateways() {
      return window.Alpine?.store('app')?.gateways || [];
    }
  };
}

function sdStrategyMixer() {
  console.log('sdStrategyMixer initializing...');
  return {
    rows: [],
    actuators: [],
    loading: true,
    saving: false,
    dna: { accessories: [] },
    activeRowIdx: 0,
    searchQuery: '',
    rowToDelete: null,
    indexToDelete: null,
    _cleanRowsSnapshot: null,
    deletedRowIds: [],

    unlocked: false,
    secondsRemaining: 0,
    expiresAt: null,
    showOtpModal: false,
    otpPin: '',
    generatedPin: '',
    pinError: '',
    loadingOtp: false,
    sessionToken: '',
    countdownInterval: null,
    showNavGuardModal: false,
    _pendingNavTab: null,
    _pendingNavSubView: null,

    async init() {
      // Dynamic selected gateway reactivity to ensure sync across switching & slow loads
      this.$watch('$store.app.selectedGateway', async (val) => {
        if (this.countdownInterval) {
          clearInterval(this.countdownInterval);
          this.countdownInterval = null;
        }
        this.unlocked = false;
        this.secondsRemaining = 0;
        this.expiresAt = null;
        this.sessionToken = '';
        if (val) {
          await this.refreshAll();
          await this.checkUnlockStatus();
        } else {
          this.rows = [];
          this.dna = { accessories: [] };
          this.activeRowIdx = null;
        }
      });

      if (Alpine.store('app').selectedGateway) {
        await this.refreshAll();
        await this.checkUnlockStatus();
      } else {
        console.log('Mixer: Waiting for gateway selection...');
        let checks = 0;
        const interval = setInterval(async () => {
          checks++;
          if (Alpine.store('app').selectedGateway || checks > 20) {
            clearInterval(interval);
            await this.refreshAll();
            await this.checkUnlockStatus();
          }
        }, 500);
      }

      // Register navigation guard for unsaved draft strategies
      window._mixerNavGuard = (toTab, toSubView) => {
        if (Alpine.store('app').activeTab !== 'smart_dispatch') return true;
        
        // Comprehensive check for any unsaved changes
        const hasUnsaved = this.isDirty();
        if (!hasUnsaved) return true;

        if (toTab === 'smart_dispatch') return true;
        if (toSubView === 'mixer') return true;

        this._pendingNavTab = toTab;
        this._pendingNavSubView = toSubView;
        this.showNavGuardModal = true;
        return false;
      };

      // Guard browser reload / close
      window.addEventListener('beforeunload', (e) => {
        if (Alpine.store('app').activeTab === 'smart_dispatch' && this.isDirty()) {
          e.preventDefault();
          e.returnValue = 'You have unsaved strategy changes.';
          return e.returnValue;
        }
      });
    },

    isDirty() {
      if (!this._cleanRowsSnapshot) return false;
      // Compare current rows to the clean rows snapshot and check if there are pending deletions
      const hasDeletions = this.deletedRowIds && this.deletedRowIds.length > 0;
      return JSON.stringify(this.rows) !== this._cleanRowsSnapshot || hasDeletions;
    },

    confirmNavAway() {
      const tab = this._pendingNavTab;
      const subView = this._pendingNavSubView;
      this._pendingNavTab = null;
      this._pendingNavSubView = null;
      this.showNavGuardModal = false;

      // Temporarily disable the guard so navigation proceeds
      const originalGuard = window._mixerNavGuard;
      window._mixerNavGuard = null;

      if (tab) {
        Alpine.store('app').setTab(tab);
      } else if (subView) {
        // Switch sub-tab view inside smartDispatchTab
        const rootComponent = document.querySelector('[x-data="smartDispatchTab()"]');
        if (rootComponent && rootComponent.__x) {
          rootComponent.__x.$data.view = subView;
        }
      }

      // Restore guard on next tick
      setTimeout(() => {
        window._mixerNavGuard = originalGuard;
      }, 50);
    },

    cancelNavAway() {
      this._pendingNavTab = null;
      this._pendingNavSubView = null;
      this.showNavGuardModal = false;
    },

    async refreshAll() {
      this.loading = true;
      await Promise.all([
        this.loadRows(),
        this.loadActuators(),
        this.loadDNA()
      ]);
      this.loading = false;
    },

    async loadRows() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      try {
        const r = await fetch(`api/smart_dispatch/matrix?gateway_id=${gw}`);
        const d = await r.json();
        if (d.ok) {
          this.rows = d.rows.map(row => {
            // Hydrate builder fields from JSON
            let cond = this.parse(row.conditions_json);
            let sigs = this.parse(row.signals_json);
            
            row.use_manual_json = row.use_manual_json || false;
            row.expanded = false; // default collapsed
            
            // Simple builder only supports single conditions for now
            row.builder_param = cond.field || 'import_c_kwh';
            row.builder_op = cond.op || 'GT';
            row.builder_value_mode = cond.value_mode || 'literal';
            row.builder_value_ref = cond.value_ref || '';
            row.builder_val = cond.value !== undefined ? cond.value : '';
            
            // Output signals
            if (sigs && sigs.length > 0) {
              row.builder_action = sigs[0].action_type || 'HOLD';
              row.builder_target_soc = sigs[0].params?.soc_max || 100;
            } else {
              row.builder_action = 'HOLD';
              row.builder_target_soc = 100;
            }
            
            return row;
          });
          // Default select the first row
          this.activeRowIdx = this.rows.length > 0 ? 0 : null;
          this._cleanRowsSnapshot = JSON.stringify(this.rows);
          this.deletedRowIds = [];
        }
      } catch (e) { console.error('Mixer: loadRows failed', e); }
    },

    async loadActuators() {
      try {
        const r = await fetch('api/smart_dispatch/actuators');
        const d = await r.json();
        if (d.ok) this.actuators = d.rows;
      } catch (e) { console.error('Mixer: loadActuators failed', e); }
    },

    async loadDNA() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      try {
        // 1. Fetch gateway profile
        const r = await fetch(`api/gateways/${gw}`);
        const d = await r.json();
        
        // 2. Fetch utility services setup to map custom physical grid topology
        let matchedSvc = null;
        try {
          const uResp = await fetch('api/utility-services');
          const uData = await uResp.json();
          if (uData.ok && Array.isArray(uData.data)) {
            matchedSvc = uData.data.find(s => (s.gateways || []).includes(gw));
          }
        } catch (ue) {
          console.warn('Mixer: failed to load utility services for DNA topology', ue);
        }

        // The API returns the gateway object directly, with live status merged in
        if (d && d.short_id) {
           const cap = d.last_data?.capacity || {};
           
           // Default to live reported telemetry or Unknown
           let topology = cap.ac_type === 1 ? 'Split Phase' : (cap.ac_type === 3 ? 'Three Phase' : 'Unknown');
           
           // If user configured active grid utility service setup, override/enrich with exact parameters
           if (matchedSvc) {
             const wiringTypes = { 1: 'Single Phase', 2: 'Split Phase', 3: 'Three Phase' };
             const phaseLabel = wiringTypes[matchedSvc.ac_wiring_type] || topology;
             
             let details = [];
             if (matchedSvc.service_voltage_v) details.push(`${matchedSvc.service_voltage_v}V`);
             if (matchedSvc.service_amps) details.push(`${matchedSvc.service_amps}A`);
             
             topology = phaseLabel;
             if (details.length > 0) {
               topology += ` (${details.join(', ')})`;
             }
           }

           this.dna = {
             ac_type: topology,
             grid_max: cap.max_grid_charge_kw || 0,
             grid_feed_max: cap.max_grid_feed_kw || 0,
             solar_control: d.last_data?.solar_relay1 !== undefined ? 'Enabled' : 'Disabled',
             accessories: d.accessories || [],
             battery_capacity_kwh: (d.batteries || []).reduce((acc, b) => acc + (b.rated_kwh || 0), 0)
           };
        }
      } catch (e) { console.error('Mixer: loadDNA failed', e); }
    },

    async toggleRow(row) {
      row.enabled = row.enabled ? 0 : 1;
      // Do NOT save to DB immediately; it is now a local in-memory change.
    },

    async saveRowSilent(row) {
      try {
        const r = await fetch('api/smart_dispatch/matrix', {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json',
            'x-session-token': this.sessionToken
          },
          body: JSON.stringify(row)
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          if (!row.id) row.id = d.id;
          return true;
        }
      } catch (e) {
        console.error('saveRowSilent failed for row', row, e);
      }
      return false;
    },

    async saveRow(row) {
      this.saving = true;

      // 1. Perform any queued database deletions
      if (this.deletedRowIds && this.deletedRowIds.length > 0) {
        for (const deleteId of this.deletedRowIds) {
          try {
            await fetch(`api/smart_dispatch/matrix/${deleteId}`, { method: 'DELETE' });
          } catch(e) { console.error('Mixer: pending delete failed', e); }
        }
        this.deletedRowIds = [];
      }

      // 2. Identify new or changed/reordered rows to save
      const cleanRows = this._cleanRowsSnapshot ? JSON.parse(this._cleanRowsSnapshot) : [];
      const savePromises = [];

      for (const r of this.rows) {
        // Serialize builder fields back to JSON if not in manual mode
        if (!r.use_manual_json) {
          r.conditions_json = JSON.stringify({
            field: r.builder_param,
            op: r.builder_op,
            value_mode: r.builder_value_mode || 'literal',
            value_ref: r.builder_value_ref || '',
            value: r.builder_value_mode === 'ref' ? '' : (isNaN(r.builder_val) || r.builder_val === '' ? r.builder_val : parseFloat(r.builder_val))
          });
          
          r.signals_json = JSON.stringify([{
            action_type: r.builder_action,
            params: {
              soc_max: r.builder_target_soc || 100,
              soc_min: r.builder_action === 'GRID_EXPORT' ? 20 : 0
            }
          }]);
        }

        const original = cleanRows.find(o => o.id === r.id);
        const isNew = !r.id;
        const isChanged = !original || 
                          original.strategy_name !== r.strategy_name ||
                          original.trigger_category !== r.trigger_category ||
                          original.conditions_json !== r.conditions_json ||
                          original.signals_json !== r.signals_json ||
                          original.eval_order !== r.eval_order ||
                          original.enabled !== r.enabled ||
                          original.use_manual_json !== r.use_manual_json ||
                          original.gateway_id !== r.gateway_id;

        if (isNew || isChanged) {
          savePromises.push(this.saveRowSilent(r));
        }
      }

      try {
        if (savePromises.length > 0) {
          await Promise.all(savePromises);
        }
        // Reload all rows to fetch database IDs & sync state cleanly
        await this.loadRows();
        Alpine.store('app').addToast('Strategy Matrix Saved successfully', 'info');
      } catch (e) { 
        Alpine.store('app').addToast('Failed to save strategy matrix', 'error');
      } finally {
        this.saving = false;
      }
    },

    deleteRow(row, index) {
      this.rowToDelete = row;
      this.indexToDelete = index;
    },

    async confirmDeleteRow() {
      const row = this.rowToDelete;
      const index = this.indexToDelete;
      if (row) {
        if (row.id) {
          if (!this.deletedRowIds) this.deletedRowIds = [];
          this.deletedRowIds.push(row.id);
        }
        this.rows.splice(index, 1);
        if (this.rows.length > 0) {
          this.activeRowIdx = Math.min(index, this.rows.length - 1);
        } else {
          this.activeRowIdx = null;
        }
      }
      this.rowToDelete = null;
      this.indexToDelete = null;
    },

    async seedDefaults() {
      if (!confirm('This will seed the matrix with 5 standard Amber Smart Dispatch rules. Continue?')) return;
      
      const defaults = [
        {
          strategy_name: 'Demand Charge Protection',
          trigger_category: 'custom',
          builder_param: 'demand_window',
          builder_op: 'EQ',
          builder_val: 'true',
          builder_value_mode: 'literal',
          builder_value_ref: '',
          builder_action: 'HOLD',
          builder_target_soc: 100,
          eval_order: 10
        },
        {
          strategy_name: 'Price Spike Discharge',
          trigger_category: 'price_spike',
          builder_param: 'spike_status',
          builder_op: 'EQ',
          builder_val: 'spike',
          builder_value_mode: 'literal',
          builder_value_ref: '',
          builder_action: 'GRID_EXPORT',
          builder_target_soc: 20,
          eval_order: 20
        },
        {
          strategy_name: 'Negative Price Lockout',
          trigger_category: 'pricing',
          builder_param: 'export_c_kwh',
          builder_op: 'LT',
          builder_val: '0',
          builder_value_mode: 'literal',
          builder_value_ref: '',
          builder_action: 'HOLD',
          builder_target_soc: 100,
          eval_order: 30
        },
        {
          strategy_name: 'Export Bonus Booster',
          trigger_category: 'soc_management',
          builder_param: 'export_c_kwh',
          builder_op: 'GT',
          builder_val: '15',
          builder_value_mode: 'literal',
          builder_value_ref: '',
          builder_action: 'GRID_EXPORT',
          builder_target_soc: 40,
          eval_order: 40
        },
        {
          strategy_name: 'Low Price Grid Charge',
          trigger_category: 'custom',
          builder_param: 'import_c_kwh',
          builder_op: 'LT',
          builder_val: '10',
          builder_value_mode: 'literal',
          builder_value_ref: '',
          builder_action: 'GRID_CHARGE',
          builder_target_soc: 100,
          eval_order: 50
        }
      ];

      for (const d of defaults) {
        let row = {
          gateway_id: Alpine.store('app').selectedGateway || 'all',
          enabled: 1,
          use_manual_json: false,
          ...d
        };
        await this.saveRow(row);
      }
      await this.loadRows();
      Alpine.store('app').addToast('Standard rules seeded successfully', 'info');
    },

    addRow() {
      // Clear any active search query so that the newly created card is visible
      this.searchQuery = '';

      // Robustly compute the next evaluation order, parsing existing integer orders
      const orders = this.rows.map(r => parseInt(r.eval_order, 10)).filter(o => !isNaN(o));
      const nextOrder = orders.length ? Math.max(...orders) + 10 : 10;

      const newRow = {
        gateway_id: Alpine.store('app').selectedGateway || 'all',
        strategy_name: '', // Leave empty so the user is prompted to input a descriptive name
        trigger_category: 'custom',
        conditions_json: '{}',
        signals_json: '[]',
        eval_order: nextOrder,
        enabled: 1,
        expanded: true,
        use_manual_json: false,
        builder_param: 'import_c_kwh',
        builder_op: 'GT',
        builder_value_mode: 'literal',
        builder_value_ref: '',
        builder_val: '30',
        builder_action: 'GRID_CHARGE',
        builder_target_soc: 100
      };

      this.rows.push(newRow);
      this.activeRowIdx = this.rows.length - 1;

      // Smoothly scroll and highlight the newly added strategy card on next DOM render tick
      this.$nextTick(() => {
        const cards = document.querySelectorAll('.mixer-strategy-card');
        if (cards.length > 0) {
          const lastCard = cards[cards.length - 1];
          lastCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
          
          // Apply a vibrant highlight glow animation
          lastCard.classList.add('ring-2', 'ring-indigo-500/80', 'shadow-2xl', 'shadow-indigo-500/20');
          
          // Clear standard styling after the highlight duration
          setTimeout(() => {
            lastCard.classList.remove('ring-2', 'ring-indigo-500/80');
          }, 3000);
        }
      });
    },

    async moveUp(idx) {
      if (idx <= 0) return;
      const temp = this.rows[idx];
      this.rows[idx] = this.rows[idx - 1];
      this.rows[idx - 1] = temp;
      
      this.rows.forEach((r, i) => {
        r.eval_order = (i + 1) * 10;
      });
      
      this.activeRowIdx = idx - 1;
    },

    async moveDown(idx) {
      if (idx >= this.rows.length - 1) return;
      const temp = this.rows[idx];
      this.rows[idx] = this.rows[idx + 1];
      this.rows[idx + 1] = temp;
      
      this.rows.forEach((r, i) => {
        r.eval_order = (i + 1) * 10;
      });
      
      this.activeRowIdx = idx + 1;
    },

    expandAll(state) {
      this.rows.forEach(r => r.expanded = state);
    },

    get activeRow() {
      return this.rows[this.activeRowIdx] ?? null;
    },

    get filteredRows() {
      if (!this.searchQuery) return this.rows;
      const q = this.searchQuery.toLowerCase();
      return this.rows.filter(r => 
        (r.strategy_name || '').toLowerCase().includes(q) ||
        (r.trigger_category || '').toLowerCase().includes(q)
      );
    },

    async checkUnlockStatus() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      
      // Load saved token from localStorage
      const cachedToken = localStorage.getItem(`sd_session_token_${gw}`) || '';
      this.sessionToken = cachedToken;

      try {
        const r = await fetch(`api/smart_dispatch/unlock/status?gateway_id=${gw}`);
        const d = await r.json();
        if (d.unlocked && d.seconds_remaining > 0) {
          this.unlocked = true;
          this.secondsRemaining = d.seconds_remaining;
          this.expiresAt = d.expires_at;
          this.startCountdown();
        } else {
          this.unlocked = false;
          this.secondsRemaining = 0;
          this.expiresAt = null;
          if (this.countdownInterval) {
            clearInterval(this.countdownInterval);
            this.countdownInterval = null;
          }
        }
      } catch (e) {
        console.error('Mixer: checkUnlockStatus failed', e);
      }
    },

    startCountdown() {
      if (this.countdownInterval) {
        clearInterval(this.countdownInterval);
      }
      this.countdownInterval = setInterval(async () => {
        if (this.secondsRemaining > 0) {
          this.secondsRemaining--;
        } else {
          clearInterval(this.countdownInterval);
          this.countdownInterval = null;
          this.unlocked = false;
          this.expiresAt = null;
          this.sessionToken = '';
          const gw = Alpine.store('app').selectedGateway;
          if (gw) {
            localStorage.removeItem(`sd_session_token_${gw}`);
            await this.loadRows();
          }
          Alpine.store('app').addToast('Safeguard override session expired. UI Locked.', 'warn');
        }
      }, 1000);
    },

    get formattedTimeRemaining() {
      if (this.secondsRemaining <= 0) return '00:00:00';
      const h = Math.floor(this.secondsRemaining / 3600).toString().padStart(2, '0');
      const m = Math.floor((this.secondsRemaining % 3600) / 60).toString().padStart(2, '0');
      const s = (this.secondsRemaining % 60).toString().padStart(2, '0');
      return `${h}:${m}:${s}`;
    },

    async generateOtpPin() {
      this.loadingOtp = true;
      this.pinError = '';
      try {
        const r = await fetch('api/smart_dispatch/generate-pin', { method: 'POST' });
        const d = await r.json();
        if (d.pin) {
          this.generatedPin = d.pin;
        } else {
          this.pinError = 'Could not generate test PIN';
        }
      } catch (e) {
        this.pinError = 'Connection error generating test PIN';
      } finally {
        this.loadingOtp = false;
      }
    },

    async unlockChallenge() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      if (!this.otpPin || this.otpPin.length !== 6) {
        this.pinError = 'Please enter a valid 6-digit PIN';
        return;
      }
      this.loadingOtp = true;
      this.pinError = '';
      try {
        const r = await fetch('api/smart_dispatch/unlock', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pin: this.otpPin, gateway_id: gw })
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          this.unlocked = true;
          this.sessionToken = d.session_token;
          localStorage.setItem(`sd_session_token_${gw}`, d.session_token);
          this.showOtpModal = false;
          this.otpPin = '';
          this.generatedPin = '';
          
          await this.checkUnlockStatus();
          await this.loadRows();
          Alpine.store('app').addToast('Safeguards temporarily unlocked', 'success');
        } else {
          this.pinError = d.detail || 'Invalid PIN';
        }
      } catch (e) {
        this.pinError = 'Failed to validate PIN. Check server logs.';
      } finally {
        this.loadingOtp = false;
      }
    },

    async lockSystem() {
      const gw = Alpine.store('app').selectedGateway;
      if (!gw) return;
      try {
        const r = await fetch('api/smart_dispatch/lock', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gateway_id: gw })
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          this.unlocked = false;
          this.secondsRemaining = 0;
          this.expiresAt = null;
          this.sessionToken = '';
          localStorage.removeItem(`sd_session_token_${gw}`);
          if (this.countdownInterval) {
            clearInterval(this.countdownInterval);
            this.countdownInterval = null;
          }
          await this.refreshAll();
          Alpine.store('app').addToast('Safeguards manually locked.', 'info');
        }
      } catch (e) {
        console.error('Failed to lock safeguards', e);
        Alpine.store('app').addToast('Lock request failed', 'error');
      }
    },

    parse(json) {
      try { return JSON.parse(json || '{}'); } catch(e) { return {}; }
    }
  };
}
window.sdStrategyMixer = sdStrategyMixer;
