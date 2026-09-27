/* Battery Tab Alpine Component — v2
   Verbatim logic extracted from tabs/battery.html <script> block. */
function batteryTab() {
  return {
    gateways: [],
    selectedGw: '',
    selectedBatterySn: '',
    activeBmsSn: '',
    batteries: [],
    bmsUnits: [],
    recordingBms: false,
    bmsInterval: null,
    autoRefreshCount: 3,
    autoRefreshInterval: 30,
    autoRefreshRemaining: 0,
    loading: false,
    fetchingBms: false,
    bmsError: '',
    // ── Modal Chart (Sprint B: source-aware refresh) ──────────
    chartModalOpen: false,
    activeChartSerial: null,
    chartInstance: null,
    chartLoading: false,
    chartEmpty: false,
    chartTab: 'VOLTAGE',
    chartDataTimes: [],
    chartDataVoltages: [],
    chartDataTemps: [],
    activeChartRealSerial: '',
    chartSource: 'live',         // 'live' | session_id string
    chartSourceName: '',         // display label for reload
    // ── Sessions panel ────────────────────────────────────────
    sessionsModalOpen: false,
    savedSessions: [],
    loadingSessions: false,
    // ── aPower info ───────────────────────────────────────────
    mqttPrefix: 'franklinwh',
    apowerData: [],
    apowerProfileData: [],
    apowerModalOpen: false,
    activeApowerRecord: null,
    balancingModalOpen: false,
    activeBalancingUnit: null,
    gwShortId: '',
    // ── Sub-tab navigation (Sprint C) ─────────────────────────
    activeSubTab: 'telemetry',   // 'telemetry' | 'charts'
    // ── Inline Charts sub-tab (Sprint C) ─────────────────────
    allSessions: [],
    loadingAllSessions: false,
    filterBatterySn: '',
    inlineChartInstance: null,
    inlineChartLoading: false,
    inlineChartEmpty: true,
    inlineChartTab: 'VOLTAGE',
    inlineChartTimes: [],
    inlineChartVoltages: [],
    inlineChartTemps: [],
    inlineChartTitle: '',
    inlineChartSerial: '',
    selectedInlineSessionId: null,

    async init() {
      const gws = await fetchJSON('api/gateways');
      if (gws) this.gateways = gws;
      const mqttStatus = await fetchJSON('api/mqtt/status');
      if (mqttStatus?.topic_prefix) this.mqttPrefix = mqttStatus.topic_prefix;
      if (this.gateways.length > 0 && !this.selectedGw) {
        this.selectedGw = this.gateways[0].short_id;
        this.load();
      }
    },

    async load() {
      if (!this.selectedGw) return;
      this.loading = true;
      this.gwShortId = this.selectedGw;
      const gwDetail = await fetchJSON('api/gateways/' + this.selectedGw + '/data');
      if (gwDetail && Array.isArray(gwDetail.apower_serial_numbers)) {
        this.batteries = gwDetail.apower_serial_numbers.map(sn => ({ short_id: sn, full_serial: sn }));
      } else {
        this.batteries = [];
      }
      // apower_units_profile: static firmware data from profile_json — always present, no BMS trigger needed
      if (gwDetail?.apower_units_profile && Array.isArray(gwDetail.apower_units_profile)) {
        this.apowerProfileData = gwDetail.apower_units_profile;
      }
      // apower_info from live cache (populated after startup counter==1 poll)
      if (gwDetail?.apower_info?.result && Array.isArray(gwDetail.apower_info.result)) {
        this.apowerData = gwDetail.apower_info.result;
      }
      if (this.batteries.length > 0 && !this.selectedBatterySn) {
        this.selectedBatterySn = this.batteries[0].full_serial || this.batteries[0].short_id;
      }
      this.loading = false;
    },

    async loadBms() {
      if (!this.selectedGw || !this.selectedBatterySn) {
        this.bmsError = 'No gateway or battery selected.';
        setTimeout(() => this.bmsError = '', 4000);
        return;
      }
      this.fetchingBms = true;
      this.bmsError = '';
      try {
        await this.triggerBmsFetch();
        const gwDetail = await fetchJSON('api/gateways/' + this.selectedGw + '/data');
        if (gwDetail?.bms_units && Array.isArray(gwDetail.bms_units)) {
          this.bmsUnits = gwDetail.bms_units.filter(u => u && u.cell_voltages);
        } else {
          this.bmsUnits = [];
        }
        if (gwDetail?.apower_info?.result && Array.isArray(gwDetail.apower_info.result)) {
          this.apowerData = gwDetail.apower_info.result;
        } else {
          this.apowerData = [];
        }
        if (this.bmsUnits.length === 0) {
          this.bmsError = 'No BMS Data found. Ensure gateway is online.';
          setTimeout(() => this.bmsError = '', 4000);
        }
        this.activeBmsSn = this.selectedBatterySn;
      } catch (err) {
        this.bmsError = 'Failed to fetch BMS telemetry: ' + err.message;
        setTimeout(() => this.bmsError = '', 5000);
      } finally {
        this.fetchingBms = false;
      }
    },

    async toggleBmsRecording() {
      if (this.recordingBms) {
        this.recordingBms = false;
        if (this.bmsInterval) clearInterval(this.bmsInterval);
        this.bmsInterval = null;
        const shouldSave = confirm(`Recording stopped at ${this.autoRefreshCount - this.autoRefreshRemaining} samples! Save partial session?`);
        if (shouldSave && this.recordingBuffer?.times.length > 0) {
          const ts = new Date().toISOString().replace('T', ' ').substring(0, 19);
          await fetchJSON(`api/gateways/${this.selectedGw}/bms/sessions`, {
            method: 'POST',
            body: JSON.stringify({ battery_sn: this.activeBmsSn, session_name: `Manual Record ${ts}`, data: this.recordingBuffer }),
          });
          this.openSessionsModal();
        }
        this.autoRefreshRemaining = 0;
        this.recordingBuffer = null;
      } else {
        if (!this.selectedGw) return;
        this.recordingBms = true;
        this.autoRefreshRemaining = this.autoRefreshCount;
        this.recordingBuffer = { times: [], voltages: [], temps: [] };
        await this.triggerBmsFetch();
        this.captureBmsBuffer();
        this.autoRefreshRemaining--;
        const tick = async () => {
          if (!this.recordingBms) return;
          await this.triggerBmsFetch();
          this.captureBmsBuffer();
          this.autoRefreshRemaining--;
          if (this.autoRefreshRemaining <= 0) {
            this.recordingBms = false;
            if (this.bmsInterval) clearInterval(this.bmsInterval);
            this.bmsInterval = null;
            const ts = new Date().toISOString().replace('T', ' ').substring(0, 19);
            await fetchJSON(`api/gateways/${this.selectedGw}/bms/sessions`, {
              method: 'POST',
              body: JSON.stringify({ battery_sn: this.activeBmsSn, session_name: `Recorded ${this.autoRefreshCount} Samples (${this.autoRefreshInterval}s interval)`, data: this.recordingBuffer }),
            });
            this.autoRefreshRemaining = 0;
            this.recordingBuffer = null;
            this.openSessionsModal();
          }
        };
        this.bmsInterval = setInterval(tick, this.autoRefreshInterval * 1000);
      }
    },

    captureBmsBuffer() {
      if (!this.bmsUnits || !this.recordingBuffer) return;
      const u = this.bmsUnits.find(u => u.serial === this.activeBmsSn);
      if (u) {
        const now = new Date();
        this.recordingBuffer.times.push(now.toTimeString().split(' ')[0]);
        this.recordingBuffer.voltages.push([...u.cell_voltages]);
        // cell_temps is the correct field name (cell_temperatures was a typo causing empty sessions)
        this.recordingBuffer.temps.push(u.cell_temps ? [...u.cell_temps] : []);
      }
    },

    async triggerBmsFetch() {
      // Sprint A: use normalised bms_units from the trigger response body directly.
      // Previously this called load() after posting, but load() reads from last_data
      // which may not yet reflect the fresh BMS poll (background race). The trigger
      // endpoint now normalises the raw dict and writes it into last_data before
      // returning, so we can populate bmsUnits with zero round-trips.
      const resp = await fetchJSON('api/gateways/' + this.selectedGw + '/bms/trigger', { method: 'POST' });
      if (resp?.bms_units && Array.isArray(resp.bms_units)) {
        this.bmsUnits = resp.bms_units.filter(u => u && u.cell_voltages);
      } else {
        // Fallback for older server builds
        await this.load();
      }
    },

    async openChartModal(serial) {
      if (!this.selectedGw || !serial) return;
      this.activeChartSerial = serial;
      this.activeChartRealSerial = serial;
      this.chartModalOpen = true;
      this.chartLoading = true;
      this.chartEmpty = false;
      this.chartTab = 'VOLTAGE';
      // Sprint B: mark source as live history
      this.chartSource = 'live';
      this.chartSourceName = 'Live History';
      const hist = await fetchJSON(`api/gateways/${this.selectedGw}/bms/history`);
      this.chartLoading = false;
      if (!hist || !hist[serial] || !hist[serial].times || hist[serial].times.length === 0) {
        this.chartEmpty = true;
        return;
      }
      this.chartDataTimes = hist[serial].times;
      this.chartDataVoltages = hist[serial].voltages;
      this.chartDataTemps = hist[serial].temps;
      this.renderChart();
    },

    // Sprint B: source-aware refresh — re-loads correct data source (live vs saved session)
    async refreshChart() {
      if (this.chartSource === 'live') {
        await this.openChartModal(this.activeChartRealSerial);
      } else {
        await this.loadSavedSession(this.chartSource, this.chartSourceName);
      }
    },

    renderChart() {
      if (this.chartTab === 'COMPARISON') return;
      this.$nextTick(() => {
        const ctx = document.getElementById('bmsCanvas');
        if (!ctx) return;
        if (this.chartInstance) { this.chartInstance.destroy(); }
        if (typeof Chart === 'undefined') return;
        const datasets = [];
        const colors = ['#ef4444','#f97316','#f59e0b','#eab308','#84cc16','#22c55e','#10b981','#14b8a6','#06b6d4','#0ea5e9','#3b82f6','#6366f1','#8b5cf6','#a855f7','#d946ef','#ec4899'];
        let yMin = null, yMax = null, yTitle = '';
        if (this.chartTab === 'VOLTAGE') {
          for (let i = 0; i < 16; i++) {
            const vData = this.chartDataVoltages.map(snap => snap[i] ? snap[i] / 1000.0 : null);
            datasets.push({ label: 'Cell '+(i+1), data: vData, borderColor: colors[i], borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, tension: 0.1 });
          }
          yTitle = 'Voltage (V)';
          // Dynamic Y range: actual data min/max + at least 5mV (0.005V) padding each side.
          // Hard-coded 3.1-3.5 hid all variation when cells are balanced (e.g., all 3313-3314mV).
          const allV = this.chartDataVoltages.flat().filter(v => v != null && v > 0).map(v => v / 1000);
          if (allV.length) {
            const dMin = Math.min(...allV), dMax = Math.max(...allV);
            const pad = Math.max(0.005, (dMax - dMin) * 0.25);
            yMin = Math.floor((dMin - pad) * 1000) / 1000;
            yMax = Math.ceil((dMax  + pad) * 1000) / 1000;
          } else {
            yMin = 3.1; yMax = 3.5; // safe fallback
          }
        } else if (this.chartTab === 'TEMPERATURE') {
          for (let i = 0; i < 16; i++) {
            const tData = this.chartDataTemps.map(snap => snap[i] != null ? snap[i] : null);
            datasets.push({ label: 'Cell '+(i+1), data: tData, borderColor: colors[i], borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, tension: 0.1 });
          }
          yTitle = 'Temperature (\u00b0C)';
          // Dynamic Y range: at least 0.5°C pad each side.
          const allT = this.chartDataTemps.flat().filter(v => v != null);
          if (allT.length) {
            const dMin = Math.min(...allT), dMax = Math.max(...allT);
            const pad = Math.max(0.5, (dMax - dMin) * 0.25);
            yMin = Math.floor((dMin - pad) * 2) / 2;
            yMax = Math.ceil((dMax  + pad) * 2) / 2;
          }
        }
        this.chartInstance = new Chart(ctx, {
          type: 'line',
          data: { labels: this.chartDataTimes, datasets },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 10 }, color: '#94a3b8' } },
              tooltip: {
                backgroundColor: '#1e293b', titleColor: '#f1f5f9', bodyColor: '#94a3b8', borderColor: '#334155', borderWidth: 1,
                callbacks: {
                  label: (c) => {
                    const v = c.parsed.y;
                    const unit = this.chartTab === 'VOLTAGE' ? ' V' : ' \u00b0C';
                    return ` ${c.dataset.label}: ${v != null ? v.toFixed(this.chartTab === 'VOLTAGE' ? 3 : 1) : '\u2014'}${unit}`;
                  }
                }
              },
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b' } },
              y: {
                display: true,
                title: { display: true, text: yTitle, color: '#64748b' },
                grid: { color: 'rgba(255,255,255,0.05)' },
                min: yMin,
                max: yMax,
                ticks: { color: '#64748b', maxTicksLimit: 8 }
              },
            },
          },
        });
      });
    },

    exportChartCSV() {
      if (!this.chartDataTimes || !this.chartDataTimes.length) return;
      let csv = 'Time,';
      for (let i=1;i<=16;i++) csv += `Cell${i}_mV,`;
      for (let i=1;i<=16;i++) csv += `Cell${i}_C,`;
      csv += '\n';
      for (let row=0; row<this.chartDataTimes.length; row++) {
        csv += `${this.chartDataTimes[row]},`;
        for (let i=0;i<16;i++) csv += `${this.chartDataVoltages[row][i]||''},`;
        for (let i=0;i<16;i++) csv += `${this.chartDataTemps[row]&&this.chartDataTemps[row][i]!=null?this.chartDataTemps[row][i]:''},`;
        csv += '\n';
      }
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.hidden = true; a.href = url;
      a.download = `fwh_bms_${this.activeChartRealSerial.replace(/[^A-Za-z0-9]/g,'_')}_${Date.now()}.csv`;
      document.body.appendChild(a); a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    },

    closeChartModal() {
      this.chartModalOpen = false;
      if (this.chartInstance) { this.chartInstance.destroy(); this.chartInstance = null; }
    },

    openBalancingModal(unit) {
      this.activeBalancingUnit = unit;
      this.balancingModalOpen = true;
    },

    openApowerModal(serial) {
      // Priority 1: live apower_info record (enriched, from get_apower_info cache)
      const rec = this.apowerData.find(a => a.apowerSn === serial);
      if (rec) {
        this.activeApowerRecord = rec;
        this.apowerModalOpen = true;
        return;
      }
      // Priority 2: profile_json.apower_units (static, persisted at startup — always available)
      // profile stores snake_case keys; map to camelCase for modal template parity
      const prof = this.apowerProfileData.find(u => u.serial === serial || serial.endsWith(u.serial) || (u.serial && u.serial.endsWith(serial)));
      if (prof) {
        this.activeApowerRecord = {
          apowerSn:  serial,
          status:    0,
          bmsVer:    prof.bms_ver  || '—',
          dcdcVer:   prof.dcdc_ver || '—',
          invVer:    prof.inv_ver  || '—',
          fpgaVer:   prof.fpga_ver || '—',
          blVer:     prof.bl_ver   || '—',
          thVer:     prof.th_ver   || '—',
          ratedCapacity: prof.rated_kwh,
          ratedPower:    prof.rated_kw,
        };
        this.apowerModalOpen = true;
        return;
      }
      // Priority 3: fallback from BMS unit data in memory
      const unit = this.bmsUnits.find(u => u.serial === serial || serial.includes(u.serial) || u.serial.includes(serial));
      this.activeApowerRecord = {
        apowerSn:  serial,
        status:    unit?.status ?? 0,
        bmsVer:    unit?.firmware || '—',
        dcdcVer:   '—', invVer: '—', fpgaVer: '—', blVer: '—', thVer: '—',
      };
      this.apowerModalOpen = true;
    },
    closeApowerModal() { this.apowerModalOpen = false; this.activeApowerRecord = null; },

    async openSessionsModal() {
      if (!this.selectedGw || !this.activeBmsSn) return;
      this.sessionsModalOpen = true;
      this.loadingSessions = true;
      const data = await fetchJSON(`api/gateways/${this.selectedGw}/bms/sessions?battery_sn=${this.activeBmsSn}`);
      this.savedSessions = Array.isArray(data) ? data : [];
      this.loadingSessions = false;
    },
    closeSessionsModal() { this.sessionsModalOpen = false; },

    async deleteSession(sessionId) {
      if (!confirm('Permanently delete this chart session?')) return;
      await fetchJSON(`api/gateways/bms/sessions/${sessionId}`, { method: 'DELETE' });
      this.savedSessions = this.savedSessions.filter(s => s.session_id !== sessionId);
    },

    async loadSavedSession(sessionId, name) {
      this.closeSessionsModal();
      this.activeChartSerial = this.activeBmsSn + ' (' + name + ')';
      this.activeChartRealSerial = this.activeBmsSn;
      this.chartModalOpen = true;
      this.chartLoading = true;
      this.chartEmpty = false;
      this.chartTab = 'VOLTAGE';
      // Sprint B: track session source so Refresh reloads correctly
      this.chartSource = sessionId;
      this.chartSourceName = name;
      const data = await fetchJSON(`api/gateways/bms/sessions/${sessionId}`);
      this.chartLoading = false;
      const payload = data?.payload;
      if (!payload || !payload.times || !payload.times.length) { this.chartEmpty = true; return; }
      this.chartDataTimes = payload.times;
      this.chartDataVoltages = payload.voltages || [];
      this.chartDataTemps = payload.temps || [];
      this.renderChart();
    },

    // ── Sprint C: Inline Charts sub-tab ──────────────────────────────────────────
    async loadAllSessions() {
      if (!this.selectedGw) return;
      this.loadingAllSessions = true;
      this.allSessions = [];
      this.inlineChartEmpty = true;
      const data = await fetchJSON(`api/gateways/${this.selectedGw}/bms/sessions`);
      this.allSessions = Array.isArray(data) ? data : [];
      this.loadingAllSessions = false;
    },

    filteredSessions() {
      if (!this.filterBatterySn) return this.allSessions;
      return this.allSessions.filter(s => s.battery_sn === this.filterBatterySn || s.battery_sn.includes(this.filterBatterySn));
    },

    allSessionSerials() {
      const snSet = new Set(this.allSessions.map(s => s.battery_sn));
      return [...snSet];
    },

    async loadInlineSession(sessionId, name, serial) {
      this.selectedInlineSessionId = sessionId;
      this.inlineChartTitle = name;
      this.inlineChartSerial = serial;
      this.inlineChartLoading = true;
      this.inlineChartEmpty = false;
      this.inlineChartTab = 'VOLTAGE';
      const data = await fetchJSON(`api/gateways/bms/sessions/${sessionId}`);
      this.inlineChartLoading = false;
      const payload = data?.payload;
      if (!payload || !payload.times || !payload.times.length) {
        this.inlineChartEmpty = true;
        return;
      }
      this.inlineChartTimes = payload.times;
      this.inlineChartVoltages = payload.voltages || [];
      this.inlineChartTemps = payload.temps || [];
      this.renderInlineChart();
    },

    renderInlineChart() {
      if (this.inlineChartTab === 'COMPARISON') return;
      this.$nextTick(() => {
        const ctx = document.getElementById('bmsCanvasInline');
        if (!ctx) return;
        if (this.inlineChartInstance) { this.inlineChartInstance.destroy(); }
        if (typeof Chart === 'undefined') return;
        const datasets = [];
        const colors = ['#ef4444','#f97316','#f59e0b','#eab308','#84cc16','#22c55e','#10b981','#14b8a6','#06b6d4','#0ea5e9','#3b82f6','#6366f1','#8b5cf6','#a855f7','#d946ef','#ec4899'];
        let yMin = null, yMax = null, yTitle = '';
        const vols = this.inlineChartVoltages;
        const temps = this.inlineChartTemps;
        if (this.inlineChartTab === 'VOLTAGE') {
          for (let i = 0; i < 16; i++) {
            datasets.push({ label: 'Cell '+(i+1), data: vols.map(s => s[i] ? s[i]/1000 : null), borderColor: colors[i], borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, tension: 0.1 });
          }
          yTitle = 'Voltage (V)';
          const allV = vols.flat().filter(v => v != null && v > 0).map(v => v / 1000);
          if (allV.length) {
            const dMin = Math.min(...allV), dMax = Math.max(...allV);
            const pad = Math.max(0.005, (dMax - dMin) * 0.25);
            yMin = Math.floor((dMin - pad) * 1000) / 1000;
            yMax = Math.ceil((dMax  + pad) * 1000) / 1000;
          } else { yMin = 3.1; yMax = 3.5; }
        } else {
          for (let i = 0; i < 16; i++) {
            datasets.push({ label: 'Cell '+(i+1), data: temps.map(s => s[i] != null ? s[i] : null), borderColor: colors[i], borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, tension: 0.1 });
          }
          yTitle = 'Temperature (\u00b0C)';
          const allT = temps.flat().filter(v => v != null);
          if (allT.length) {
            const dMin = Math.min(...allT), dMax = Math.max(...allT);
            const pad = Math.max(0.5, (dMax - dMin) * 0.25);
            yMin = Math.floor((dMin - pad) * 2) / 2;
            yMax = Math.ceil((dMax  + pad) * 2) / 2;
          }
        }
        const chartOpts = {
          type: 'line',
          data: { labels: this.inlineChartTimes, datasets },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 9 }, color: '#94a3b8' } },
              tooltip: {
                backgroundColor: '#1e293b', titleColor: '#f1f5f9', bodyColor: '#94a3b8', borderColor: '#334155', borderWidth: 1,
                callbacks: { label: (c) => { const unit = this.inlineChartTab === 'VOLTAGE' ? ' V' : ' \u00b0C'; return ` ${c.dataset.label}: ${c.parsed.y != null ? c.parsed.y.toFixed(this.inlineChartTab === 'VOLTAGE' ? 3 : 1) : '\u2014'}${unit}`; } }
              },
            },
            scales: {
              x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b', maxTicksLimit: 10 } },
              y: { display: true, title: { display: true, text: yTitle, color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' }, min: yMin, max: yMax, ticks: { color: '#64748b', maxTicksLimit: 8 } },
            },
          },
        };
        this.inlineChartInstance = new Chart(ctx, chartOpts);
      });
    },

    exportInlineCSV() {
      if (!this.inlineChartTimes?.length) return;
      let csv = 'Time,';
      for (let i=1;i<=16;i++) csv += `Cell${i}_mV,`;
      for (let i=1;i<=16;i++) csv += `Cell${i}_C,`;
      csv += '\n';
      for (let row=0; row<this.inlineChartTimes.length; row++) {
        csv += `${this.inlineChartTimes[row]},`;
        for (let i=0;i<16;i++) csv += `${this.inlineChartVoltages[row]?.[i]||''},`;
        for (let i=0;i<16;i++) csv += `${this.inlineChartTemps[row]?.[i]!=null?this.inlineChartTemps[row][i]:''},`;
        csv += '\n';
      }
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.hidden = true; a.href = url; a.download = `fwh_bms_${(this.inlineChartSerial||'').replace(/[^A-Za-z0-9]/g,'_')}_${Date.now()}.csv`;
      document.body.appendChild(a); a.click();
      window.URL.revokeObjectURL(url); document.body.removeChild(a);
    },

    async deleteInlineSession(sessionId) {
      if (!confirm('Permanently delete this session?')) return;
      await fetchJSON(`api/gateways/bms/sessions/${sessionId}`, { method: 'DELETE' });
      this.allSessions = this.allSessions.filter(s => s.session_id !== sessionId);
      if (this.selectedInlineSessionId === sessionId) {
        this.inlineChartEmpty = true;
        this.inlineChartTitle = '';
        this.selectedInlineSessionId = null;
      }
    },

    safelyGetApowerInfo(serial, key, defaultVal) {
      if (!this.apowerData || !this.apowerData.length) return defaultVal;
      const rec = this.apowerData.find(a => a.apowerSn === serial);
      if (!rec) return defaultVal;
      return rec[key] !== undefined && rec[key] !== null && rec[key] !== '' ? rec[key] : defaultVal;
    },

    remainingKwh(serial, soc) {
      // Prefer direct remainingPower field from get_apower_info() when available.
      // Falls back to: ratedCapacity × SOC% / 100.
      // Further fallback: assume 13.6 kWh (same default as Capacity Rated card).
      const direct = this.safelyGetApowerInfo(serial, 'remainingPower', null);
      if (direct !== null && direct !== undefined && direct !== '') {
        return parseFloat(direct).toFixed(1);
      }
      const capRaw = this.safelyGetApowerInfo(serial, 'ratedCapacity', '13.6');
      const cap = parseFloat(capRaw);
      if (isNaN(cap) || cap <= 0 || soc == null || soc === '') return '—';
      return (cap * parseFloat(soc) / 100).toFixed(1);
    },

    decodeRunMode(code)     { const m={0:'Off',1:'On',8:'Debug'}; return m[code]??(code!=null?'Mode '+code:'—'); },
    decodeBmsState(code)    { const s={0:'Off',1:'Init',2:'Pre-charge',3:'Standby',4:'Ready',5:'Standby',6:'Charging',7:'Discharging',8:'Debug/Fault',9:'Shutdown'}; return s[code]??(code!=null?'St '+code:'—'); },
    decodeInvStatus(code)   { const s={0:'Off',1:'Init',2:'Pre-charge',3:'Standby',4:'Ready',5:'Standby',6:'Charging',7:'Discharging',8:'Debug/Fault',9:'Shutdown'}; return s[code]??(code!=null?'St '+code:'—'); },
    decodeDcdcStatus(code)  { if(code==null)return'—'; const s={0:'Off',1:'Init',2:'Standby',3:'Soft-Start',4:'Buck',5:'Boost',6:'Buck-Boost',7:'Running',8:'Fault',9:'Shutdown'}; return s[code]??('Code '+code); },
    decodeMosState(code)    { const s={0:'All Off',1:'Charge Only',2:'Discharge Only',3:'Chg+Dis',17:'Pre+Chg',19:'Pre+Both',33:'DCDC+Chg',35:'DCDC+Both',48:'DCDC+Pre',51:'All Active'}; return s[code]??(code!=null?'0x'+code.toString(16).toUpperCase():'—'); },
    decodeSwitchState(code) { const s={0:'Open',1:'Closed',2:'Pre-close',3:'Fault'}; return s[code]??(code!=null?'Code '+code:'—'); },

    // Format total-seconds → hh:mm:ss (hours omitted when < 1h for clean display)
    fmtSecs(totalSec) {
      totalSec = Math.max(0, Math.round(totalSec));
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;
      const mm = String(m).padStart(2, '0');
      const ss = String(s).padStart(2, '0');
      return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },
  };
}
