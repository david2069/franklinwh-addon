// automations_tab.js — Alpine component for Edge Automations tab
// Includes compactMultiSelect() used by the Rule Builder modal

function compactMultiSelect() {
    return {
        open: false,
        search: '',
        selected: [],
        get availableGateways() {
            if (window.Alpine?.store('app')?.gateways) return window.Alpine.store('app').gateways;
            return this.$store?.app?.gateways || [];
        },
        get filteredGateways() {
            const term = this.search.toLowerCase();
            return this.availableGateways.filter(g =>
                (g.short_id  && g.short_id.toLowerCase().includes(term)) ||
                (g.full_serial && g.full_serial.toLowerCase().includes(term)) ||
                (g.name      && g.name.toLowerCase().includes(term))
            );
        },
        toggleAll(e) {
            if (e.target.checked) this.selected = ['ALL'];
            else this.selected = [];
            this.$data.builder.gateway_serial = this.selected.join(',');
        },
        toggleGw(e, id) {
            if (e.target.checked) {
                this.selected = this.selected.filter(x => x !== 'ALL');
                if (!this.selected.includes(id)) this.selected.push(id);
            } else {
                this.selected = this.selected.filter(x => x !== id);
            }
            this.selected = [...new Set(this.selected)];
            this.$data.builder.gateway_serial = this.selected.join(',');
        },
        init() {
            this.$watch('$data.builder.gateway_serial', val => {
                if (val && typeof val === 'string') {
                    const mapped = val.split(',').map(s => s.trim()).filter(Boolean);
                    if (JSON.stringify(this.selected) !== JSON.stringify(mapped)) this.selected = mapped;
                } else if (!val) { this.selected = []; }
            });
            const current_gw = this.$data.builder.gateway_serial;
            if (current_gw && current_gw !== 'ALL') {
                let parts = current_gw.split(',').map(s => s.trim()).filter(Boolean);
                if (this.availableGateways?.length) {
                    parts = parts.map(p => {
                        const hit = this.availableGateways.find(g => g.short_id === p || g.full_serial === p);
                        return hit ? hit.full_serial : p;
                    });
                }
                this.selected = [...new Set(parts)];
                this.$data.builder.gateway_serial = this.selected.join(',');
            } else if (current_gw === 'ALL') {
                this.selected = ['ALL'];
            }
        },
    };
}

