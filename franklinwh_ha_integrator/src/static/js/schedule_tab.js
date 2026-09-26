/* schedule_tab.js — Schedule tab Alpine component */
function scheduleTab() {
  return {
    gateways: [],
    selectedGw: '',
    schedule: null,
    loading: false,
    error: null,
    editableSchedule: [],
    showAdvanced: false,
    quickAdd: { start: '08:00', end: '10:00', name: 'Boost', dispatchId: 8 },

    // ── Submit modal ─────────────────────────────────────────────
    showSubmitModal: false,
    submitting: false,
    submitForm: { reserve_soc: 20, operation: 0, default_strategy: 'SELF', default_tariff: 'OFF_PEAK' },
    // Result displayed inline in the modal after the API responds
    submitResult: null,   // null | { ok: bool, httpStatus: int, message: str }
    _submitAutoCloseTimer: null,

    // ── Pricing modal snapshot (for Cancel / Revert) ──────────────
    _pricingSnapshot: null,   // deep-clone of strategyList taken at openPricingModal()
    _pendingMerge: null,      // double-tap guard for mergeDayType

    // ── Nav guard modal (unsaved changes leaving the tab) ────────
    showNavGuardModal: false,
    _pendingNavTab: null,
    showValidateModal: false,
    validateResult: null,   // { pass: bool, issues: [] }

    // ── Forecast Chart State ─────────────────────────────────────
    showForecastChart: false,
    engineSlots: [],

    // Read-only mirror of the utility service's fixed charges, so a schedule
    // can be judged on what a day actually costs. Fetched, never stored and
    // never edited here — the service record owns it, and this screen only
    // reports what that record says.
    standingChargeCDay: 0,
    optimising: false,

    // ── Refresh modal ────────────────────────────────────────────
    showRefreshModal: false,
    refreshResult: null,    // { ok: bool, message: str } | null
    refreshCountdown: 0,
    _refreshTimer: null,

    // ── Preset state ─────────────────────────────────────────────
    presets: [],
    showSavePresetModal: false,
    presetName: '',
    presetDescription: '',
    savingPreset: false,
    presetSaveResult: null,   // { ok: bool, message: str } | null
    showLoadPresetModal: false,
    loadingPreset: false,

    // ── Editor source tracking ────────────────────────────────────
    // 'live'   = content loaded directly from device
    // 'preset' = a named preset was loaded
    // 'custom' = user started a new schedule (no load)
    loadedPresetName: '',    // name of the last loaded preset, '' = live/custom
    _cleanSnapshot: null,    // JSON snapshot of editableSchedule at last load — detects unsaved block edits
    _cleanStrategySnapshot: null, // JSON snapshot of strategyList at last load — detects unsaved pricing/name edits

    // ── Multi-season state (BKL-SCHED-01 Phase 1) ─────────────────
    // Stores the full strategyList from the API for safe round-tripping.
    // editableSchedule is always the active season/day-type slice for the editor.
    // On submit, editableSchedule is written back into the correct strategyList slot
    // before the full strategyList is sent — preserving all other seasons/day-types.
    strategyList: [],          // full multi-season structure from API (may be [])
    isMultiSeason: false,      // true → must use set_tou_schedule_multi on save
    activeSeason: 0,           // index into strategyList for today's month
    activeDayType: 1,          // 1=weekday 2=weekend (based on today)
    _pendingRemoveSeason: null,// double-tap guard for season remove button

    // ── Add Season inline form ───────────────────────────────────
    _addingSeasonName: '',     // input value while inline form is open
    _showAddSeasonForm: false, // true → show inline name entry

    // ── Copy rates guards ─────────────────────────────────────────
    _showCopyConfirm: false,        // true → show "Copy to ALL seasons" confirm overlay
    _showCopyDayTypeConfirm: false, // true → show "Copy to sibling day-type" confirm overlay

    // ── Calculate Savings ────────────────────────────────────────
    _savingsLoading: false,
    _savingsResult: null,      // { savings30, savings365 } or null

    // ── Snapshot history panel ───────────────────────────────────
    _showHistory: false,
    // Inline name entry for "snapshot -> preset". Declared here because an
    // undeclared property is not reactive in Alpine — the form would never open.
    // Optimiser output, shown in its own dialog rather than crammed into a
    // toast. See optimiseSchedule().
    showOptimiseModal: false,
    optimiseResult: null,

    _presetFromSnapshotId: null,
    _presetFromSnapshotName: '',
    _snapshotHistory: [],      // [{id, label, ts, season_count}]
    _historyTotal: 0,
    _historyOffset: 0,
    _historyPageSize: 20,
    _historyLoading: false,

    // ── CLI Table View (Gap 1) ─────────────────────────────────────
    showTableView: false,

    // ── TOU Health Check (Gap 2) ──────────────────────────────────
    touHealth: null,           // null | health result dict from /schedule/health
    touHealthLoading: false,
    showResetTouModal: false,
    resetTouResult: null,      // null | {ok, steps[], sync_cleared, error}
    resetTouLoading: false,

    // Init

    // ────────────────────────────────────────────────────────────
    async init() {
      await this.fetchDispatchCodes();
      const gws = await fetchJSON('api/gateways');
      if (gws) this.gateways = gws;
      if (this.gateways.length === 1) {
        this.selectedGw = this.gateways[0].short_id;
        await this.load();
      }

      // ── Dirty toast: poll every 1.5 s — catches deep object mutations
      // that Alpine's $watch might miss (nested strategyList property edits).
      // Batch R.1 (2026-08-02): timer id captured to this._dirtyPollTimer
      // so destroy() can clear it once R.2 makes tab unmount actually fire.
      let _wasClean = true;
      this._dirtyPollTimer = setInterval(() => {
        const nowDirty = this.isDirty();
        if (nowDirty && _wasClean) {
          _wasClean = false;
          const gwName = this.selectedGw || 'Gateway';
          this.$store.app.addToast(
            `Unsaved changes — submit to Gateway ${gwName} when ready`,
            'warn',
            { autoDismiss: true, dismissMs: 7000 }
          );
        } else if (!nowDirty) {
          _wasClean = true;
        }
      }, 1500);

      // ── Nav guard: intercept sidebar tab changes when dirty ──
      window._scheduleNavGuard = (toTab) => {
        if (toTab === 'schedule') return true;
        if (!this.isDirty()) return true;
        this._pendingNavTab = toTab;
        this.showNavGuardModal = true;
        return false;
      };

      // ── Live Chart Redraw Watchers ─────────────────────────────
      this.$watch('editableSchedule', () => {
        if (this.showForecastChart) this.renderForecastChart();
      }, { deep: true });
      this.$watch('showForecastChart', (val) => {
        if (val) setTimeout(() => this.renderForecastChart(), 150);
      });
    },

    // Called from the nav guard modal — user chose to leave anyway
    confirmNavAway() {
      const tab = this._pendingNavTab;
      this._pendingNavTab = null;
      this.showNavGuardModal = false;
      // Deregister guard so setTab can proceed cleanly
      window._scheduleNavGuard = null;
      this.$store.app.setTab(tab);
    },

    // Called from nav guard modal — user chose to stay
    cancelNavAway() {
      this._pendingNavTab = null;
      this.showNavGuardModal = false;
    },

    // ────────────────────────────────────────────────────────────
    // Load schedule from Cloud API (silent — no modal)
    // ────────────────────────────────────────────────────────────
    async load(background = false) {
      // Follows the selected gateway's service. Display only, so it is not
      // awaited — a slow lookup must not delay the schedule itself.
      this.loadStandingCharge();
      if (!this.selectedGw) return;
      if (!background) {
        this.loading = true; this.error = null; this.schedule = null;
      }
      
      this.fetchEngineForecast();

      const data = await fetchJSON('api/gateways/' + this.selectedGw + '/schedule');
      if (data?.error) {
        this.error = data.error;
      } else if (data) {
        this.schedule = data;

        // ── Store multi-season structure (BKL-SCHED-01 Phase 1) ──
        this.strategyList  = data.strategy_list    || [];
        this.isMultiSeason = data.is_multi_season  || false;
        this.activeSeason  = data.active_season_idx ?? 0;
        this.activeDayType = data.active_day_type  ?? 1;

        // tou_periods is the active season/day-type slice (backward compat)
        this.editableSchedule = JSON.parse(JSON.stringify(data.tou_periods || [])).map(p => {
          if (p.startHourTime === '24:00') p.startHourTime = '23:59';
          if (p.endHourTime === '24:00') p.endHourTime = '23:59';
          return p;
        });
        // Reset editor source — this is the live schedule from the device
        this.loadedPresetName = '';
        this._cleanSnapshot = JSON.stringify(this.editableSchedule);
        this._cleanStrategySnapshot = JSON.stringify(this.strategyList);

        // ── Synthesise Season 1 for flat schedules (no strategy_list from API) ──
        // Ensures the season section is always visible when schedule data exists.
        if (this.strategyList.length === 0 && this.editableSchedule.length > 0) {
          // Build dayType entry and lift rate fields from blocks up to dayType level.
          // FranklinWH stores rates on dayTypeVoList entries, not individual blocks.
          const synthDt = {
            dayType: 3,
            dayName: 'Everyday',
            detailVoList: JSON.parse(JSON.stringify(this.editableSchedule))
          };
          for (const block of this.editableSchedule) {
            const bk = this._waveRateKey(block.waveType);
            const sk = this._waveSellKey(block.waveType);
            if (block[bk] != null && synthDt[bk] == null) synthDt[bk] = block[bk];
            if (block[sk] != null && synthDt[sk] == null) synthDt[sk] = block[sk];
          }
          this.strategyList = [{
            seasonName: 'Season 1',
            month: '1,2,3,4,5,6,7,8,9,10,11,12',
            dayTypeVoList: [synthDt]
          }];
        }


        // ── Phase 3: Load usage type + tariff identity ──────────────
        await this._loadUsageType();
        if (this.usageType >= 2) await this.loadTariffIdentity();

        // ── TOU Health Check (Gap 2) — fire after schedule is loaded ──
        // Non-blocking: health fetch runs in background, does not block load()
        this.fetchTouHealth().catch(e => console.warn('[TOU Health] fetch error:', e));
      } else {
        if (!background) this.error = 'No data returned from server.';
      }
      if (!background) this.loading = false;
    },


    // ────────────────────────────────────────────────────────────
    // Refresh modal (with progress UX + countdown)
    // ────────────────────────────────────────────────────────────
    async openRefreshModal() {
      this.refreshResult = null;
      this.showRefreshModal = true;
      this.refreshCountdown = 30;
      if (this._refreshTimer) clearInterval(this._refreshTimer);
      this._refreshTimer = setInterval(() => {
        this.refreshCountdown--;
        if (this.refreshCountdown <= 0) {
          this.showRefreshModal = false;
          clearInterval(this._refreshTimer);
        }
      }, 1000);

      // Run the actual load
      await this.load();
      this.refreshResult = this.error
        ? { ok: false, message: this.error }
        : { ok: true, message: `Loaded ${this.editableSchedule.length} TOU block(s) from Cloud API.` };
    },

    // ────────────────────────────────────────────────────────────
    // Validate — client-side 1440-minute gap + coverage checker
    // ────────────────────────────────────────────────────────────
    _validateInline() {
      // Auto-sort before checking
      this.editableSchedule.sort((a, b) => a.startHourTime.localeCompare(b.startHourTime));

      const issues = [];
      let lastEnd = '00:00';

      this.editableSchedule.forEach((item, index) => {
        if (item.startHourTime !== lastEnd) {
          issues.push(`Gap at Block ${index + 1}: expected start ${lastEnd}, found ${item.startHourTime}.`);
        }
        const normEnd = (item.endHourTime === '23:59') ? '24:00' : item.endHourTime;
        if (normEnd !== '24:00' && normEnd <= item.startHourTime) {
          issues.push(`Block ${index + 1} invalid: start ${item.startHourTime} ≥ end ${item.endHourTime}.`);
        }
        lastEnd = normEnd;
      });

      if (this.editableSchedule.length === 0) {
        issues.push('No time blocks defined — add at least one block.');
      } else if (lastEnd !== '24:00' && lastEnd !== '23:59') {
        issues.push(`Schedule ends at ${lastEnd} instead of midnight (24:00).`);
      }

      return issues;
    },

    validateSchedule() {
      this.validateResult = null;
      this.showValidateModal = true;
      // 800ms simulated processing for perceived responsiveness
      setTimeout(() => {
        const issues = this._validateInline();
        this.validateResult = issues.length > 0
          ? { pass: false, issues }
          : { pass: true, message: 'Schedule is valid! Continuous 24-hour coverage verified.' };
      }, 800);
    },

    // ────────────────────────────────────────────────────────────
    // Quick Add
    // ────────────────────────────────────────────────────────────
    // ────────────────────────────────────────────────────────────
    // Quick Add — Refactored for Chronological Integrity & Backfill
    // ────────────────────────────────────────────────────────────
    doQuickAdd() {
      const newStart = (this.quickAdd.start || '').trim();
      const newEnd = (this.quickAdd.end || '').trim();
      
      if (!newStart || !newEnd) {
        this.$store.app.addToast('Start and End times are required', 'warn');
        return;
      }
      if (newStart >= newEnd) {
        this.$store.app.addToast('Invalid range: Start must be before End', 'error');
        return;
      }

      const newDispatchId = parseInt(this.quickAdd.dispatchId, 10);
      const newName = this.quickAdd.name || 'Quick Block';

      // 1. Resolve overlaps
      let reconciled = [];
      const newBlock = {
        startHourTime: newStart, endHourTime: newEnd,
        name: newName, waveType: 0,
        dispatchId: newDispatchId,
        maxChargeSoc: 100, minDischargeSoc: 0,
        gridChargeMax: 5000, gridDischargeMax: 5000, solarCutoff: 100
      };

      for (let block of this.editableSchedule) {
        const bStart = block.startHourTime;
        const bEnd = block.endHourTime;

        // No overlap
        if (bEnd <= newStart || bStart >= newEnd) {
          reconciled.push(block);
          continue;
        }

        // Partial overlap: Existing block starts before new block
        if (bStart < newStart) {
          reconciled.push({ ...block, endHourTime: newStart });
        }

        // Partial overlap: Existing block ends after new block
        if (bEnd > newEnd) {
          reconciled.push({ ...block, startHourTime: newEnd });
        }
      }

      reconciled.push(newBlock);

      // 2. Sort by start time, then end time
      reconciled.sort((a, b) => {
        const s = a.startHourTime.localeCompare(b.startHourTime);
        if (s !== 0) return s;
        return a.endHourTime.localeCompare(b.endHourTime);
      });

      // 3. Gap backfilling & Merge contiguous same-type blocks
      let finalized = [];
      let lastTime = '00:00';
      const selfConsumptionTemplate = {
        name: 'Self-Consumption', waveType: 0,
        dispatchId: 6,
        maxChargeSoc: 100, minDischargeSoc: 0,
        gridChargeMax: 5000, gridDischargeMax: 5000, solarCutoff: 100
      };

      for (let block of reconciled) {
        // Skip invalid/zero-length blocks (sanity)
        if (block.startHourTime >= block.endHourTime) continue;

        // Fill gap before this block
        if (block.startHourTime > lastTime) {
          finalized.push({ ...selfConsumptionTemplate, startHourTime: lastTime, endHourTime: block.startHourTime });
        }
        
        // Add the block (ensuring it starts AFTER or AT lastTime to prevent overlap bugs)
        const adjustedStart = block.startHourTime < lastTime ? lastTime : block.startHourTime;
        if (block.endHourTime > adjustedStart) {
          finalized.push({ ...block, startHourTime: adjustedStart });
          lastTime = block.endHourTime;
        }
      }

      // Fill final gap until 24:00
      if (lastTime < '24:00') {
        finalized.push({ ...selfConsumptionTemplate, startHourTime: lastTime, endHourTime: '24:00' });
      }

      // 4. Merge adjacent blocks with same dispatchId & waveType
      let merged = [];
      for (let i = 0; i < finalized.length; i++) {
        let curr = finalized[i];
        if (merged.length > 0) {
          let prev = merged[merged.length - 1];
          if (prev.endHourTime === curr.startHourTime && prev.dispatchId === curr.dispatchId && prev.waveType === curr.waveType) {
            prev.endHourTime = curr.endHourTime;
            continue;
          }
        }
        merged.push(curr);
      }

      this.editableSchedule = merged;
      // Auto-advance for next block
      this.quickAdd.start = this.quickAdd.end;
      
      this.$nextTick(() => {
        if (this.showForecastChart) this.renderForecastChart();
      });
      this.$store.app.addToast('Block merged into schedule', 'info');
    },

    // ────────────────────────────────────────────────────────────
    // Preset: Save
    // ────────────────────────────────────────────────────────────
    async openSavePresetModal() {
      // If a preset was loaded, pre-fill its name so "Save" becomes an overwrite-back
      this.presetName = this.loadedPresetName || '';
      this.presetDescription = '';
      this.presetSaveResult = null;
      this.savingPreset = false;
      await this.fetchPresets();
      this.showSavePresetModal = true;
    },

    async savePreset() {
      if (!this.presetName.trim()) {
        this.presetSaveResult = { ok: false, message: 'Preset name is required.' };
        return;
      }
      // Detect overwrite: name matches an existing preset but user hasn't confirmed
      const nameClean = this.presetName.trim();
      const isOverwrite = this.presets.some(p => p.name === nameClean);
      if (isOverwrite && !this._overwriteConfirmed) {
        this.presetSaveResult = {
          ok: null,  // null = warning (not error, not success)
          message: `"${nameClean}" already exists. Click Save again to overwrite it.`
        };
        this._overwriteConfirmed = true;
        return;
      }
      this._overwriteConfirmed = false;
      this.savingPreset = true;
      this.presetSaveResult = null;
      try {
        // Normalise 23:59 → 24:00 before saving
        const processed = this.editableSchedule.map(p => {
          const copy = { ...p };
          if (copy.endHourTime === '23:59' || copy.endHourTime === '00:00') copy.endHourTime = '24:00';
          return copy;
        });
        const res = await fetch('api/gateways/schedule/presets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: nameClean,
            description: this.presetDescription.trim(),
            schedule: processed
          })
        });
        const json = await res.json();
        if (json.success) {
          this.presetSaveResult = { ok: true, message: `Preset "${nameClean}" saved!` };
          // Update source tracking: the editor now represents this preset
          this.loadedPresetName = nameClean;
          this._cleanSnapshot = JSON.stringify(this.editableSchedule);
          this._cleanStrategySnapshot = JSON.stringify(this.strategyList);
          await this.fetchPresets();
          setTimeout(() => { this.showSavePresetModal = false; }, 1500);
        } else {
          this.presetSaveResult = { ok: false, message: json.detail || json.error || 'Failed to save preset.' };
        }
      } catch (e) {
        this.presetSaveResult = { ok: false, message: 'Network error: ' + e.message };
      } finally {
        this.savingPreset = false;
      }
    },

    // ────────────────────────────────────────────────────────────
    // Preset: Load
    // ────────────────────────────────────────────────────────────
    async openLoadPresetModal() {
      this.loadingPreset = false;
      await this.fetchPresets();
      this.showLoadPresetModal = true;
    },

    async fetchPresets() {
      try {
        const data = await fetchJSON('api/gateways/schedule/presets');
        if (data) this.presets = data.presets || [];
      } catch (e) {
        console.warn('Failed to fetch presets:', e);
      }
    },

    async loadPreset(name) {
      this.loadingPreset = true;
      try {
        const res = await fetch(`api/gateways/schedule/presets/${encodeURIComponent(name)}/load`, {
          method: 'POST'
        });
        const json = await res.json();
        if (json.success && Array.isArray(json.schedule)) {
          // ── Preset upscale / merge (BKL-SCHED-01 Phase 1) ────────────────
          // Old presets store a flat tou_periods list.
          // By NOT clearing strategyList, we allow the loaded blocks to replace
          // the time blocks in the currently active season and day-type only,
          // preserving any other seasons/day-types the user has configured.

          this.editableSchedule = json.schedule.map((p, i) => {
            const copy = { ...p, id: p.id || Date.now() + i };
            if (copy.endHourTime === '24:00') copy.endHourTime = '23:59';
            return copy;
          });
          // Track which preset is loaded and take a clean snapshot
          this.loadedPresetName = name;
          this._cleanSnapshot = JSON.stringify(this.editableSchedule);
          this._cleanStrategySnapshot = JSON.stringify(this.strategyList);
          this.showLoadPresetModal = false;
          this.$store.app.addToast(`Loaded preset: ${name}`, 'ok');
        } else {
          this.$store.app.addToast(json.detail || json.error || 'Failed to load preset.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast('Network error: ' + e.message, 'error');
      } finally {
        this.loadingPreset = false;
      }
    },


    async deletePreset(name) {
      // Double-confirm pattern (native confirm() blocked in HA ingress iframes)
      if (this._pendingDeletePreset !== name) {
        this._pendingDeletePreset = name;
        this.$store.app.addToast(`Tap delete again to remove "${name}"`, 'warn');
        setTimeout(() => { if (this._pendingDeletePreset === name) this._pendingDeletePreset = null; }, 4000);
        return;
      }
      this._pendingDeletePreset = null;
      try {
        const res = await fetch(`api/gateways/schedule/presets/${encodeURIComponent(name)}`, {
          method: 'DELETE'
        });
        const json = await res.json();
        if (json.success) {
          await this.fetchPresets();
          this.$store.app.addToast(`Deleted preset: ${name}`, 'ok');
        } else {
          this.$store.app.addToast(json.detail || json.error || 'Failed to delete preset.', 'error');
        }
      } catch (e) {
        this.$store.app.addToast('Network error: ' + e.message, 'error');
      }
    },

    // ────────────────────────────────────────────────────────────
    // Submit schedule to Cloud API (hard-blocked on validation failure)
    // ────────────────────────────────────────────────────────────
    // ────────────────────────────────────────────────────────────
    // saveToGateway() — entry point from the toolbar button.
    // Full 24h coverage → direct submit (no dialog).
    // Gaps detected    → open the gap-fill submit modal.
    // ────────────────────────────────────────────────────────────
    async saveToGateway() {
      const issues = this._validateInline();
      if (issues.length > 0) {
        // Has gaps or errors — open the submit modal so user can
        // review and pick a gap-fill default before confirming.
        this.submitResult = null;
        this.showSubmitModal = true;
        return;
      }
      // Full 24h, no gaps — submit directly without a dialog.
      await this.submitSchedule(false);
    },

    // ────────────────────────────────────────────────────────────
    // submitSchedule(fromModal)
    //   fromModal = true  → called from the gap-fill dialog button
    //   fromModal = false → called directly by saveToGateway()
    // ────────────────────────────────────────────────────────────
    async submitSchedule(fromModal = false) {
      if (!fromModal) {
        const issues = this._validateInline();
        if (issues.length > 0) {
          this.validateResult = { pass: false, issues };
          this.showValidateModal = true;
          return;
        }
      }

      this.submitting = true;
      this.submitResult = null;
      // For gap-fill modal path, keep the modal open to show spinner → result.
      // For direct (no-gap) path, do NOT open the modal — use toast feedback instead.
      if (fromModal) this.showSubmitModal = true;

      try {
        // ── Normalise 23:59 → 24:00 and ensure every block has 'name' ──
        const sanitise = (p) => {
          const c = { ...p };
          if (c.startHourTime === '23:59') c.startHourTime = '24:00';
          if (c.endHourTime   === '23:59') c.endHourTime   = '24:00';
          if (!c.name || !c.name.trim()) c.name = c.waveType != null
            ? ({ 0: 'Off-Peak', 1: 'Mid-Peak', 2: 'On-Peak', 4: 'Super Off-Peak' }[c.waveType] || 'Time Block')
            : 'Time Block';
          return c;
        };
        const schedCopy = JSON.parse(JSON.stringify(this.editableSchedule)).map(sanitise);

        let payload;

        if (this.strategyList && this.strategyList.length > 0) {
          const stratCopy = JSON.parse(JSON.stringify(this.strategyList));
          for (const season of stratCopy) {
            for (const dt of (season.dayTypeVoList || [])) {
              dt.detailVoList = (dt.detailVoList || []).map(sanitise);
            }
          }
          const season = stratCopy[this.activeSeason];
          if (season) {
            const dtEntry = (season.dayTypeVoList || []).find(d => d.dayType === this.activeDayType)
                         || (season.dayTypeVoList || [])[0];
            if (dtEntry) dtEntry.detailVoList = schedCopy;
          }
          payload = {
            strategy_list:  stratCopy,
            operation:      parseInt(this.submitForm.operation),
            default_mode:   this.submitForm.default_strategy,
            default_tariff: this.submitForm.default_tariff,
          };
        } else {
          payload = {
            schedule:       schedCopy,
            operation:      parseInt(this.submitForm.operation),
            default_mode:   this.submitForm.default_strategy,
            default_tariff: this.submitForm.default_tariff,
          };
        }

        const res = await fetch('api/gateways/' + this.selectedGw + '/schedule/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const httpStatus = res.status;
        const json = await res.json().catch(() => ({}));

        if (json.ok) {
          this._cleanSnapshot = JSON.stringify(this.editableSchedule);
          this._cleanStrategySnapshot = JSON.stringify(this.strategyList);
          
          if (fromModal) {
            // Modal is open — show result inside it
            this.submitResult = { ok: true, httpStatus, message: 'Schedule pushed successfully.' };
          } else {
            // Direct path — close any open modal, show toast only
            this.showSubmitModal = false;
            this.$store.app.addToast('Schedule pushed to gateway ✓', 'ok');
          }
        } else {
          const errText = json.detail || json.error || JSON.stringify(json) || 'Unknown error';
          // Always surface errors in the modal regardless of path
          this.submitResult = { ok: false, httpStatus, message: errText };
          this.showSubmitModal = true;
        }
      } catch (e) {
        this.submitResult = { ok: false, httpStatus: 0, message: 'Network error: ' + e.message };
        this.showSubmitModal = true;
      }
      this.submitting = false;
    },


    // ── Source/dirty helpers ──────────────────────────────────────
    isDirty() {
      if (!this._cleanSnapshot) return false;
      const blocksDirty = JSON.stringify(this.editableSchedule) !== this._cleanSnapshot;
      const stratDirty  = this._cleanStrategySnapshot != null &&
                          JSON.stringify(this.strategyList) !== this._cleanStrategySnapshot;
      return blocksDirty || stratDirty;
    },

    sourceLabel() {
      if (this.loadedPresetName) return `Preset: "${this.loadedPresetName}"`;
      return 'Live device schedule';
    },

    sourceIcon() {
      if (this.loadedPresetName) return 'fa-bookmark';
      return 'fa-satellite-dish';
    },

    // ── Phase 2: Season & Day-Type Management (BKL-SCHED-01) ──────
    // ──────────────────────────────────────────────────────────────

    // Returns a display string like "Jun–Oct" or "Jan, Feb, Mar" from season.month
    seasonMonthRange(season) {
      if (!season) return '';
      const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const raw = season.month || season.months || '';
      if (!raw) return '';
      const nums = raw.split(',').map(m => parseInt(m.trim())).filter(n => !isNaN(n)).sort((a,b) => a-b);
      if (nums.length === 0) return '';
      if (nums.length === 1) return MONTHS[nums[0]-1];
      // If consecutive range, show start–end
      let isRange = true;
      for (let i = 1; i < nums.length; i++) {
        if (nums[i] !== nums[i-1] + 1) { isRange = false; break; }
      }
      if (isRange) return `${MONTHS[nums[0]-1]}–${MONTHS[nums[nums.length-1]-1]}`;
      // Non-consecutive: show all abbreviated
      if (nums.length <= 4) return nums.map(n => MONTHS[n-1]).join(', ');
      return `${nums.length} months`;
    },

    // Returns true if today's month falls within this season's month list
    isActiveSeason(season) {
      const raw = season?.month || season?.months || '';
      if (!raw) return false;
      const todayMonth = new Date().getMonth() + 1; // 1-indexed
      return raw.split(',').map(m => parseInt(m.trim())).includes(todayMonth);
    },

    // Switch to a different season — saves current editable slice back first
    selectSeason(idx) {
      if (idx === this.activeSeason) return;
      // Write current editableSchedule back into the strategyList slot before switching
      this._flushEditableToStrategyList();
      this.activeSeason = idx;
      // Load the new season's active day-type slice
      this.showPriceStrip = false;   // collapse when season changes
      this._loadSliceFromStrategyList();
    },

    // Switch to a different day-type within the current season
    selectDayType(dayType) {
      if (dayType === this.activeDayType) return;
      this._flushEditableToStrategyList();
      this.activeDayType = dayType;
      this.showPriceStrip = false;   // collapse when daytype changes
      this._loadSliceFromStrategyList();
    },

    // Write current editableSchedule back into the correct strategyList slot
    _flushEditableToStrategyList() {
      if (!this.strategyList || this.strategyList.length === 0) return;
      const season = this.strategyList[this.activeSeason];
      if (!season) return;
      const dayTypeVos = season.dayTypeVoList || [];
      const dtEntry = dayTypeVos.find(d => d.dayType === this.activeDayType) || dayTypeVos[0];
      if (!dtEntry) return;
      // Normalise 23:59 → 24:00 before persisting in the structure
      dtEntry.detailVoList = JSON.parse(JSON.stringify(this.editableSchedule)).map(p => {
        const c = { ...p };
        if (c.endHourTime === '23:59') c.endHourTime = '24:00';
        if (c.startHourTime === '23:59') c.startHourTime = '24:00';
        return c;
      });
    },

    // Load the editableSchedule from the current activeSeason + activeDayType slice
    _loadSliceFromStrategyList() {
      if (!this.strategyList || this.strategyList.length === 0) {
        this.editableSchedule = [];
        return;
      }
      const season = this.strategyList[this.activeSeason];
      if (!season) { this.editableSchedule = []; return; }
      const dayTypeVos = season.dayTypeVoList || [];
      const dtEntry = dayTypeVos.find(d => d.dayType === this.activeDayType) || dayTypeVos[0];
      const raw = (dtEntry?.detailVoList || []);
      this.editableSchedule = JSON.parse(JSON.stringify(raw)).map(p => {
        if (p.startHourTime === '24:00') p.startHourTime = '23:59';
        if (p.endHourTime === '24:00') p.endHourTime = '23:59';
        return p;
      });
      // Update activeDayType to match the entry we actually loaded (may have fallen back)
      if (dtEntry) this.activeDayType = dtEntry.dayType;
      this._cleanSnapshot = JSON.stringify(this.editableSchedule);
      this._cleanStrategySnapshot = JSON.stringify(this.strategyList);
    },

    // Toggle the inline "Add Season" name-entry form (replaces window.prompt which is blocked in HA ingress)
    addSeason() {
      if (this.strategyList.length >= 4) {
        this.$store?.app?.addToast('Maximum 4 seasons allowed.', 'warn');
        return;
      }
      this._addingSeasonName = 'Season ' + (this.strategyList.length + 1);
      this._showAddSeasonForm = true;
      // Focus the input on next tick
      this.$nextTick(() => { document.getElementById('addSeasonNameInput')?.focus(); });
    },

    // Confirm the inline Add Season form
    confirmAddSeason() {
      const name = (this._addingSeasonName || '').trim();
      if (!name) { this._showAddSeasonForm = false; return; }
      const allMonths = new Set([1,2,3,4,5,6,7,8,9,10,11,12]);
      const usedMonths = new Set(
        this.strategyList.flatMap(s => (s.month || s.months || '').split(',').map(m => parseInt(m.trim())))
      );
      const freeMonths = [...allMonths].filter(m => !usedMonths.has(m));
      if (freeMonths.length === 0) {
        this.$store?.app?.addToast('All 12 months are assigned — remove or edit an existing season first.', 'warn');
        this._showAddSeasonForm = false; return;
      }
      const newSeason = {
        seasonName: name,
        month: freeMonths.join(','),
        dayTypeVoList: [{
          dayType: 3, dayName: 'Everyday',
          detailVoList: [{
            name: 'Off-Peak', startHourTime: '00:00', endHourTime: '24:00',
            waveType: 0, dispatchId: 6, maxChargeSoc: 100, minDischargeSoc: 0,
            gridChargeMax: 5000, gridDischargeMax: 5000
          }]
        }]
      };
      this.strategyList.push(newSeason);
      this.isMultiSeason = true;
      this._showAddSeasonForm = false;
      this._addingSeasonName = '';
      this.selectSeason(this.strategyList.length - 1);
      this.$store?.app?.addToast(`Season "${name}" added — assign months and rate blocks.`, 'ok');
    },

    cancelAddSeason() {
      this._showAddSeasonForm = false;
      this._addingSeasonName = '';
    },

    // Remove a season (with safety check)
    removeSeason(idx) {
      if (this.strategyList.length <= 1) return;
      const name = this.strategyList[idx]?.seasonName || this.strategyList[idx]?.name || 'this season';
      // Double-confirm pattern (native confirm() blocked in HA ingress iframes)
      if (this._pendingRemoveSeason !== idx) {
        this._pendingRemoveSeason = idx;
        this.$store.app.addToast(`Tap ✕ again to remove "${name}" — unsaved until gateway save`, 'warn');
        setTimeout(() => { if (this._pendingRemoveSeason === idx) this._pendingRemoveSeason = null; }, 4000);
        return;
      }
      this._pendingRemoveSeason = null;
      this.strategyList.splice(idx, 1);
      // Adjust active season index if needed
      if (this.activeSeason >= this.strategyList.length) {
        this.activeSeason = this.strategyList.length - 1;
      }
      this.isMultiSeason = this.strategyList.length > 1 ||
        (this.strategyList[0]?.dayTypeVoList?.length > 1);
      this._loadSliceFromStrategyList();
    },

    // Add an opposing day-type to a season that only has one
    addDayType(seasonIdx) {
      const season = this.strategyList[seasonIdx];
      if (!season) return;
      const existing = (season.dayTypeVoList || []).map(d => d.dayType);
      // Add weekday if only weekend, add weekend if only weekday
      const newType = existing.includes(1) ? 2 : 1;
      const newName = newType === 1 ? 'Weekday' : 'Weekend';
      // Upgrade existing dayType=3 (Everyday) to explicit weekday/weekend split
      if (existing.includes(3)) {
        const everyEntry = season.dayTypeVoList.find(d => d.dayType === 3);
        if (everyEntry) everyEntry.dayType = 1;
        season.dayTypeVoList.push({
          dayType: 2,
          dayName: 'Weekend',
          detailVoList: JSON.parse(JSON.stringify(everyEntry?.detailVoList || [{
            name: 'Off-Peak', startHourTime: '00:00', endHourTime: '24:00',
            waveType: 0, dispatchId: 6, maxChargeSoc: 100, minDischargeSoc: 0,
            gridChargeMax: 5000, gridDischargeMax: 5000
          }]))
        });
      } else {
        // Clone existing blocks as a starting point for the new day-type
        const template = season.dayTypeVoList[0]?.detailVoList || [];
        season.dayTypeVoList.push({
          dayType: newType,
          dayName: newName,
          detailVoList: JSON.parse(JSON.stringify(template))
        });
      }
      this.isMultiSeason = true;
      this.selectDayType(newType);
    },

    // ────────────────────────────────────────────────────────────
    // Row management
    // ────────────────────────────────────────────────────────────
    addNewRow() {
      this.editableSchedule.push({
        startHourTime: '00:00', endHourTime: '06:00', name: 'New Block',
        waveType: 0, dispatchId: 1, maxChargeSoc: 100, minDischargeSoc: 0,
        gridChargeMax: 5000, gridDischargeMax: 5000, solarCutoff: 100
      });
    },

    removeRow(i) { this.editableSchedule.splice(i, 1); },

    // ────────────────────────────────────────────────────────────
    // Timeline helpers
    // ────────────────────────────────────────────────────────────
    timeToMins(t) {
      if (!t) return 0;
      const [h, m] = t.split(':');
      return parseInt(h) * 60 + parseInt(m);
    },

    calculateTimeline() {
      const blocks = [...(this.editableSchedule || [])].sort((a, b) => a.startHourTime.localeCompare(b.startHourTime));
      const segments = [];
      let cur = 0;
      for (const b of blocks) {
        const s = this.timeToMins(b.startHourTime);
        let e = this.timeToMins(b.endHourTime);
        if (e < s) e = 1440;
        if (s > cur) segments.push({ width: ((s - cur) / 1440) * 100, dispatchId: 6, waveType: 0, name: 'Default (Self)' });
        segments.push({ width: ((e - s) / 1440) * 100, dispatchId: b.dispatchId, waveType: b.waveType ?? 0, name: b.name });
        cur = e;
      }
      if (cur < 1440) segments.push({ width: ((1440 - cur) / 1440) * 100, dispatchId: 6, waveType: 0, name: 'Default (Self)' });
      return segments;
    },

    getDispatchColor(id) {
      return { 1: '#3b82f6', 2: '#9ca3af', 3: '#eab308', 6: '#14b8a6', 7: '#f97316', 8: '#22c55e' }[id] || '#9ca3af';
    },

    getDispatchShortName(id) {
      return { 1: 'LOAD', 2: 'STBY', 3: 'SOLR', 6: 'SELF', 7: 'EXPT', 8: 'CHRG' }[id] || 'UNK';
    },

    // One catalogue, fetched from the backend, rather than a literal repeated
    // here and in two more places in smart_dispatch.js. The old maps had
    // already drifted and none carried a description, so a dropdown reading
    // "Home Loads (1)" gave no way to know it leaves the battery alone and
    // sends surplus solar to the grid.
    dispatchCodes: [],

    async fetchDispatchCodes() {
      try {
        const data = await fetchJSON('api/dispatch-codes');
        if (data?.codes) this.dispatchCodes = data.codes;
      } catch (e) {
        console.warn('Failed to fetch dispatch codes:', e);
      }
    },

    dispatchCode(id) {
      return this.dispatchCodes.find(c => c.id === Number(id)) || null;
    },

    getDispatchName(id) {
      return this.dispatchCode(id)?.label
        || { 1: 'Home Loads', 2: 'Standby', 3: 'Solar Charging', 6: 'Self Consumption', 7: 'Grid Export', 8: 'Grid Charge' }[id]
        || 'Unknown';
    },

    // What choosing it is for, which is not what the label says: dispatch 1 is
    // labelled "Home Loads" and is in practice an export strategy.
    getDispatchIntent(id) {
      return this.dispatchCode(id)?.intent || this.getDispatchName(id);
    },

    getDispatchDescription(id) {
      return this.dispatchCode(id)?.description || '';
    },

    // waveType: 0=Off-Peak, 1=Mid-Peak (Shoulder), 2=On-Peak, 4=Super Off-Peak
    getWaveLabel(waveType) {
      return { 0: 'Off-Peak', 1: 'Mid-Peak', 2: 'On-Peak', 4: 'Super Off-Peak' }[waveType] ?? 'Off-Peak';
    },

    dayTypeLabel(dt) {
      return { 1: 'Weekdays (Mon\u2013Fri)', 2: 'Weekends (Sat\u2013Sun)', 3: 'Every Day' }[dt?.dayType]
             || dt?.dayName || 'All Days';
    },

    // Returns unique wave rates from a detailVoList for the pricing modal
    // Returns unique wave rates from a dayTypeVoList entry for the pricing modal.
    // Rates are stored on the dayType object (e.g. dt.eleticRatePeak), NOT on individual blocks.
    // This matches the FranklinWH Cloud API structure (cli_commands/tou.py _extract_rates).
    waveRateSummary(dt) {
      if (!dt) return [];
      const blocks  = dt.detailVoList || [];
      const seen    = new Set();
      const rates   = [];
      for (const b of blocks) {
        if (!seen.has(b.waveType)) {
          seen.add(b.waveType);
          const buyKey  = this._waveRateKey(b.waveType);
          const sellKey = this._waveSellKey(b.waveType);
          const rawBuy  = parseFloat(dt[buyKey]  ?? 0) || 0;
          const rawSell = parseFloat(dt[sellKey] ?? 0) || 0;
          rates.push({
            label:    this.getWaveLabel(b.waveType),
            waveType: b.waveType,
            buy:  '$' + rawBuy.toFixed(2),
            sell: '$' + rawSell.toFixed(2),
            rawBuy,
            rawSell,
          });
        }
      }
      return rates;
    },

    // Returns all 4 wave types for the pricing modal rate grid — always complete,
    // regardless of which wave types are actually used in the schedule blocks.
    // Rows for unused wave types are dimmed in the UI but remain fully editable.
    allWaveRates(dt) {
      const WAVES = [
        { waveType: 4, label: 'Super Off-Peak', color: '#3b82f6' },
        { waveType: 0, label: 'Off-Peak',       color: '#6b7280' },
        { waveType: 1, label: 'Mid-Peak',       color: '#f59e0b' },
        { waveType: 2, label: 'On-Peak',        color: '#ef4444' },
      ];
      const usedWaves = new Set((dt?.detailVoList || []).map(b => b.waveType));
      return WAVES.map(w => {
        const rawBuy  = parseFloat(dt?.[this._waveRateKey(w.waveType)] ?? 0) || 0;
        const rawSell = parseFloat(dt?.[this._waveSellKey(w.waveType)] ?? 0) || 0;
        return { ...w, rawBuy, rawSell, usedInBlocks: usedWaves.has(w.waveType) };
      });
    },

    // Returns the dayTypeVoList entry for the pricing modal.
    // Now directly uses activeSeason/activeDayType so pricing modal and main editor stay in sync.
    pricingDayTypeEntry() {
      const season = this.strategyList[this.activeSeason];
      if (!season) return null;
      const dts = season.dayTypeVoList || [];
      return dts.find(d => d.dayType === this.activeDayType) || dts[0] || null;
    },

    // Return buy or sell rate for a block from its parent dayType entry.
    // Use this in the pricing modal block table instead of reading from the block itself.
    rateFromDayType(dt, block, field) {
      const key = field === 'buy' ? this._waveRateKey(block.waveType) : this._waveSellKey(block.waveType);
      const val = dt[key];
      if (val == null) return '—';
      return '$' + parseFloat(val).toFixed(2);
    },

    // Returns the dayTypeVoList entry for the currently active season + dayType.
    // Used by the inline "Show Prices" strip to read rates for the current view.
    activeDayTypeEntry() {
      const season = this.strategyList[this.activeSeason];
      if (!season) return null;
      const dts = season.dayTypeVoList || [];
      return dts.find(d => d.dayType === this.activeDayType) || dts[0] || null;
    },


    // Set buy or sell rate on a dayTypeVoList entry directly.
    // Rates are stored at dt level (not on individual blocks) to match the API structure.
    // When pricingApplyAll is true, propagates the change to every daytype in every season.
    setWaveRate(dt, waveType, field, value) {
      // Enforce 2 decimal places (Australian tariff convention)
      const numVal = Math.max(0, parseFloat(parseFloat(value).toFixed(2)) || 0);
      const key = field === 'buy' ? this._waveRateKey(waveType) : this._waveSellKey(waveType);
      dt[key] = numVal;
      if (this.pricingApplyAll) {
        // Propagate to every daytype in every season
        for (const season of (this.strategyList || [])) {
          for (const dayType of (season.dayTypeVoList || [])) {
            dayType[key] = numVal;
          }
        }
      }
    },

    // Merge weekday/weekend split back to a single Everyday (dayType=3) entry.
    // Keeps weekday blocks as the template; weekend-only blocks are discarded.
    // Two-tap guard for Merge to All Days — prevents accidental merges
    mergeDayTypeGuard(seasonIdx) {
      if (this._pendingMerge !== seasonIdx) {
        this._pendingMerge = seasonIdx;
        this.$store?.app?.addToast('Tap "Merge to All Days" again to confirm — this removes the Weekday/Weekend split.', 'warn');
        setTimeout(() => { if (this._pendingMerge === seasonIdx) this._pendingMerge = null; }, 4000);
        return;
      }
      this._pendingMerge = null;
      this.mergeDayType(seasonIdx);
    },

    mergeDayType(seasonIdx) {
      const season = this.strategyList[seasonIdx];
      if (!season) return;
      const dayTypeVos = season.dayTypeVoList || [];
      if (dayTypeVos.length <= 1) return;
      // Use weekday entry as template for merged Everyday block
      const wdEntry = dayTypeVos.find(d => d.dayType === 1) || dayTypeVos[0];
      season.dayTypeVoList = [{
        dayType: 3,
        dayName: 'Everyday',
        detailVoList: JSON.parse(JSON.stringify(wdEntry?.detailVoList || []))
      }];
      this.activeDayType = 3;
      this._loadSliceFromStrategyList();
    },



    // ── Copy rates to ALL seasons/day-types (two-step guard) ───────
    // Step 1: show confirm overlay
    requestCopyRatesToAll() {
      this._showCopyConfirm = true;
    },
    // Step 2: perform the copy after user confirms — reads flat eleticRate/eleticSell keys
    copyRatesToAllSeasons() {
      const src = this.pricingDayTypeEntry();
      if (!src) { this._showCopyConfirm = false; return; }
      // Collect all rate keys present on the source dayType entry
      const RATE_KEYS = [
        'eleticRatePeak','eleticRateShoulder','eleticRateValley','eleticRateSuperOffPeak','eleticRateSharp','eleticRateGridFee',
        'eleticSellPeak','eleticSellShoulder','eleticSellValley','eleticSellSuperOffPeak','eleticSellSharp',
      ];
      for (const season of this.strategyList) {
        for (const dt of (season.dayTypeVoList || [])) {
          if (dt === src) continue; // skip source itself
          for (const key of RATE_KEYS) {
            if (src[key] != null) dt[key] = src[key];
          }
        }
      }
      this._showCopyConfirm = false;
      this.$store?.app?.addToast('Rates copied to all seasons and day-types.', 'ok');
    },

    // ── Copy rates to sibling day-type (Weekday ↔ Weekend) ──────────
    // Step 1: show confirm
    requestCopyRatesToSiblingDayType() {
      this._showCopyDayTypeConfirm = true;
    },
    // Step 2: perform copy to the sibling (other) day-type entry in the same season
    copyRatesToSiblingDayType() {
      const season = this.strategyList[this.activeSeason];
      const src = this.pricingDayTypeEntry();
      if (!season || !src) { this._showCopyDayTypeConfirm = false; return; }
      const RATE_KEYS = [
        'eleticRatePeak','eleticRateShoulder','eleticRateValley','eleticRateSuperOffPeak','eleticRateSharp','eleticRateGridFee',
        'eleticSellPeak','eleticSellShoulder','eleticSellValley','eleticSellSuperOffPeak','eleticSellSharp',
      ];
      let copied = 0;
      for (const dt of (season.dayTypeVoList || [])) {
        if (dt === src) continue;
        for (const key of RATE_KEYS) {
          if (src[key] != null) dt[key] = src[key];
        }
        copied++;
      }
      this._showCopyDayTypeConfirm = false;
      if (copied === 0) {
        this.$store?.app?.addToast('No sibling day-type to copy to — only one day-type in this season.', 'warn');
      } else {
        const siblingLabel = this.activeDayType === 1 ? 'Weekends' : this.activeDayType === 2 ? 'Weekdays' : 'all day-types';
        this.$store?.app?.addToast(`Rates copied to ${siblingLabel} in this season.`, 'ok');
      }
    },

    // ── Rate health helper ───────────────────────────────────────────
    // Returns true when a wave type is used in blocks but its buy rate is $0.00,
    // which signals rates were likely never set for this day-type.
    hasMissingRates(rate) {
      return rate.usedInBlocks && rate.rawBuy === 0;
    },

    // ── Month picker helpers ─────────────────────────────────────────
    // Returns which season index owns a given month (1–12), or -1 if unowned.
    monthOwner(month) {
      return this.strategyList.findIndex(s =>
        (s.month || s.months || '').split(',').map(m => parseInt(m.trim())).includes(month)
      );
    },
    // Check if a month is in the active (currently editing) season.
    monthInActiveSeason(month) {
      const s = this.strategyList[this.activeSeason];
      if (!s) return false;
      return (s.month || s.months || '').split(',').map(m => parseInt(m.trim())).includes(month);
    },
    // Toggle a month for the active season.
    toggleMonth(month) {
      const s = this.strategyList[this.activeSeason];
      if (!s) return;
      const nums = (s.month || s.months || '').split(',').map(m => parseInt(m.trim())).filter(Boolean);
      const owner = this.monthOwner(month);
      if (nums.includes(month)) {
        // Remove from this season (cannot remove last month)
        const remaining = nums.filter(m => m !== month);
        if (remaining.length === 0) {
          this.$store?.app?.addToast('A season must have at least one month.', 'warn');
          return;
        }
        s.month = remaining.sort((a,b)=>a-b).join(',');
      } else {
        if (owner !== -1) {
          // Steal from another season
          const other = this.strategyList[owner];
          const otherNums = (other.month || other.months || '').split(',').map(m=>parseInt(m.trim())).filter(Boolean);
          const remaining = otherNums.filter(m => m !== month);
          if (remaining.length === 0) {
            this.$store?.app?.addToast('Cannot steal — the other season would have no months left.', 'warn');
            return;
          }
          other.month = remaining.sort((a,b)=>a-b).join(',');
        }
        const updated = [...nums, month].sort((a,b)=>a-b);
        s.month = updated.join(',');
      }
    },

    // ── TOU Health Check (Gap 2) ─────────────────────────────────────
    async fetchTouHealth() {
      if (!this.selectedGw) return;
      this.touHealthLoading = true;
      try {
        const res = await fetchJSON(`api/gateways/${this.selectedGw}/schedule/health`);
        if (res && !res.error) {
          this.touHealth = res;
        } else {
          this.touHealth = null;
          console.warn('[TOU Health] error response:', res?.error || res?.detail);
        }
      } catch (e) {
        this.touHealth = null;
        console.warn('[TOU Health] fetch failed:', e);
      } finally {
        this.touHealthLoading = false;
      }
    },

    // CSS class for the TOU health badge
    touHealthClass() {
      const s = this.touHealth?.health_status;
      if (s === 'HEALTHY')  return 'bg-emerald-500/15 border-emerald-500/30 text-emerald-400';
      if (s === 'DEGRADED') return 'bg-amber-500/15 border-amber-500/30 text-amber-400';
      if (s === 'FAULT')    return 'bg-red-500/15 border-red-500/30 text-red-400';
      return 'bg-gray-700/40 border-gray-600 text-gray-500';
    },

    // Icon for the TOU health badge
    touHealthIcon() {
      const s = this.touHealth?.health_status;
      if (s === 'HEALTHY')  return 'fa-check-circle';
      if (s === 'DEGRADED') return 'fa-exclamation-triangle';
      if (s === 'FAULT')    return 'fa-times-circle';
      return 'fa-heartbeat';
    },

    // Perform TOU mode reset — requires confirmed:true sent to /health/reset
    async resetTou() {
      if (!this.selectedGw || this.resetTouLoading) return;
      this.resetTouLoading = true;
      this.resetTouResult = null;
      try {
        const res = await fetch(`api/gateways/${this.selectedGw}/schedule/health/reset`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirmed: true })
        });
        const json = await res.json();
        if (res.ok && json.ok) {
          this.resetTouResult = { ok: true, steps: json.steps || [], sync_cleared: json.sync_cleared };
          this.$store?.app?.addToast('TOU reset complete — schedule sync re-established.', 'ok');
          // Re-fetch health badge
          setTimeout(() => this.fetchTouHealth(), 2000);
        } else {
          const errMsg = json.detail || json.error || 'Reset failed';
          this.resetTouResult = { ok: false, error: errMsg, steps: json.steps || [] };
          this.$store?.app?.addToast('TOU reset failed: ' + errMsg, 'error');
        }
      } catch (e) {
        this.resetTouResult = { ok: false, error: 'Network error: ' + e.message, steps: [] };
        this.$store?.app?.addToast('TOU reset error: ' + e.message, 'error');
      } finally {
        this.resetTouLoading = false;
      }
    },

    // ── Calculate Savings (read-only Cloud API call) ───────────────────
    async calculateSavings() {
      if (!this.selectedGw || this._savingsLoading) return;
      this._savingsLoading = true;
      this._savingsResult = null;
      try {
        const body = { strategy_list: this.strategyList };
        const res = await fetchJSON(`api/gateways/${this.selectedGw}/schedule/calculate-savings`, {
          method: 'POST',
          body: JSON.stringify(body),
        });
        if (res?.ok) {
          this._savingsResult = res.data;
        } else {
          this.$store?.app?.addToast(res?.error || 'Savings estimate failed', 'warn');
        }
      } catch(e) {
        this.$store?.app?.addToast('Savings estimate API error: ' + e.message, 'error');
      } finally {
        this._savingsLoading = false;
      }
    },

    // ── Snapshot History  ────────────────────────────────────────────
    async loadSnapshotHistory(offset = 0) {
      if (!this.selectedGw) return;
      this._historyLoading = true;
      try {
        const res = await fetchJSON(
          `api/gateways/${this.selectedGw}/schedule/snapshots`
          + `?limit=${this._historyPageSize}&offset=${offset}`);
        if (res?.ok) {
          this._snapshotHistory = res.snapshots || [];
          this._historyTotal = res.total ?? this._snapshotHistory.length;
          this._historyOffset = res.offset ?? offset;
        }
      } finally {
        this._historyLoading = false;
      }
    },

    get _historyHasNext() {
      return this._historyOffset + this._historyPageSize < this._historyTotal;
    },

    historyPage(delta) {
      const next = Math.max(0, this._historyOffset + delta * this._historyPageSize);
      if (next >= this._historyTotal && delta > 0) return;
      return this.loadSnapshotHistory(next);
    },

    /** Delete one restore point.
     *
     * Confirmed because it cannot be undone: a snapshot is the only record of
     * what the gateway held at that moment, and nothing else keeps a copy.
     */
    async deleteSnapshot(snapshotId, label) {
      if (!await this.$store.app.confirm({
        title: 'Delete this snapshot?',
        body: label || `Snapshot ${snapshotId}`,
        detail: 'This is a restore point. Deleting it cannot be undone, and '
              + 'nothing else keeps a copy of that schedule.',
        confirmLabel: 'Delete', danger: true,
      })) return;

      const res = await fetchJSON(
        `api/gateways/${this.selectedGw}/schedule/snapshots/${snapshotId}`,
        { method: 'DELETE' });
      if (res?.ok) {
        this.$store.app.addToast('Snapshot deleted.', 'info');
        await this.loadSnapshotHistory(this._historyOffset);
      } else {
        this.$store.app.addToast(res?.error || 'Delete failed', 'error');
      }
    },

    // Promote a snapshot into a preset. Snapshots and presets held the same
    // schedules in different shapes with no route between them, so a known-good
    // schedule captured before a change could be restored but never reused by
    // an automation or the MQTT select.
    //
    // window.prompt is blocked under HA ingress, so the name is collected by
    // the same inline form pattern as Add Season.
    promptSnapshotPreset(snapshotId, label) {
      this._presetFromSnapshotId = snapshotId;
      this._presetFromSnapshotName = (label || `Snapshot ${snapshotId}`)
        .replace(/^Saved\s+/, '').trim();
      this.$nextTick(() => { document.getElementById('snapPresetNameInput')?.focus(); });
    },

    cancelSnapshotPreset() {
      this._presetFromSnapshotId = null;
      this._presetFromSnapshotName = '';
    },

    async saveSnapshotAsPreset() {
      const snapshotId = this._presetFromSnapshotId;
      const name = (this._presetFromSnapshotName || '').trim();
      if (!snapshotId || !this.selectedGw) return;
      if (!name) {
        this.$store?.app?.addToast('Give the preset a name.', 'warn');
        return;
      }

      const res = await fetchJSON(
        `api/gateways/${this.selectedGw}/schedule/snapshots/${snapshotId}/save-as-preset`,
        { method: 'POST', body: JSON.stringify({ name }) });

      if (!res?.ok) {
        this.$store?.app?.addToast(res?.error || res?.detail || 'Could not save preset', 'error');
        return;
      }

      this.cancelSnapshotPreset();
      await this.fetchPresets();

      // A preset is one flat day. If the snapshot had more than one season or
      // day type, only part of it came across — say which part, and say it
      // stickily, because the preset will look complete in the list.
      if (res.lossy) {
        const dropped = [...(res.dropped_seasons || []), ...(res.dropped_day_types || [])];
        this.$store?.app?.addToast(
          `Preset "${name}" saved from ${res.season} / ${res.day_type} `
          + `(${(res.blocks || []).length} blocks). Not included: ${dropped.join(', ')}. `
          + 'Marked unverified until you review it.',
          'warn', { autoDismiss: false });
      } else {
        this.$store?.app?.addToast(
          `Preset "${name}" saved (${(res.blocks || []).length} blocks).`, 'success');
      }
    },

    async restoreSnapshot(snapshotId) {
      if (!this.selectedGw) return;
      const res = await fetchJSON(`api/gateways/${this.selectedGw}/schedule/snapshots/${snapshotId}/restore`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (res?.ok) {
        this._showHistory = false;
        this.$store?.app?.addToast(
          'Snapshot loaded into the editor. Save to Gateway to apply it.',
          'warn', { autoDismiss: false });
        await this.load();
      } else {
        this.$store?.app?.addToast(res?.error || 'Restore failed', 'error');
      }
    },

    // Returns the season(s) to display in the pricing modal
    getPricingSeasons() {
      if (!this.strategyList || this.strategyList.length === 0) return [];
      if (this.pricingView === 'current') {
        const month = new Date().getMonth() + 1;
        const idx = this.strategyList.findIndex(s => {
          const ms = (s.month || s.months || '').split(',').map(m => parseInt(m.trim()));
          return ms.includes(month);
        });
        if (idx >= 0) return [this.strategyList[idx]];
      }
      return this.strategyList;
    },

    // Rate lookup that reads directly from block data (for pricing modal)
    rateFromBlock(block, field) {
      const key = field === 'buy' ? this._waveRateKey(block.waveType) : this._waveSellKey(block.waveType);
      const val = block[key];
      if (val == null) return '\u2014';
      return '$' + parseFloat(val).toFixed(3);
    },

    openPricingModal() {
      // Snapshot the full strategyList so Cancel can restore it
      this._pricingSnapshot = JSON.parse(JSON.stringify(this.strategyList));
      this._pendingMerge = null;
      this.pricingApplyAll = false;
      this.showPricingModal = true;
    },

    cancelPricingModal() {
      // Restore strategyList to the state it was in when the modal opened
      if (this._pricingSnapshot !== null) {
        this.strategyList = JSON.parse(JSON.stringify(this._pricingSnapshot));
        // Reload editable slice from the restored state
        this._loadSliceFromStrategyList();
        this.$store?.app?.addToast('Pricing changes discarded.', 'warn');
      }
      this._pricingSnapshot = null;
      this.showPricingModal = false;
    },

    closePricingModal() {
      // Keep changes — clear snapshot (don't restore)
      this._pricingSnapshot = null;
      this.showPricingModal = false;
      // Fire dirty toast immediately if pricing changes were made
      // (the setInterval poll may take up to 1.5s to catch deep mutations)
      if (this.isDirty()) {
        const gwName = this.selectedGw || 'Gateway';
        this.$store?.app?.addToast(
          `Unsaved changes \u2014 submit to Gateway ${gwName} when ready`,
          'warn',
          { autoDismiss: true, dismissMs: 7000 }
        );
      }
    },

    // ── Phase 3: Usage Type + Tariff Setup (BKL-SCHED-01) ─────────
    // ──────────────────────────────────────────────────────────────

    // ── R1: Usage Type state ──────────────────────────────────────
    usageType: null,          // null = not set (show prompt), 1/2/3
    showUsageTypeModal: false,

    // ── R5: Pricing modal state ───────────────────────────────────
    showPricingModal: false,
    showPriceStrip: false,             // inline rate strip for current schedule view
    pricingApplyAll: false,    // ☐ Apply rates to ALL seasons when editing

    // ── R2: Tariff Identity state ─────────────────────────────────
    tariffInfo: null,         // { name, utility, type, is_nbt } populated from API

    // ── R3: Tariff Setup Modal state ──────────────────────────────
    showTariffSetupModal: false,
    tariffSetupStep: 1,       // 1=type, 2=identity, 3=confirm
    tariffSetupForm: { type: 1, utility: '', name: '', is_nbt: false },
    tariffSaving: false,

    // ── Load usageType and tariffInfo during init ─────────────────
    async _loadUsageType() {
      if (!this.selectedGw) return;
      try {
        const data = await fetchJSON('api/gateways/' + this.selectedGw + '/schedule/usage-type');
        if (data) this.usageType = data.usage_type;
        // Show prompt automatically on first load if never set
        if (this.usageType === null) this.showUsageTypeModal = true;
      } catch (e) {
        console.warn('Failed to load usageType:', e);
      }
    },

    async loadTariffIdentity() {
      if (!this.selectedGw || this.usageType < 2) return;
      try {
        const data = await fetchJSON('api/gateways/' + this.selectedGw + '/schedule/tariff');
        if (data?.tariff) {
          this.tariffInfo = data.tariff;
          // Pre-populate tariff setup form with current values (for "Change Tariff")
          if (data.tariff.name) this.tariffSetupForm.name = data.tariff.name;
          if (data.tariff.utility) this.tariffSetupForm.utility = data.tariff.utility;
          if (data.tariff.electricity_type) this.tariffSetupForm.type = data.tariff.electricity_type;
          if (data.tariff.is_nbt) this.tariffSetupForm.is_nbt = data.tariff.is_nbt;
        }
      } catch (e) {
        console.warn('Failed to load tariff identity:', e);
      }
    },

    // ── R1: Set usage type and persist ───────────────────────────
    async setUsageType(type) {
      this.usageType = type;
      try {
        await fetch('api/gateways/' + this.selectedGw + '/schedule/usage-type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ usage_type: type })
        });
      } catch (e) {
        console.warn('Failed to persist usageType:', e);
      }
      // If switching to pricing mode, load tariff info
      if (type >= 2) await this.loadTariffIdentity();
    },

    // ── R3: Tariff Setup Modal controls ──────────────────────────
    openTariffSetup() {
      this.tariffSetupStep = 1;
      this.showTariffSetupModal = true;
    },

    async tariffSetupNext() {
      if (this.tariffSetupStep < 3) {
        this.tariffSetupStep++;
        return;
      }
      // Step 3 → Save
      this.tariffSaving = true;
      try {
        // Store tariff identity in app_config via the tariff endpoint
        const res = await fetch('api/gateways/' + this.selectedGw + '/schedule/tariff', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            utility: this.tariffSetupForm.utility,
            name: this.tariffSetupForm.name,
            electricity_type: this.tariffSetupForm.type,
            is_nbt: this.tariffSetupForm.is_nbt
          })
        });
        const json = await res.json();
        if (json.ok !== false) {
          // Refresh tariff card
          this.tariffInfo = {
            utility: this.tariffSetupForm.utility,
            name: this.tariffSetupForm.name,
            electricity_type: this.tariffSetupForm.type,
            is_nbt: this.tariffSetupForm.is_nbt
          };
          this.showTariffSetupModal = false;
          this.$store?.app?.addToast('Tariff setup saved.', 'ok');
        } else {
          this.$store?.app?.addToast('Failed to save tariff: ' + (json.detail || 'Unknown error'), 'error');
        }
      } catch (e) {
        this.$store?.app?.addToast('Error saving tariff: ' + e.message, 'error');
      } finally {
        this.tariffSaving = false;
      }
    },

    // ── R4: Per-block rate helpers (TOU only) ─────────────────────
    // Rates are stored per block using waveType as key.
    // Field names MUST match franklinwh_cloud/mixins/tou.py RATE_FIELD_MAP.
    // waveType: 0=Off-Peak, 1=Mid-Peak/Shoulder, 2=On-Peak, 4=Super Off-Peak
    _waveRateKey(waveType) {
      return {
        0: 'eleticRateValley',      // Off-Peak buy
        1: 'eleticRateShoulder',    // Mid-Peak / Shoulder buy
        2: 'eleticRatePeak',        // On-Peak buy
        4: 'eleticRateSuperOffPeak' // Super Off-Peak buy
      }[waveType] ?? 'eleticRateValley';
    },
    _waveSellKey(waveType) {
      return {
        0: 'eleticSellValley',      // Off-Peak sell
        1: 'eleticSellShoulder',    // Mid-Peak / Shoulder sell
        2: 'eleticSellPeak',        // On-Peak sell
        4: 'eleticSellSuperOffPeak' // Super Off-Peak sell
      }[waveType] ?? 'eleticSellValley';
    },

    rateForBlock(block, field) {
      if (this.tariffInfo?.electricity_type === 2) {
        // Fixed rate — return the single rate from tariff card (not per block)
        return field === 'buy' ? (this.tariffInfo.flat_buy ?? '—') : (this.tariffInfo.flat_sell ?? '—');
      }
      if (this.tariffInfo?.electricity_type !== 1) return '—';
      const key = field === 'buy' ? this._waveRateKey(block.waveType) : this._waveSellKey(block.waveType);
      const val = block[key];
      if (val === undefined || val === null) return '$0.000';
      return '$' + parseFloat(val).toFixed(3);
    },

    setRateForBlock(block, field, value) {
      if (this.tariffInfo?.electricity_type !== 1) return;
      const key = field === 'buy' ? this._waveRateKey(block.waveType) : this._waveSellKey(block.waveType);
      block[key] = parseFloat(value) || 0;
    },

    zeroAllSellRates() {
      this.editableSchedule.forEach(block => {
        const key = this._waveSellKey(block.waveType);
        block[key] = 0;
      });
    },

    // ── R5: CLI Table View helpers ────────────────────────────────
    // Returns all blocks across all seasons/day-types as a flat array for CLI view
    tableRows() {
      if (!this.strategyList || this.strategyList.length === 0) {
        return this.editableSchedule.map(b => ({
          season: 'All Year', dayType: 'All Days', ...b
        }));
      }
      const rows = [];
      for (const season of this.strategyList) {
        const seasonName = season.seasonName || season.name || 'Season';
        for (const dt of (season.dayTypeVoList || [])) {
          const dayName = dt.dayType === 1 ? 'Weekday' : dt.dayType === 2 ? 'Weekend' : (dt.dayName || 'All');
          // Rates live on the dayType entry, never on the block — see the
          // comment above _waveRateKey. Spreading the block alone produced a
          // row with no rate fields at all, so rateFromBlock() looked up an
          // undefined key and every Buy/Sell cell in this table rendered "—"
          // while the block list above showed the correct price from the same
          // data. It read as prices being lost; nothing was lost.
          const dtRates = {};
          for (const [k, v] of Object.entries(dt)) {
            if (k.startsWith('eleticRate') || k.startsWith('eleticSell')) dtRates[k] = v;
          }
          for (const block of (dt.detailVoList || [])) {
            rows.push({ season: seasonName, dayType: dayName, ...dtRates, ...block });
          }
        }
      }
      return rows;
    },

    copyTableToClipboard() {
      const showRates = this.usageType >= 2 && this.tariffInfo?.electricity_type === 1;
      const header = ['SEASON', 'DAY-TYPE', 'START', 'END', 'NAME', 'DISPATCH', 'WAVE'].concat(
        showRates ? ['BUY', 'SELL'] : []
      ).join('\t');
      const rows = this.tableRows().map(r =>
        [r.season, r.dayType, r.startHourTime, r.endHourTime,
         r.name || '',
         this.getDispatchName(r.dispatchId),
         this.getWaveLabel(r.waveType)
        ].concat(
          showRates ? [this.rateForBlock(r, 'buy'), this.rateForBlock(r, 'sell')] : []
        ).join('\t')
      );
      const text = [header, ...rows].join('\n');
      navigator.clipboard.writeText(text).then(() => {
        this.$store?.app?.addToast('Table copied to clipboard!', 'ok');
      }).catch(() => {
        this.$store?.app?.addToast('Copy failed — try selecting and copying manually.', 'error');
      });
    },

    // ── R3: Tariff card button handler ────────────────────────────
    openConfigurePricingModal() {
      // For TOU: enter inline rate editing mode (hint toast)
      if (this.tariffInfo?.electricity_type === 1 || !this.tariffInfo) {
        this.openPricingModal(); // Open $ Pricing modal for rate editing
      } else {
        this.openTariffSetup(); // For Fixed/Tiered, re-open the tariff setup to edit rates
      }
    },

    // ────────────────────────────────────────────────────────────
    // Live Energy Forecast Simulation Chart (00:00 - 24:00 Theoretical Day)
    // ────────────────────────────────────────────────────────────
    /** Fixed charges from the utility service linked to this gateway.
     *
     * Silent on failure: an absent supply charge means the line is simply not
     * shown, which is correct for a service that has none. A toast for a
     * missing optional figure would be noise on a screen that is about
     * something else.
     */
    /** Suggest a schedule from the tariff, saved as a preset.
     *
     * Two steps on purpose. The generator saves a preset and returns its
     * reasoning; loading it into the editor is a separate, explicit choice,
     * and reaching the battery needs Save to Gateway after that. A schedule
     * inferred from a rate table is a proposal about how a house runs — it
     * should never arrive without someone agreeing to it.
     */
    async optimiseSchedule() {
      if (!this.selectedGw || this.optimising) return;
      // Belt and braces with the x-show gate: a static tariff has known prices
      // to plan against. Real-time spot pricing is Smart Dispatch's job — it
      // reacts to the live price rather than committing to a day in advance.
      if (this.usageType !== 2) {
        Alpine.store('app').addToast(
          'Optimise works on static tariffs. For real-time pricing, use Smart Dispatch or an Automation.',
          'warn');
        return;
      }
      this.optimising = true;
      try {
        const r = await fetchJSON(
          `api/gateways/${this.selectedGw}/schedule/optimise?objective=lowest_bill`,
          { method: 'POST' });

        if (!r || r.ok === false) {
          Alpine.store('app').addToast(r?.error || 'Could not build a schedule from this tariff.', 'warn');
          return;
        }

        const name = r.saved_as || r.suggested_name || 'Optimised';
        const season = r.active_season ? ` for ${r.active_season}` : '';
        const solar = r.solar_informed ? '' : ' (no solar forecast — it may charge through daylight)';

        // The reasoning is the value of this feature, and it was being
        // delivered as ~900 characters of run-on text inside a toast — block
        // after block with no boundaries, unreadable and auto-dismissable.
        // It belongs in a table, one row per block, with the same Load action.
        this.optimiseResult = {
          name,
          season: r.active_season || '',
          solarInformed: !!r.solar_informed,
          solarSource: r.solar_source || 'none',
          soc: r.soc || {},
          socSufficient: r.soc_sufficient !== false,
          batteryKwh: r.battery_kwh || 0,
          seasons: (r.seasons || []).map(sn => ({
            name: sn.season_name,
            months: sn.months,
            soc: sn.soc || {},
            blocks: sn.blocks || [],
          })),
        };
        this.showOptimiseModal = true;
      } catch (e) {
        Alpine.store('app').addToast(`Optimise failed: ${e.message}`, 'error');
      } finally {
        this.optimising = false;
      }
    },

    /** Load the generated preset into the editor from the optimiser dialog.
     *
     * Still two steps: this populates the editor, and Save to Gateway is what
     * reaches the battery. A schedule inferred from a rate table is a proposal
     * about how a house runs and should never arrive without agreement.
     */
    async loadOptimisedPreset() {
      const name = this.optimiseResult?.name;
      if (!name) return;
      this.showOptimiseModal = false;
      await this.loadPreset(name);
      Alpine.store('app').addToast(
        'Loaded into the editor. Review it, then Save to Gateway to apply.',
        'warn', { autoDismiss: false });
    },

    async loadStandingCharge() {
      if (!this.selectedGw) return;
      try {
        // GET /utility-services returns every service with a `gateways` array;
        // there is no per-gateway endpoint, and inventing one for a read-only
        // display would be a second route to the same fact.
        const r = await fetchJSON('api/utility-services');
        const list = Array.isArray(r) ? r : (r?.data || r?.services || []);
        const svc = list.find(x => (x.gateways || []).includes(this.selectedGw));
        const fields = ['supply_charge_day', 'metering_fee', 'network_fixed_fee'];
        this.standingChargeCDay = fields.reduce(
          (sum, f) => sum + (parseFloat(svc?.[f]) || 0), 0);
      } catch (e) {
        this.standingChargeCDay = 0;
      }
    },

    async fetchEngineForecast() {
      if (!this.selectedGw) return;
      try {
        const data = await fetchJSON(`api/smart_dispatch/forecast?gateway_id=${this.selectedGw}`);
        if (data && Array.isArray(data.slots)) {
          this.engineSlots = data.slots;
          if (this.showForecastChart) this.renderForecastChart();
        }
      } catch (e) {
        console.warn('Failed to fetch engine forecast for chart overlay', e);
      }
    },

    renderForecastChart() {
      const canvas = document.getElementById('scheduleForecastChart');
      if (!canvas) return;

      const existingChart = Chart.getChart(canvas);
      if (existingChart) {
        existingChart.destroy();
      }

      // Generate a fixed 48-slot array representing 00:00 to 23:30
      const simSlots = [];
      for (let h = 0; h < 24; h++) {
        for (let m of [0, 30]) {
          const hhmm = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
          simSlots.push({ time: hhmm, h, m, pv_kw: 0, home_load_kw: 0 });
        }
      }

      // Map any available engine forecast data into the 24h fixed timeline
      if (this.engineSlots && this.engineSlots.length > 0) {
        this.engineSlots.forEach(s => {
          if (!s.start) return;
          const dt = new Date(s.start);
          const sh = dt.getHours();
          const sm = dt.getMinutes();
          // Find closest 30-min bin
          const binM = sm >= 15 && sm < 45 ? 30 : (sm >= 45 ? 0 : 0);
          const binH = sm >= 45 ? (sh + 1) % 24 : sh;
          
          const target = simSlots.find(slot => slot.h === binH && slot.m === binM);
          if (target) {
            target.pv_kw = s.pv_kw || 0;
            target.home_load_kw = s.home_load_kw || 0;
          }
        });
      }

      const labels = simSlots.map(s => s.time);
      const solarData = simSlots.map(s => s.pv_kw);
      const homeLoadData  = simSlots.map(s => -s.home_load_kw);

      const gw = Alpine.store('app').currentGateway || this.gateways.find(g => g.short_id === this.selectedGw);
      const batteryKw2 = (gw && gw.battery_count ? gw.battery_count : 1) * 5.0;
      const batteryCapacityKwh = (gw && gw.battery_count ? gw.battery_count : 1) * 13.6;

      const chargeData = new Array(simSlots.length).fill(0);
      const dischargeData = new Array(simSlots.length).fill(0);
      const socData = new Array(simSlots.length).fill(0);

      // Start the theoretical day at 10% SOC to simulate overnight drain
      let currentSoc = 10.0;

      // SIMULATION LOOP based on this.editableSchedule
      for (let i = 0; i < simSlots.length; i++) {
        socData[i] = currentSoc;

        let chargeKw = 0;
        let dischargeKw = 0;
        const s = simSlots[i];
        const slotMin = s.h * 60 + s.m;
        
        // Find active block in editableSchedule
        const p = this.editableSchedule.find(period => {
          const startMin = this.timeToMins(period.startHourTime);
          let endMin     = this.timeToMins(period.endHourTime);
          if (endMin <= startMin) endMin = 1440;
          return slotMin >= startMin && slotMin < endMin;
        });

        const dispatchId = p ? p.dispatchId : 4; // Default to Self-Consumption (4)
        const solarKw = s.pv_kw;
        const loadKw = s.home_load_kw;

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

        // Apply block SOC cutoffs if configured (Advanced)
        let localMaxCharge = 100;
        let localMinDischarge = 10;
        if (p) {
          if (p.dispatchId === 8 && p.maxChargeSoc != null) localMaxCharge = p.maxChargeSoc;
          if (p.dispatchId === 7 && p.minDischargeSoc != null) localMinDischarge = p.minDischargeSoc;
        }

        const maxEnergyKwh = batteryCapacityKwh * (localMaxCharge / 100);
        const minEnergyKwh = batteryCapacityKwh * (localMinDischarge / 100);

        if (newEnergyKwh > maxEnergyKwh) {
          chargeKw = Math.max(0, (maxEnergyKwh - currentEnergyKwh) / 0.5);
          newEnergyKwh = maxEnergyKwh;
        }
        if (newEnergyKwh < minEnergyKwh) {
          dischargeKw = Math.max(0, (currentEnergyKwh - minEnergyKwh) / 0.5);
          newEnergyKwh = minEnergyKwh;
        }

        chargeData[i] = chargeKw;
        dischargeData[i] = -dischargeKw;
        currentSoc = (newEnergyKwh / batteryCapacityKwh) * 100;
      }

      const solarKw2   = (this.tariffInfo?.solar_capacity || 5000) / 1000 * 0.85; // rough estimate
      const maxFlow    = Math.ceil(Math.max(batteryKw2, solarKw2) / 5) * 5 || 5;

      const annotations = {};
      const addAnnotation = (key, startIdx, endIdx, dispatchId, name) => {
        annotations[key] = {
          type: 'box', 
          xMin: startIdx - 0.5, 
          xMax: endIdx + 0.5, 
          yScaleID: 'y',
          backgroundColor: this.getDispatchColor(dispatchId) + '26',
          borderWidth: 0, 
          drawTime: 'beforeDatasetsDraw',
          label: {
            display: true, position: { x: 'center', y: 'start' }, content: name,
            font: { size: 9, weight: 'bold' }, color: 'rgba(255,255,255,0.4)', padding: 4
          }
        };
      };

      // Create background boxes based on editableSchedule
      let annotCount = 0;
      this.editableSchedule.forEach(p => {
        if (!p.startHourTime || !p.endHourTime) return;
        const startMin = this.timeToMins(p.startHourTime);
        let endMin     = this.timeToMins(p.endHourTime);
        if (endMin <= startMin) endMin = 1440;
        
        const startIdx = simSlots.findIndex(s => (s.h * 60 + s.m) >= startMin);
        let endIdx = simSlots.findLastIndex ? simSlots.findLastIndex(s => (s.h * 60 + s.m) < endMin) : -1;
        if (!simSlots.findLastIndex) {
          endIdx = simSlots.reduce((acc, s, idx) => (s.h * 60 + s.m) < endMin ? idx : acc, -1);
        }
        
        if (startIdx >= 0 && endIdx >= 0) {
          addAnnotation(`box${annotCount++}`, startIdx, endIdx, p.dispatchId, this.getDispatchName(p.dispatchId));
        }
      });
      
      // NOW line annotation overlay
      const now = new Date();
      const nowH = now.getHours();
      const nowM = now.getMinutes();
      const nowBinM = nowM >= 15 && nowM < 45 ? 30 : (nowM >= 45 ? 0 : 0);
      const nowBinH = nowM >= 45 ? ((nowH + 1) % 24) : nowH;
      const nowIdx = simSlots.findIndex(s => s.h === nowBinH && s.m === nowBinM);
      
      if (nowIdx !== -1) {
        annotations.nowLine = {
          type: 'line',
          xMin: nowIdx,
          xMax: nowIdx,
          yScaleID: 'y',
          borderColor: 'rgba(239, 68, 68, 0.7)',
          borderWidth: 2,
          borderDash: [5, 5],
          label: {
            display: true,
            content: 'NOW',
            position: 'end',
            color: 'rgba(239, 68, 68, 1)',
            font: { size: 10, weight: 'bold' },
            backgroundColor: 'rgba(0,0,0,0.5)',
            yAdjust: -20
          }
        };
      }

      new Chart(canvas, {
        type: 'bar',
        data: {
          labels,
          datasets: [
            { type: 'line', label: 'SOC%', data: socData, yAxisID: 'y1', borderColor: '#3b82f6', backgroundColor: '#3b82f6', borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.3 },
            { label: 'Solar PV', data: solarData, backgroundColor: '#facc15', yAxisID: 'y', stacked: true, maxBarThickness: 16 },
            { label: 'Charge', data: chargeData, backgroundColor: '#34d399', yAxisID: 'y', stacked: true, maxBarThickness: 16 },
            { label: 'Discharge', data: dischargeData, backgroundColor: '#f87171', yAxisID: 'y', stacked: true, maxBarThickness: 16 },
            { label: 'Home Load', data: homeLoadData, backgroundColor: '#818cf8', yAxisID: 'y', stacked: true, maxBarThickness: 16 }
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: true, position: 'top', align: 'end', labels: { boxWidth: 8, color: '#9ca3af', font: { size: 10 } } },
            annotation: { annotations }
          },
          scales: {
            x: { stacked: true, grid: { color: 'rgba(255,255,255,0.05)', drawBorder: false }, ticks: { color: '#9ca3af', maxTicksLimit: 12, maxRotation: 0 } },
            y: { stacked: true, type: 'linear', display: true, position: 'left', min: -maxFlow, max: maxFlow, grid: { color: 'rgba(255,255,255,0.05)', drawBorder: false }, ticks: { color: '#9ca3af', stepSize: 2, callback: v => (v > 0 ? '+' : '') + v + ' kW' } },
            y1: { type: 'linear', display: true, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: '#3b82f6', stepSize: 20, callback: v => v + '%' } }
          }
        }
      });
    },

    // Batch R.1 (2026-08-02): clear the 1.5s dirty-toast poll on tab
    // unmount. Currently dead code under x-show (never fires) — becomes
    // load-bearing once R.2 flips to x-if lazy mount.
    destroy() {
      if (this._dirtyPollTimer) { clearInterval(this._dirtyPollTimer); this._dirtyPollTimer = null; }
      if (this._refreshTimer)   { clearInterval(this._refreshTimer);   this._refreshTimer   = null; }
    }

  };
}