function automationsTab() {
    return {
        view: 'rules',
        jobs: [], history: [], audit: [],
        loading: false, historyLoading: false, auditLoading: false,
        saving: false,
        searchRules: '', searchHistory: '', searchAudit: '',
        retentionDays: localStorage.getItem('fwh_scheduler_retention') || '30',
        visibleColumns: (() => {
            try {
                const stored = localStorage.getItem('fwh_rules_columns');
                if (stored) return JSON.parse(stored);
            } catch(e) {}
            return { name: true, tags: true, logic: true, payload: true };
        })(),
        saveColumnPreferences() {
            localStorage.setItem('fwh_rules_columns', JSON.stringify(this.visibleColumns));
        },
        cronSelection: '*/5 * * * *',
        cronValidation: { valid: true, message: '' },

        get filteredJobs() {
            if (!this.searchRules) return this.jobs;
            const t = this.searchRules.toLowerCase();
            return this.jobs.filter(j =>
                (j.name?.toLowerCase().includes(t)) ||
                (j.tags?.some(tg => tg.toLowerCase().includes(t))) ||
                (j.enabled ? 'enabled' : 'paused').includes(t)
            );
        },
        get filteredHistory() {
            if (!this.searchHistory) return this.history;
            const t = this.searchHistory.toLowerCase();
            return this.history.filter(r =>
                (r.rule_name?.toLowerCase().includes(t)) ||
                (r.action_type?.toLowerCase().includes(t)) ||
                (r.status?.toLowerCase().includes(t)) ||
                (r.detail?.toLowerCase().includes(t)) ||
                this.formatDate(r.timestamp).toLowerCase().includes(t)
            );
        },
        get filteredAudit() {
            if (!this.searchAudit) return this.audit;
            const t = this.searchAudit.toLowerCase();
            return this.audit.filter(r =>
                (r.source?.toLowerCase().includes(t)) ||
                (r.details?.toLowerCase().includes(t)) ||
                this.formatDate(r.timestamp).toLowerCase().includes(t)
            );
        },

        showBuilder: false,
        builderSize: 'default',
        showUnlockModal: false,
        showExecuteModal: false,
        executeResult: null,
        verifyingLogic: false,
        logicTestResult: null,
        executingJob: null,
        jobToDelete: null,
        unlockChallenge: '',
        userUnlockPin: '',
        editingJobId: null,
        executeSeconds: 0,
        executeInterval: null,
        executePollInterval: null,

        builder: {
            name: '', gateway_serial: 'ALL', trigger_type: 'condition', priority: 25,
            cron: '0 0 * * *', run_at: '', has_conditions: false,
            tags: '', condition_logic: 'AND', duration_str: '00:00:00',
            conditions: [{ metric: 'battery.soc', operator: '>', value: '', value_ref: '', value_mode: 'literal', enabled: true }],
            actions:    [{ type: 'set_smart_circuit', payload: { id: 1, state: 'on' }, enabled: true }],
        },

        // Smart Dispatch config for the active gateway — loaded on initTab and when gateway changes.
        // Used to pre-populate template default values instead of hardcoded numbers.
        dispatchCfg: {
            min_soc: 20,
            max_soc: 90,
            max_charge_price: 15,
            min_export_price: 10,
            export_bonus_threshold: 10,
            daily_earnings_target: 5,
            monthly_earnings_target: 0,
        },

        // (Home Loads catalog helpers live on window.__ABHomeLoads — registered
        //  once at module load below, outside the Alpine factory.)

        openNewRuleBuilder() {
            let def_gw = 'ALL';
            const gws = window.Alpine?.store('app')?.gateways || this.$store?.app?.gateways;
            if (gws?.length > 0) def_gw = gws[0].short_id;
            this.builder = {
                name: '', gateway_serial: def_gw, trigger_type: 'condition', priority: 25,
                cron: '0 0 * * *', run_at: '', has_conditions: false,
                tags: '', condition_logic: 'AND', duration_str: '00:00:00',
                enabled: true,
                conditions: [{ metric: 'battery.soc', operator: '>', value: '', value_ref: '', value_mode: 'literal', enabled: true }],
                actions:    [{ type: 'set_smart_circuit', payload: { id: 1, state: 'on' }, enabled: true }],
            };
            this.editingJobId = null; this.logicTestResult = null;
            // Load live config for the chosen gateway so template defaults reflect saved values
            this.loadDispatchCfg();
            this.initSelectionFromCron();
            this.showBuilder = true;
            setTimeout(() => this.$refs.ruleNameInput?.focus(), 100);
        },

        editJob(job) {
            this.editingJobId = job.id;
            const jobCron = (job.cron && job.cron !== 'unknown') ? job.cron : '*/1 * * * *';
            const isSchedule = jobCron !== '*/1 * * * *';
            const isOneOff = !!job.run_at;
            let finalTrigger = isOneOff ? 'one-off' : (isSchedule ? 'schedule' : 'condition');

            let localRunAt = '';
            if (job.run_at) {
                try {
                    const d = new Date(job.run_at);
                    localRunAt = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
                } catch {}
            }

            this.builder = {
                name: job.name,
                gateway_serial: job.gateway_serial || 'ALL',
                trigger_type: finalTrigger, priority: job.priority || 25,
                cron: isSchedule ? jobCron : '0 0 * * *',
                run_at: localRunAt,
                has_conditions: Array.isArray(job.conditions) && job.conditions.length > 0,
                tags: Array.isArray(job.tags) ? job.tags.join(', ') : (job.tags || ''),
                condition_logic: job.condition_logic || 'AND',
                duration_str: typeof job.duration_secs === 'number' ? this.formatDuration(job.duration_secs) : '00:00:00',
                enabled: job.enabled !== false,
                conditions: (job.conditions?.length > 0 ? JSON.parse(JSON.stringify(job.conditions)) : [{ metric: 'grid.status', operator: '==', value: '', value_ref: '', value_mode: 'literal', enabled: true }])
                    .map(c => {
                        let op = c.operator;
                        if (c.metric === 'pricing.sd_signal' && ['<','>','<=','>='].includes(op)) op = '==';
                        return { value_ref: '', value_mode: 'literal', ...c, operator: op, enabled: c.enabled !== false, value_mode: c.value_ref ? 'ref' : 'literal' };
                    }),
                actions: (JSON.parse(JSON.stringify(job.actions?.length ? job.actions : [{ type: 'set_smart_circuit', payload: { id: 1, state: 'on' } }])))
                    .map(a => ({ ...a, enabled: a.enabled !== false })),
            };
            this.logicTestResult = null;
            this.initSelectionFromCron();
            this.showBuilder = true;
            setTimeout(() => this.$refs.ruleNameInput?.focus(), 100);
        },

        addCondition() { this.builder.conditions.push({ metric: 'battery.soc', operator: '>=', value: '', value_ref: '', value_mode: 'literal', enabled: true }); },
        removeCondition(idx) { this.builder.conditions.splice(idx, 1); },
        toggleCondition(idx) {
            const c = this.builder.conditions[idx];
            c.enabled = c.enabled !== false ? false : true;
            this.builder.conditions = [...this.builder.conditions];
        },
        addAction() { this.builder.actions.push({ type: 'set_smart_circuit', payload: { id: 1, state: 'on' }, enabled: true }); },
        removeAction(idx) { this.builder.actions.splice(idx, 1); },
        toggleAction(idx) {
            const a = this.builder.actions[idx];
            a.enabled = a.enabled !== false ? false : true;
            this.builder.actions = [...this.builder.actions];
        },
        moveConditionUp(idx) {
            if (idx > 0) { [this.builder.conditions[idx-1], this.builder.conditions[idx]] = [this.builder.conditions[idx], this.builder.conditions[idx-1]]; this.builder.conditions = [...this.builder.conditions]; }
        },
        moveConditionDown(idx) {
            if (idx < this.builder.conditions.length - 1) { [this.builder.conditions[idx+1], this.builder.conditions[idx]] = [this.builder.conditions[idx], this.builder.conditions[idx+1]]; this.builder.conditions = [...this.builder.conditions]; }
        },
        moveActionUp(idx) {
            if (idx > 0) { [this.builder.actions[idx-1], this.builder.actions[idx]] = [this.builder.actions[idx], this.builder.actions[idx-1]]; this.builder.actions = [...this.builder.actions]; }
        },
        moveActionDown(idx) {
            if (idx < this.builder.actions.length - 1) { [this.builder.actions[idx+1], this.builder.actions[idx]] = [this.builder.actions[idx], this.builder.actions[idx+1]]; this.builder.actions = [...this.builder.actions]; }
        },
        updatePayload(act, strValue) {
            try { act.payload = JSON.parse(strValue); } catch(e) {}
        },

        getConditionResultStatus(idx) {
            if (!this.logicTestResult?.evaluations) return null;
            let passed = 0, failed = 0;
            for (const ev of this.logicTestResult.evaluations) {
                if (ev.states?.[idx] !== undefined) { ev.states[idx] ? passed++ : failed++; }
            }
            if (passed && !failed) return 'All Passed';
            if (!passed && failed) return 'All Failed';
            if (passed && failed) return 'Mixed';
            return null;
        },

        getTestButtonStyles() {
            if (this.verifyingLogic) return '';
            if (!this.logicTestResult) return 'border:2px solid #a855f7;color:#a855f7;animation:pulse-glow-purple 2s infinite;background:rgba(168,85,247,0.05)';
            if (this.logicTestResult.error) return 'border:2px solid var(--danger);color:var(--danger);animation:pulse-glow-red 2s infinite;background:rgba(239,68,68,0.05)';
            const allMatched = this.logicTestResult.evaluations?.every(e => e.matched);
            return allMatched ? 'background:var(--ok);color:white;border:2px solid var(--ok)' : 'border:2px solid var(--danger);color:var(--danger);animation:pulse-glow-red 2s infinite;background:rgba(239,68,68,0.05)';
        },

        applyTemplate(tpl) {
            if (!tpl) return;
            this.builder.trigger_type = 'condition';
            this.builder.has_conditions = true;
            this.builder.condition_logic = 'AND';
            this.builder.duration_str = '00:00:00';
            this.builder.tags = 'Smart Dispatch, Pricing';

            // Pull live config values so defaults aren't hardcoded
            const minSoc     = this.dispatchCfg.min_soc     ?? 20;
            const maxSoc     = this.dispatchCfg.max_soc     ?? 90;
            const maxChgPrc  = this.dispatchCfg.max_charge_price     ?? 15;
            const minExpPrc  = this.dispatchCfg.min_export_price     ?? 10;
            const expBonus   = this.dispatchCfg.export_bonus_threshold ?? 10;
            const dailyEarn  = this.dispatchCfg.daily_earnings_target ?? 5;

            if (tpl === 'demand_charge') {
                this.builder.name = 'Demand Charge Avoidance';
                this.builder.priority = 1;
                this.builder.conditions = [
                    { metric: 'pricing.demand_window_active', operator: '==', value: 'true', enabled: true },
                    { metric: 'battery.soc', operator: '>=', value: String(minSoc), enabled: true },
                ];
                this.builder.actions = [{ type: 'smart_dispatch.hold', payload: {}, enabled: true }];
            } else if (tpl === 'negative_export') {
                this.builder.name = 'Negative Export Avoidance';
                this.builder.priority = 2;
                this.builder.conditions = [
                    { metric: 'pricing.export_price_c_kwh', operator: '<', value: '0', enabled: true },
                    { metric: 'battery.soc', operator: '>=', value: String(maxSoc), enabled: true },
                ];
                this.builder.actions = [{ type: 'smart_dispatch.curtail_solar', payload: {}, enabled: true }];
            } else if (tpl === 'price_spike') {
                this.builder.name = 'Price Spike Response';
                this.builder.priority = 3;
                this.builder.conditions = [
                    { metric: 'pricing.spike_status', operator: '==', value: 'spike', enabled: true },
                    { metric: 'battery.soc', operator: '>', value: String(minSoc), enabled: true },
                ];
                this.builder.condition_logic = 'AND';
                this.builder.actions = [{ type: 'smart_dispatch.force_discharge', payload: { target_soc: String(minSoc) }, enabled: true }];
            } else if (tpl === 'export_bonus') {
                // Opportunistic export: capitalise on high feed-in tariff or solarSponge windows
                this.builder.name = 'Export Bonus / FiT Window';
                this.builder.priority = 4;
                this.builder.condition_logic = 'ANY';
                this.builder.conditions = [
                    { metric: 'pricing.export_price_c_kwh', operator: '>=', value: String(expBonus), enabled: true },
                    { metric: 'pricing.spike_status', operator: '==', value: 'potential', enabled: true },
                ];
                this.builder.actions = [{ type: 'smart_dispatch.force_discharge', payload: { target_soc: String(minSoc) }, enabled: true }];
            } else if (tpl === 'force_export') {
                // Deliberate Grid Export: discharge battery to grid during peak/spike or when price exceeds threshold
                this.builder.name = 'Force Export / Grid Discharge';
                this.builder.priority = 3;
                this.builder.condition_logic = 'ANY';
                this.builder.tags = 'Smart Dispatch, Export, Peak';
                this.builder.conditions = [
                    { metric: 'pricing.export_price_c_kwh', operator: '>=', value: String(minExpPrc), enabled: true },
                    { metric: 'pricing.spike_status',       operator: '==', value: 'spike',            enabled: true },
                ];
                this.builder.actions = [{
                    type: 'smart_dispatch.force_discharge',
                    payload: { target_soc: String(minSoc), power_limit_kw: 'Max', duration_mins: 30 },
                    enabled: true,
                }];
            } else if (tpl === 'earnings_target') {
                this.builder.name = 'Earnings Target Met';
                this.builder.priority = 5;
                this.builder.conditions = [{ metric: 'dispatch.daily_earnings', operator: '>=', value: String(dailyEarn), enabled: true }];
                this.builder.actions = [{ type: 'smart_dispatch.hold', payload: {}, enabled: true }];
            } else if (tpl === 'force_charge') {
                this.builder.name = 'Force Charge / Negative Price';
                this.builder.priority = 6;
                this.builder.condition_logic = 'ANY';
                this.builder.conditions = [
                    { metric: 'pricing.import_price_c_kwh', operator: '<',  value: '0',              enabled: true },
                    { metric: 'pricing.import_price_c_kwh', operator: '<=', value: String(maxChgPrc), enabled: true },
                ];
                this.builder.actions = [{ type: 'smart_dispatch.force_charge', payload: { target_soc: String(maxSoc) }, enabled: true }];
            }
        },

        describeCron(cron) {
            if (!cron) return '';
            const parts = cron.trim().split(/\s+/);
            if (parts.length !== 5) return 'Invalid cron';

            const [min, hour, dom, month, dow] = parts;
            const pad = v => String(v).padStart(2, '0');

            // Case 1: Every N minutes (e.g. */5 * * * *)
            if (min.startsWith('*/') && hour === '*' && dom === '*' && month === '*' && dow === '*') {
                const step = min.substring(2);
                return `Every ${step} minutes`;
            }

            // Case 2: Every hour (0 * * * *)
            if (min === '0' && hour === '*' && dom === '*' && month === '*' && dow === '*') {
                return 'Every hour';
            }

            // Case 3: Specific time every day (e.g. 0 8 * * *)
            if (!isNaN(min) && !isNaN(hour) && dom === '*' && month === '*' && dow === '*') {
                return `Every day at ${pad(hour)}:${pad(min)}`;
            }

            // Case 4: Specific time on weekdays (e.g. 0 8 * * 1-5)
            if (!isNaN(min) && !isNaN(hour) && dom === '*' && month === '*') {
                let dowStr = '';
                if (dow === '1-5') {
                    dowStr = 'weekdays';
                } else if (dow === '0,6' || dow === '6,0' || dow === '6-7' || dow === '0-1') {
                    dowStr = 'weekends';
                } else if (dow !== '*') {
                    dowStr = `days (${dow})`;
                }
                if (dowStr) {
                    return `Every day at ${pad(hour)}:${pad(min)} on ${dowStr}`;
                }
            }

            // Fallback: build a structured description
            let desc = '';
            
            // Minutes
            if (min === '*') {
                desc += 'Every minute';
            } else if (min.startsWith('*/')) {
                desc += `Every ${min.substring(2)} minutes`;
            } else {
                desc += `At minute ${min}`;
            }

            // Hours
            if (hour === '*') {
                // implicit
            } else if (hour.startsWith('*/')) {
                desc += ` of every ${hour.substring(2)} hours`;
            } else {
                if (!min.startsWith('*/') && min !== '*') {
                    desc = `At ${pad(hour)}:${pad(min)}`;
                } else {
                    desc += ` of hour ${hour}`;
                }
            }

            // Days/Months/DOW
            let location = '';
            if (dom !== '*') location += ` on day of month ${dom}`;
            if (month !== '*') location += ` in month ${month}`;
            if (dow !== '*') {
                let dayNames = dow;
                if (dow === '1-5') dayNames = 'Mon-Fri';
                else if (dow === '0,6' || dow === '6,0') dayNames = 'Sat-Sun';
                location += ` on ${dayNames}`;
            }

            if (location) {
                desc += location;
            } else if (hour === '*' && min === '*') {
                // default
            } else if (!desc.startsWith('Every') && !desc.startsWith('At')) {
                desc = 'At ' + desc;
            }

            return desc;
        },

        validateCron(cron) {
            if (!cron) return { valid: false, message: 'Expression is empty' };
            const parts = cron.trim().split(/\s+/);
            if (parts.length !== 5) {
                return { valid: false, message: 'Cron expression must have exactly 5 fields.' };
            }
            const cronRegex = /^[0-9\*,/\-\?]+$/;
            for (let i = 0; i < 5; i++) {
                if (!cronRegex.test(parts[i])) {
                    return { valid: false, message: `Invalid character in field ${i+1}.` };
                }
            }
            return { valid: true, message: this.describeCron(cron) };
        },

        updateCronValidation() {
            const res = this.validateCron(this.builder.cron);
            this.cronValidation = {
                valid: res.valid,
                message: res.valid ? `Valid: ${res.message}` : `Error: ${res.message}`
            };
        },

        initSelectionFromCron() {
            const presets = [
                '*/5 * * * *', '*/10 * * * *', '*/15 * * * *', '*/30 * * * *',
                '* * * * *', '0 * * * *', '0 0 * * *', '0 8 * * *',
                '0 0 * * 0', '0 0 1 * *', '0 0 1 */3 *', '0 0 1 */6 *', '0 0 1 1 *'
            ];
            if (presets.includes(this.builder.cron)) {
                this.cronSelection = this.builder.cron;
            } else {
                this.cronSelection = 'custom';
            }
            this.updateCronValidation();
        },

        async initTab() {
            this.checkUnlockStatus();
            this.$watch('$store.app.automationsPinEnabled', (enabled) => {
                if (enabled === false) {
                    this.showUnlockModal = false;
                } else {
                    this.checkUnlockStatus();
                }
            });
            this.$watch('cronSelection', val => {
                if (val && val !== 'custom') {
                    this.builder.cron = val;
                    this.updateCronValidation();
                }
            });
            this.$watch('builder.cron', val => {
                if (this.cronSelection === 'custom') {
                    this.updateCronValidation();
                }
            });
            await this.fetchJobs();
            await this.loadDispatchCfg();
            // Watch for gateway changes so template defaults stay in sync
            this.$watch('builder.gateway_serial', () => this.loadDispatchCfg());
            // Batch R.1 (2026-08-02): timer ids captured for destroy()
            // clearance under R.2 x-if lazy mount. Both were previously
            // uncapturable and leaked forever per external mobile-crash
            // report 2026-07-11.
            this._jobsTimer = setInterval(() => { if (this.view === 'queue' || this.view === 'rules') this.fetchJobs(); }, 30000);
            this._bmsTimer  = setInterval(() => this.pollBmsNotifications(), 30000);
        },

        async loadDispatchCfg() {
            try {
                // Use the selected gateway from the builder, or fall back to the global selected gateway.
                // When 'ALL' is selected, fetch the 'global' SD config row so lookup values are still populated.
                let gwId = (this.builder.gateway_serial && this.builder.gateway_serial !== 'ALL')
                    ? this.builder.gateway_serial.split(',')[0].trim()
                    : '';
                if (!gwId) {
                    // Fall back to global SD config row for wildcard / unset gateway
                    gwId = window.Alpine?.store('app')?.selectedGateway || 'global';
                }
                const r = await fetch(`api/smart_dispatch/config?gateway_id=${gwId}`);
                if (!r.ok) return;
                const d = await r.json();
                if (d?.ok && d.config) {
                    this.dispatchCfg = {
                        min_soc:               d.config.min_soc               ?? 20,
                        max_soc:               d.config.max_soc               ?? 90,
                        max_charge_price:      d.config.max_charge_price      ?? 15,
                        min_export_price:      d.config.min_export_price      ?? 0,
                        export_bonus_threshold: d.config.export_bonus_threshold ?? 5,
                        daily_earnings_target: d.config.daily_earnings_target ?? 5,
                        monthly_earnings_target: d.config.monthly_earnings_target ?? 0,
                    };
                }
            } catch (e) {
                console.warn('automationsTab: loadDispatchCfg failed', e);
            }
        },


        checkUnlockStatus() {
            if (window.Alpine?.store('app')?.automationsPinEnabled === false) {
                this.showUnlockModal = false;
                return;
            }
            const expiry = localStorage.getItem('fwh_automations_unlock_expiry');
            if (expiry && parseInt(expiry) > Date.now()) { this.showUnlockModal = false; }
            else { this.unlockChallenge = Math.floor(1000 + Math.random() * 9000).toString(); this.userUnlockPin = ''; this.showUnlockModal = true; }
        },

        confirmUnlock() {
            if (this.userUnlockPin === this.unlockChallenge) {
                localStorage.setItem('fwh_automations_unlock_expiry', String(Date.now() + 24 * 3600 * 1000));
                this.showUnlockModal = false;
            }
        },

        cancelUnlock() { window.location.search = '?tab=dashboard'; },

        async fetchJobs() {
            this.loading = true;
            try { const r = await fetch('api/scheduler/jobs'); if (r.ok) this.jobs = await r.json(); }
            catch (e) { console.error('fetchJobs error', e); }
            finally { this.loading = false; }
        },

        async fetchHistory() {
            this.historyLoading = true;
            try {
                const r = await fetch('api/scheduler/history?limit=' + parseInt(this.retentionDays) * 50);
                if (r.ok) { const d = await r.json(); this.history = Array.isArray(d) ? d : (d.history || []); }
            } catch (e) { console.error('fetchHistory error', e); }
            this.historyLoading = false;
        },

        async fetchAudit() {
            this.auditLoading = true;
            try {
                const r = await fetch('api/scheduler/audit');
                if (r.ok) { const d = await r.json(); this.audit = d.logs || []; }
            } catch (e) { console.error('fetchAudit error', e); }
            this.auditLoading = false;
        },

        async toggleJob(job_id, enabled) {
            try { await fetch(`api/scheduler/jobs/${job_id}/toggle?enabled=${enabled}`, { method: 'POST' }); await this.fetchJobs(); }
            catch (e) { console.error('toggleJob error', e); }
        },

        async deleteJob(job_id) {
            const job = this.jobs.find(j => j.id === job_id);
            if (job) this.jobToDelete = job;
        },

        async confirmDeleteJob() {
            if (!this.jobToDelete) return;
            try {
                await fetch(`api/scheduler/jobs/${this.jobToDelete.id}`, { method: 'DELETE' });
                this.$store.app.addToast(`Deleted: ${this.jobToDelete.name}`, 'info');
                await this.fetchJobs();
            } catch (e) {
                this.$store.app.addToast('Failed to delete automation', 'error');
            }
            this.jobToDelete = null;
        },

        async executeJobNow(job) {
            this.executingJob = job; 
            this.executeResult = { status: 'loading', data: [] }; 
            this.showExecuteModal = true;
            this.executeSeconds = 0;
            
            if (this.executeInterval) clearInterval(this.executeInterval);
            if (this.executePollInterval) clearInterval(this.executePollInterval);
            
            this.executeInterval = setInterval(() => { this.executeSeconds++; }, 1000);

            try {
                const r = await fetch(`api/scheduler/jobs/${job.id}/execute`, { method: 'POST' });
                const res = await r.json();
                
                if (r.ok) {
                    this.$store.app.addToast('Execution triggered', 'success');
                    const traceId = res.trace_id;
                    
                    // Poll for results using traceId
                    this.executePollInterval = setInterval(async () => {
                        try {
                            const hr = await fetch(`api/scheduler/history?request_id=${traceId}&limit=50`);
                            if (hr.ok) {
                                const historyData = await hr.json();
                                const items = Array.isArray(historyData) ? historyData : (historyData.history || []);
                                this.executeResult.data = items;
                                
                                // As soon as we have any data, flip to 'success' status so the UI renders the timeline template
                                if (items.length > 0 && this.executeResult.status === 'loading') {
                                    this.executeResult.status = 'success';
                                }
                                
                                // Check if all actions are complete (no 'running' states left)
                                const isStillRunning = items.some(h => h.status === 'running');
                                // If we have at least one result and nothing is 'running', we can stop polling
                                if (items.length > 0 && !isStillRunning) {
                                     clearInterval(this.executeInterval);
                                     clearInterval(this.executePollInterval);
                                }
                            }
                        } catch (e) { console.warn('Trace poll failed', e); }
                    }, 2000);

                } else {
                    this.executeResult = { status: 'error', detail: res.detail };
                    this.$store.app.addToast(`Execution failed: ${res.detail}`, 'error');
                    clearInterval(this.executeInterval);
                }
            } catch (e) {
                this.executeResult = { status: 'error', detail: String(e) };
                clearInterval(this.executeInterval);
            }
        },

        async verifyLogic() {
            if (!this.builder.gateway_serial) { this.logicTestResult = { error: 'Configure a target gateway first.' }; return; }
            this.verifyingLogic = true; this.logicTestResult = null;
            try {
                const r = await fetch('api/scheduler/evaluate', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ gateway_serial: this.builder.gateway_serial, condition_logic: this.builder.condition_logic, conditions: this.builder.conditions }),
                });
                this.logicTestResult = await r.json();
            } catch (e) { this.logicTestResult = { error: String(e) }; }
            this.verifyingLogic = false;
        },

        async submitRule() {
            this.saving = true;
            try {
                const tagsArray = this.builder.tags ? this.builder.tags.split(',').map(s => s.trim()).filter(Boolean) : [];
                const finalConditions = (this.builder.trigger_type === 'condition' || this.builder.has_conditions) ? this.builder.conditions : [];
                const finalCron = this.builder.trigger_type === 'schedule' ? this.builder.cron : '*/1 * * * *';
                let finalRunAt = null;
                if (this.builder.trigger_type === 'one-off' && this.builder.run_at)
                    finalRunAt = new Date(this.builder.run_at).toISOString().replace('.000Z', 'Z');
                const payload = {
                    name: this.builder.name, gateway_serial: this.builder.gateway_serial, owner: 'user',
                    tags: tagsArray, cron: finalCron, run_at: finalRunAt,
                    condition_logic: this.builder.condition_logic,
                    duration_secs: this.parseDuration(this.builder.duration_str),
                    priority: parseInt(this.builder.priority) || 25,
                    enabled: this.builder.enabled !== false,
                    conditions: finalConditions, actions: this.builder.actions,
                };
                const url = this.editingJobId ? `api/scheduler/jobs/${this.editingJobId}` : 'api/scheduler/jobs';
                const r = await fetch(url, { method: this.editingJobId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                const res = await r.json();
                if (r.ok && res?.status === 'ok') {
                    this.showBuilder = false; this.editingJobId = null;
                    this.builder = { name: '', gateway_serial: 'ALL', condition_logic: 'AND', duration_str: '00:00:00', trigger_type: 'condition', priority: 25, has_conditions: false, tags: '', cron: '0 0 * * *', run_at: '', conditions: [{ metric: 'grid.status', operator: '==', value: '' }], actions: [{ type: 'set_smart_circuit', payload: { id: 1, state: 'on' } }] };
                    await this.fetchJobs();
                } else {
                    alert(`Submission failed: ${res?.detail || 'Unknown error'}`);
                }
            } catch (e) {
                alert('Failed to deploy rule: ' + (e.message || String(e)));
            } finally { this.saving = false; }
        },

        /* ── BMS notification polling ────────────────────────── */
        // Batch R.1 (2026-08-02): cap the seen-set to prevent unbounded
        // growth over a long session (external mobile-crash report
        // 2026-07-11 flagged this as minor leak — pollBmsNotifications
        // fires every 30s and adds every unique id, never removes).
        _bmsNotifSeen: new Set(),
        _bmsNotifSeenMax: 200,
        async pollBmsNotifications() {
            try {
                const r = await fetch('api/system/notifications/pop?type=record_bms');
                if (!r.ok) return;
                const items = await r.json();
                for (const n of items) {
                    if (this._bmsNotifSeen.has(n.id)) continue;
                    this._bmsNotifSeen.add(n.id);
                    this.$store.app.addToast(`🔋 BMS Recording complete — session "${n.session_name}" saved (${n.gateway_id})`, 'info', { duration: 8000 });
                }
                // Trim: keep only the most-recent N ids. Set preserves
                // insertion order, so slicing tail gives newest entries.
                if (this._bmsNotifSeen.size > this._bmsNotifSeenMax) {
                    const arr = Array.from(this._bmsNotifSeen);
                    this._bmsNotifSeen = new Set(arr.slice(-this._bmsNotifSeenMax));
                }
            } catch {} // silent — background poll
        },

        cloneHistoricalJob(log) {
            this.builder = {
                name: 'Resubmit ' + (log.rule_name || log.rule_id || 'Historical Rule'),
                gateway_serial: log.gateway_serial || 'ALL',
                trigger_type: log.action_type === 'condition_check' ? 'condition' : 'one-off',
                priority: log.priority || 25,
                cron: '*/1 * * * *', run_at: '', has_conditions: log.action_type === 'condition_check',
                condition_logic: 'AND', duration_str: '00:00:00',
                conditions: [], actions: [],
            };
            if (log.action_type === 'condition_check') {
                try { this.builder.conditions = JSON.parse(log.action_payload); } catch {}
            } else {
                try { this.builder.actions.push({ type: log.action_type, payload: JSON.parse(log.action_payload) }); } catch {}
            }
            this.editingJobId = null; this.showBuilder = true;
            setTimeout(() => this.$refs.ruleNameInput?.focus(), 100);
        },

        saveSettings() { localStorage.setItem('fwh_scheduler_retention', this.retentionDays); },

        getTagColor(tag) {
            const colors = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4'];
            let hash = 0;
            for (let i = 0; i < tag.length; i++) hash = tag.charCodeAt(i) + ((hash << 5) - hash);
            return colors[Math.abs(hash) % colors.length];
        },

        getAuditColor(action) {
            if (action === 'create') return 'background:rgba(16,185,129,0.1);color:var(--ok)';
            if (action === 'delete') return 'background:rgba(239,68,68,0.1);color:var(--danger)';
            if (action === 'update') return 'background:rgba(59,130,246,0.1);color:var(--primary)';
            return 'background:rgba(255,255,255,0.1);color:var(--text-secondary)';
        },

        formatDate(isoString) {
            if (!isoString) return '';
            const d = new Date(isoString + (isoString.includes('Z') || isoString.includes('+') ? '' : 'Z'));
            if (isNaN(d.valueOf())) return isoString;
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' (' + d.toLocaleDateString() + ')';
        },

        formatDuration(secs) {
            if (!secs) return '00:00:00';
            const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
            const pad = v => String(v).padStart(2, '0');
            return `${pad(h)}:${pad(m)}:${pad(s)}`;
        },

        parseDuration(str) {
            if (!str) return 0;
            const parts = String(str).split(':');
            if (parts.length === 1) return parseInt(parts[0]) || 0;
            if (parts.length === 2) return (parseInt(parts[0]) || 0) * 60 + (parseInt(parts[1]) || 0);
            return (parseInt(parts[0]) || 0) * 3600 + (parseInt(parts[1]) || 0) * 60 + (parseInt(parts[2]) || 0);
        },

        autoFormatDuration(str) {
            if (!str?.trim()) return '00:00:00';
            return this.formatDuration(this.parseDuration(str));
        },
    };
}

// ── Smart Dispatch Panel (B3) ─────────────────────────────────────────────────
// Separate Alpine component: x-data="smartDispatchPanel()"
// Manages: live signal polling, reserved preset readiness, rule toggles, history.

function smartDispatchPanel() {
    return {
        signal: null, signalLoading: true, signalError: null,
        signalLastFetched: null, _pollTimer: null,

        presets: [], presetsLoading: true, presetsError: null,
        canExecute: false, baselineWarning: null,

        rules: [], rulesLoading: true, rulesError: null,

        history: [], historyLoading: true,

        async init() {
            await Promise.all([
                this.fetchSignal(),
                this.fetchPresets(),
                this.fetchRules(),
                this.fetchHistory(),
            ]);
            this._pollTimer = setInterval(() => this.fetchSignal(), 30_000);
        },

        destroy() {
            // Batch R.1 (2026-08-02): clear all 3 concurrent timers on unmount
            // (was only clearing _pollTimer; the fetchJobs + bmsNotif timers
            // added in init() at lines ~492-493 leaked prior to this).
            if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
            if (this._jobsTimer) { clearInterval(this._jobsTimer); this._jobsTimer = null; }
            if (this._bmsTimer)  { clearInterval(this._bmsTimer);  this._bmsTimer  = null; }
            // Also clear the executeInterval / executePollInterval created
            // during the manual-execute flow if they're still running
            // (belt-and-braces — usually cleared inside their own lifecycle).
            if (this.executeInterval)     { clearInterval(this.executeInterval);     this.executeInterval = null; }
            if (this.executePollInterval) { clearInterval(this.executePollInterval); this.executePollInterval = null; }
        },

        async refreshAll() {
            await Promise.all([
                this.fetchSignal(),
                this.fetchPresets(),
                this.fetchRules(),
                this.fetchHistory(),
            ]);
        },

        async fetchSignal() {
            this.signalLoading = true;
            try {
                const r = await fetch('api/automation/signal');
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                this.signal = await r.json();
                this.signalError = null;
                this.signalLastFetched = new Date();
            } catch(e) { this.signalError = String(e); }
            finally { this.signalLoading = false; }
        },

        async fetchPresets() {
            try {
                const r = await fetch('api/automation/presets/reserved');
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                const d = await r.json();
                this.presets = d.reserved_presets || [];
                this.canExecute = d.can_execute;
                this.baselineWarning = d.warning;
            } catch(e) { this.presetsError = String(e); }
            finally { this.presetsLoading = false; }
        },

        async fetchRules() {
            try {
                const r = await fetch('api/automation/rules');
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                const d = await r.json();
                this.rules = d.rules || [];
            } catch(e) { this.rulesError = String(e); }
            finally { this.rulesLoading = false; }
        },

        async fetchHistory() {
            try {
                const r = await fetch('api/automation/history?limit=25');
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                const d = await r.json();
                this.history = d.history || [];
            } catch(e) { /* non-fatal */ }
            finally { this.historyLoading = false; }
        },

        async toggleRule(rule) {
            const enabled = !rule.enabled;
            try {
                const r = await fetch(`api/automation/rules/${rule.rule_id}/toggle`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled }),
                });
                if (r.ok) rule.enabled = enabled ? 1 : 0;
            } catch(e) {
                window.$store?.app?.addToast?.(`Toggle failed: ${e.message}`, 'error');
            }
        },

        // ── Signal display ───────────────────────────────────────────────────
        get signalActionLabel() {
            if (!this.signal) return '—';
            if (this.signal.action === 'APPLY_PRESET' && this.signal.preset_name)
                return this.signal.preset_name;
            return this.signal.action || '—';
        },

        signalActionColor(s) {
            if (!s) return 'text-gray-400';
            const p = s.preset_name || '';
            if (p.includes('Spike Hold'))    return 'text-red-400';
            if (p.includes('Force Charge'))  return 'text-purple-400';
            if (p.includes('Peak Discharge'))return 'text-orange-400';
            if (p.includes('Solar Sponge')) return 'text-yellow-400';
            if (p.includes('Default'))      return 'text-teal-400';
            return 'text-blue-400';
        },

        signalActionBg(s) {
            if (!s) return 'bg-gray-800/40 border-gray-700';
            const p = s.preset_name || '';
            if (p.includes('Spike Hold'))    return 'bg-red-950/30 border-red-900/60';
            if (p.includes('Force Charge'))  return 'bg-purple-950/30 border-purple-900/60';
            if (p.includes('Peak Discharge'))return 'bg-orange-950/30 border-orange-900/60';
            if (p.includes('Solar Sponge')) return 'bg-yellow-950/30 border-yellow-900/60';
            if (p.includes('Default'))      return 'bg-teal-950/30 border-teal-900/60';
            return 'bg-blue-950/30 border-blue-900/60';
        },

        signalActionIcon(s) {
            if (!s) return 'fa-question-circle';
            const p = s.preset_name || '';
            if (p.includes('Spike Hold'))    return 'fa-shield-halved';
            if (p.includes('Force Charge'))  return 'fa-bolt';
            if (p.includes('Peak Discharge'))return 'fa-arrow-up-from-bracket';
            if (p.includes('Solar Sponge')) return 'fa-sun';
            if (p.includes('Default'))      return 'fa-rotate-left';
            return 'fa-robot';
        },

        // ── Rule helpers ─────────────────────────────────────────────────────
        rulePresetName(rule) {
            try { const p = JSON.parse(rule.action_params||'{}'); return p.preset_name || rule.action; }
            catch { return rule.action; }
        },

        conditionSummary(rule) {
            try {
                const c = JSON.parse(rule.condition_json || '{}');
                if (!c || !c.conditions || !c.conditions.length) return 'Always (catch-all)';
                return c.conditions.map(cd => {
                    const v = Array.isArray(cd.value) ? `[${cd.value.join(', ')}]` : String(cd.value);
                    return `${cd.field} ${cd.op} ${v}`;
                }).join(` ${c.operator || 'AND'} `);
            } catch { return '—'; }
        },

        priorityBadgeColor(p) {
            if (p <= 20)  return 'bg-red-900/60 text-red-300 border-red-800';
            if (p <= 40)  return 'bg-orange-900/60 text-orange-300 border-orange-800';
            if (p <= 60)  return 'bg-yellow-900/60 text-yellow-300 border-yellow-800';
            if (p >= 999) return 'bg-gray-800 text-gray-400 border-gray-700';
            return 'bg-blue-900/60 text-blue-300 border-blue-800';
        },

        // ── Preset helpers ───────────────────────────────────────────────────
        presetRoleIcon(role) {
            return { baseline:'fa-anchor', force_charge:'fa-bolt',
                     peak_discharge:'fa-arrow-up-from-bracket',
                     solar_sponge:'fa-sun', spike_hold:'fa-shield-halved' }[role] || 'fa-file';
        },

        presetRoleColor(role) {
            return { baseline:'text-teal-400', force_charge:'text-purple-400',
                     peak_discharge:'text-orange-400', solar_sponge:'text-yellow-400',
                     spike_hold:'text-red-400' }[role] || 'text-gray-400';
        },

        // ── History helpers ──────────────────────────────────────────────────
        historyTimeAgo(ts) {
            if (!ts) return '—';
            const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') ? '' : 'Z'));
            const s = Math.floor((Date.now() - d.getTime()) / 1000);
            if (s < 60) return `${s}s ago`;
            if (s < 3600) return `${Math.floor(s/60)}m ago`;
            if (s < 86400) return `${Math.floor(s/3600)}h ago`;
            return d.toLocaleDateString();
        },

        historyActionColor(action) {
            if (action === 'APPLY_PRESET') return 'text-blue-400';
            if (action === 'RESUME_TOU')   return 'text-teal-400';
            return 'text-gray-400';
        },

        // ── Misc ─────────────────────────────────────────────────────────────
        formatPrice(v) {
            if (v === null || v === undefined) return '—';
            const n = parseFloat(v);
            return n < 0 ? `+${Math.abs(n).toFixed(2)}¢ earn` : `${n.toFixed(2)}¢`;
        },

        lastFetchedStr() {
            if (!this.signalLastFetched) return '';
            const s = Math.floor((Date.now() - this.signalLastFetched.getTime()) / 1000);
            return s < 5 ? 'just now' : `${s}s ago`;
        },

        // ── Target battery and macro helpers ─────────────────────────────────
        targetGateways() {
            const serial = this.builder.gateway_serial;
            if (!serial) return [];
            const list = serial.split(',').map(s => s.trim()).filter(Boolean);
            if (list.includes('ALL')) return this.gateways;
            return this.gateways.filter(g => list.includes(g.full_serial));
        },

        getGatewayBatteries(full_serial) {
            const gw = this.gateways.find(g => g.full_serial === full_serial);
            if (!gw || !gw.apower_serial_numbers) return [];
            const serials = Array.isArray(gw.apower_serial_numbers) 
                ? gw.apower_serial_numbers 
                : (typeof gw.apower_serial_numbers === 'string' ? gw.apower_serial_numbers.split(',').map(x => x.trim()).filter(Boolean) : []);
            return serials.map((sn, idx) => ({
                short_id: sn.slice(-8),
                full_serial: sn,
                name: `aPower Battery ${idx + 1}`
            }));
        },
    };
}


// ── Home Loads catalog for the AB metric picker ─────────────────────────────
// Registered on window so the per-condition inline x-data picker can fetch
// without having to climb through nested Alpine scopes. Cached per scope.
// See docs/automation_builder_home_loads_exposure_plan.md §4-§5.
window.__ABHomeLoads = (function () {
    const cache = {};

    async function load(scope) {
        const key = scope || 'all';
        if (cache[key] !== undefined) return cache[key];
        const promise = (async () => {
            try {
                const res = await fetch(`api/automation/home-load-fields?scope=${encodeURIComponent(key)}`);
                const data = await res.json();
                return (data && data.ok) ? data : null;
            } catch (e) {
                console.error('home-load-fields fetch failed', e);
                return null;
            }
        })();
        cache[key] = promise;
        const result = await promise;
        cache[key] = result;
        return result;
    }

    function optgroupsForPicker(catalog) {
        if (!catalog) return [];
        const groups = [];
        if (catalog.site_aggregate_fields && catalog.site_aggregate_fields.length) {
            groups.push({
                label: 'Home Loads — Site Aggregates',
                items: catalog.site_aggregate_fields.map(f => ({ val: f.path, txt: f.label })),
            });
        }
        for (const gw of (catalog.gateways || [])) {
            for (const ld of (gw.loads || [])) {
                groups.push({
                    label: `Home Load · ${gw.label} · ${ld.name}`,
                    items: ld.fields.map(f => ({ val: f.path, txt: `${ld.name} · ${f.label}` })),
                });
            }
            if (gw.aggregate_fields && gw.aggregate_fields.length) {
                groups.push({
                    label: `Home Loads — ${gw.label} aggregates`,
                    items: gw.aggregate_fields.map(f => ({ val: f.path, txt: `${gw.label} · ${f.label}` })),
                });
            }
        }
        return groups;
    }

    function scopeFromRuleGateway(gw) {
        if (!gw || gw === 'ALL' || gw === 'all' || gw === '') return 'all';
        return String(gw).split(',')[0];
    }

    return { load, optgroupsForPicker, scopeFromRuleGateway };
})();
