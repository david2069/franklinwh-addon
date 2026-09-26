// metrics_tab.js — Alpine component for Metrics / Telemetry tab
function metricsTab() {
  return {
    currentView: 'cloud', // 'cloud' | 'energy_flow' | 'power_history' | 'telemetry'

    // Batch Q (2026-07-30) — Power History view state
    phWindow: '24h',
    phRows: [],
    phModeTransitions: [],
    phStaleRows: [],
    phLoading: false,
    phShowStale: false,
    _phChart: null,
    
    // Cloud Reporting state
    cloudType: "1_5m", // default: Day 5-Minute Power (was "1" hourly)
    cloudDate: new Date().toISOString().split('T')[0], // yyyy-mm-dd
    cloudSubTab: 'system', // 'system', 'home', 'apower', 'grid', 'solar'
    cloudData: null,
    cloudLoading: false,
    cloudFlowSummary: {
      solar:   { total: 0, toHome: 0, toBattery: 0, toGrid: 0 },
      battery: { total: 0, toHome: 0, toGrid: 0, fromSolar: 0, fromGrid: 0, charged: 0 },
      grid:    { total: 0, toBattery: 0, toHome: 0, exported: 0 },
      home:    { total: 0, fromSolar: 0, fromBattery: 0, fromGrid: 0 },
      isDetailed: false,
      // BD-20 / #23 — distinguishes "cloud returned no timeseries for this
      // time span" from "genuinely zero energy". Day view sets hasData=true
      // as long as the endpoint responded (arrays present). Week/Month/Year/
      // Lifetime views set hasData=true only if at least one energy-total
      // array has entries, AND at least one has a non-zero sum — matching
      // the cloud's actual coverage for that period type.
      hasData: false,
    },

    // Energy Flow (Sankey) state
    sankeyData: null,
    sankeyLoading: false,
    sankeyReady: false,
    sankeyFocus: null,   // null | 'solar' | 'battery' | 'grid' | 'home' — card-click focus filter
    _sankeyChart: null,
    flowSummary: {
      solar:   { total: 0, toHome: 0, toBattery: 0, toGrid: 0 },
      battery: { total: 0, toHome: 0, toGrid: 0, fromGrid: 0, fromSolar: 0, netCharge: 0 },
      grid:    { total: 0, toBattery: 0, toHome: 0, exportFromBattery: 0, exportFromSolar: 0 },
      home:    { total: 0 },
    },

    gateways: [],
    selectedGw: '',
    rows: [],
    loading: false,
    showRaw: false,
    showFields: false,
    selectedFields: ['operating_mode','run_status_desc','battery_soc','battery_kw','home_kw','grid_kw'],
    availableFields: [
      { key:'battery_soc',       label:'Batt SoC',      unit:'%'   },
      { key:'battery_kw',        label:'Battery Power', unit:'kW'  },
      { key:'grid_kw',           label:'Grid Power',    unit:'kW'  },
      { key:'home_kw',           label:'Home Load',     unit:'kW'  },
      { key:'solar_kw',          label:'Solar Output',  unit:'kW'  },
      { key:'operating_mode',    label:'Runtime Mode',  unit:''    },
      { key:'run_status_desc',   label:'Run Status',    unit:''    },
      { key:'grid_frequency',    label:'Grid Freq',     unit:'Hz'  },
      { key:'wifi_signal',       label:'WiFi Signal',   unit:'dBm' },
      { key:'mobile_signal',     label:'Mobile Signal', unit:'dBm' },
      { key:'mppt_active_power', label:'MPPT Power',    unit:'kW'  },
      { key:'ac_voltage',        label:'AC Voltage',    unit:'V'   },
      { key:'smart_circuit_1_state', label:'Smart Circuit 1', unit:'' },
      { key:'generator_status',  label:'Generator',     unit:''    },
      { key:'alarms_count',      label:'Alarms Count',  unit:''    },
    ],
    powerFields: [
      { label:'Battery SoC',   path:'battery_soc',       unit:'%'  },
      { label:'Battery Power', path:'battery_use',        unit:'kW' },
      { label:'Grid Power',    path:'grid_use',           unit:'kW' },
      { label:'Home Load',     path:'home_load',          unit:'kW' },
      { label:'Solar',         path:'solar_production',   unit:'kW' },
      { label:'Grid Freq',     path:'grid_frequency',     unit:'Hz' },
    ],

    get latest() {
      if (!this.rows.length) return null;
      const r = this.rows[0];
      return { ...r, data: typeof r.data_json === 'string'
        ? (() => { try { return JSON.parse(r.data_json); } catch { return {}; } })()
        : (r.data_json || {}) };
    },

    formatField(data, key) {
      if (!data) return '—';
      const field = this.availableFields.find(f => f.key === key);
      let val = this.getVal(data, key);
      if (val == null && key === 'battery_soc')    val = this.getVal(data, 'soc');
      if (val == null && key === 'battery_kw')     val = this.getVal(data, 'battery_use');
      if (val == null && key === 'home_kw')        val = this.getVal(data, 'home_load');
      if (val == null && key === 'grid_kw')        val = this.getVal(data, 'grid_use');
      if (val == null && key === 'solar_kw')       val = this.getVal(data, 'solar_production');
      if (key === 'operating_mode') {
        let rawVal = val ?? this.getVal(data, 'work_mode_desc') ?? this.getVal(data, 'work_mode');
        if (rawVal != null) {
          const modeItem = this.$store.app.MODES?.find(m => String(m.id) === String(rawVal));
          val = modeItem ? modeItem.name : rawVal;
        }
      }
      if (val == null || val === '') return '—';
      return field?.unit ? `${val} ${field.unit}` : val;
    },

    getVal(data, path) {
      if (!data) return null;
      return path.split('.').reduce((o, k) => o?.[k], data) ?? null;
    },

    moveField(idx, direction) {
      if (idx + direction < 0 || idx + direction >= this.selectedFields.length) return;
      const arr = [...this.selectedFields];
      [arr[idx], arr[idx + direction]] = [arr[idx + direction], arr[idx]];
      this.selectedFields = arr;
      this.saveFields();
    },

    saveFields() {
      localStorage.setItem('fwh_metrics_fields', JSON.stringify(this.selectedFields));
    },

    async init() {
      const saved = localStorage.getItem('fwh_metrics_fields');
      if (saved) {
        try {
          const parsed = JSON.parse(saved);
          if (Array.isArray(parsed) && parsed.length > 0) this.selectedFields = parsed;
        } catch {}
      }
      
      // Fix for SPA caching: re-fire load on activeTab = metrics
      if (this.$store && this.$store.app) {
        this.$watch('$store.app.activeTab', (newVal) => {
          if (newVal === 'metrics') {
            if (this.currentView === 'telemetry') {
               // Only reload if we haven't loaded recently to avoid spam
               this.load();
            } else {
               this.loadCloud();
            }
          }
        });
      }

      const gws = await fetchJSON('api/gateways');
      if (gws) this.gateways = gws;
      if (this.gateways.length > 0) {
        this.selectedGw = this.gateways[0].short_id;
      }
      
      this.loadCloud();
      this.load(); // preload telemetry in background
    },

    onGwChange() {
      if (this.currentView === 'cloud') {
         this.loadCloud();
      } else if (this.currentView === 'energy_flow') {
         if (this.sankeyData) this.loadSankey(); // only reload if already loaded
      } else {
         this.load();
      }
    },

    // ── View switch ──
    setView(v) {
      this.currentView = v;
      if (v === 'cloud') this.loadCloud();
      else if (v === 'telemetry') this.load();
      else if (v === 'energy_flow' && !this.sankeyData) this.loadSankey();
      else if (v === 'power_history') this.loadPowerHistory();
    },

    // ── Batch Q: Power History ──
    async loadPowerHistory() {
      if (!this.selectedGw) {
        // Fall back to first available gateway if none selected
        if (this.gateways && this.gateways.length > 0) {
          this.selectedGw = this.gateways[0].short_id;
        } else {
          return;
        }
      }
      this.phLoading = true;
      try {
        const url = `api/metrics/timeline?short_id=${this.selectedGw}&last=${this.phWindow}`;
        const d = await fetchJSON(url);
        if (d.ok) {
          this.phRows = d.rows || [];
          this.phModeTransitions = d.mode_transitions || [];
          this.phStaleRows = this.phRows.filter(r => r.is_stale);
          // Wait for Alpine to render the canvas before drawing
          this.$nextTick(() => this._renderPhChart());
        }
      } catch (e) {
        console.warn('loadPowerHistory failed', e);
      } finally {
        this.phLoading = false;
      }
    },

    _renderPhChart() {
      const canvas = document.getElementById('phChart');
      if (!canvas || typeof Chart === 'undefined') return;
      if (this._phChart) {
        this._phChart.destroy();
        this._phChart = null;
      }
      if (!this.phRows.length) return;

      // Build datasets — hide null points so the line breaks cleanly on stale drops
      const labels = this.phRows.map(r => (r.ts || '').substring(11));  // HH:MM:SS
      const mkSeries = (label, colour, key, axis = 'y') => ({
        label, borderColor: colour, backgroundColor: colour + '20',
        data: this.phRows.map(r => r[key] == null ? null : r[key]),
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
        tension: 0.15, yAxisID: axis, spanGaps: false,
      });

      const datasets = [
        mkSeries('Battery kW', '#22d3ee', 'batt_kw'),   // cyan (Bridge palette)
        mkSeries('Grid kW',    '#ef4444', 'grid_kw'),   // red
        mkSeries('Home kW',    '#a78bfa', 'home_kw'),   // purple
        mkSeries('Solar kW',   '#f59e0b', 'solar_kw'),  // amber
        mkSeries('SoC %',      '#10b981', 'soc', 'y1'), // green on secondary axis
      ];

      // Mode-transition annotations (vertical lines)
      const annotations = {};
      (this.phModeTransitions || []).forEach((t, i) => {
        const idx = this.phRows.findIndex(r => r.ts === t.ts);
        if (idx < 0) return;
        const colour = (t.to || '').includes('VPP') ? '#a855f7'
                     : (t.to || '').includes('Self') ? '#10b981'
                     : (t.to || '').includes('Time') ? '#3b82f6'
                     : '#64748b';
        annotations['mode_' + i] = {
          type: 'line',
          xMin: idx, xMax: idx,
          borderColor: colour, borderWidth: 1, borderDash: [4, 4],
          label: {
            content: `→ ${t.to}`,
            enabled: true, position: 'start',
            color: colour, backgroundColor: 'rgba(0,0,0,0.7)',
            font: { size: 9, weight: 'bold' },
            padding: 3,
          }
        };
      });

      const ctx = canvas.getContext('2d');
      this._phChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { position: 'bottom',
                      labels: { boxWidth: 12, font: { size: 10 }, color: '#94a3b8' } },
            tooltip: { position: 'nearest',
                       backgroundColor: 'rgba(15,23,42,0.95)',
                       titleColor: '#e2e8f0', bodyColor: '#cbd5e1',
                       borderColor: '#334155', borderWidth: 1 },
            annotation: { annotations },
          },
          scales: {
            x: { grid: { color: 'rgba(255,255,255,0.03)' },
                 ticks: { color: '#64748b', maxRotation: 0, autoSkipPadding: 20 } },
            y: { position: 'left',
                 title: { display: true, text: 'Power (kW)', color: '#64748b' },
                 grid: { color: 'rgba(255,255,255,0.05)' },
                 ticks: { color: '#94a3b8' },
                 suggestedMin: -5.5, suggestedMax: 5.5 },
            y1: { position: 'right',
                  title: { display: true, text: 'SoC (%)', color: '#10b981' },
                  grid: { drawOnChartArea: false },
                  ticks: { color: '#10b981' },
                  min: 0, max: 100 },
          }
        }
      });
    },

    async load() {
      this.loading = true;
      const url = 'api/metrics' + (this.selectedGw ? `?short_id=${this.selectedGw}&limit=30` : '?limit=30');
      const data = await fetchJSON(url);
      this.rows = (Array.isArray(data) ? data : []).map(r => {
        let parsed = {};
        if (typeof r.data_json === 'string') { try { parsed = JSON.parse(r.data_json); } catch {} }
        else { parsed = r.data_json || {}; }
        return { ...r, data: parsed };
      });
      
      // Dynamic fields autodiscovery
      if (this.latest && this.latest.data) {
        const keys = Object.keys(this.latest.data);
        keys.forEach(k => {
          if (!this.availableFields.find(f => f.key === k)) {
            this.availableFields.push({ key: k, label: k, unit: '' });
          }
        });
      }
      
      this.loading = false;
    },

    // ── Cloud Reporting Logic ──
    async loadCloud() {
      if (!this.selectedGw) return;
      this.cloudLoading = true;
      const queryType = this.cloudType === '1_5m' ? '1' : this.cloudType;
      const url = `api/metrics/historical?short_id=${this.selectedGw}&type=${queryType}&timeperiod=${this.cloudDate}`;
      try {
        const response = await fetch(url);
        if (!response.ok) {
           throw new Error(response.status + ' ' + response.statusText);
        }
        const result = await response.json();
        
        if (result?.metrics?.status === 404 || result?.detail === "Gateway client offline") {
           throw new Error("Cloud data not found or gateway is offline.");
        }
        
        let d = result?.metrics || null;
        if (!d || Object.keys(d).length === 0) throw new Error("Empty metrics payload received");

        if (d.deviceTimeArray && Array.isArray(d.deviceTimeArray) && d.deviceTimeArray.length > 1 && String(d.deviceTimeArray[0]).includes(':')) {
            let labels = [];
            const aggregated = { load: [], solar: [], apower_dis: [], apower_chg: [], grid_in: [], grid_out: [], soc: [] };
            const calculateSum = (idx, keys) => {
               let val = 0;
               keys.forEach(k => { if (d[k] && d[k][idx]) val += d[k][idx]; });
               return val;
            };

            if (this.cloudType === '1_5m') {
                // 5-Minute Resolution: Direct array mapping without aggregations
                for (let i = 0; i < d.deviceTimeArray.length; i++) {
                   const dtStr = d.deviceTimeArray[i]; // "2026-04-12 13:05:00"
                   let timeLabel = dtStr.includes(' ') ? dtStr.split(' ')[1].slice(0, 5) : dtStr.slice(0, 5); // "13:05"
                   labels.push(timeLabel);
                   
                   // Raw kW points
                   aggregated.load.push(Math.round(calculateSum(i, ['powerSolarHomeArray', 'powerGirdHomeArray', 'powerFhpHomeArray', 'powerGenHomeArray', 'powerV2lHomeArray']) * 1000) / 1000);
                   aggregated.solar.push(Math.round(calculateSum(i, ['powerSolarHomeArray', 'powerSolarGirdArray', 'powerSolarFhpArray']) * 1000) / 1000);
                   aggregated.apower_dis.push(Math.round(calculateSum(i, ['powerFhpHomeArray', 'powerFhpGirdArray']) * 1000) / 1000);
                   aggregated.apower_chg.push(Math.round(calculateSum(i, ['powerSolarFhpArray', 'powerGirdFhpArray', 'powerGenFhpArray', 'powerV2lFhpArray']) * 1000) / 1000);
                   aggregated.grid_in.push(Math.round(calculateSum(i, ['powerGirdHomeArray', 'powerGirdFhpArray']) * 1000) / 1000);
                   aggregated.grid_out.push(Math.round(calculateSum(i, ['powerSolarGirdArray', 'powerFhpGirdArray']) * 1000) / 1000);
                   
                   if (d.socArray && d.socArray[i] !== '' && typeof d.socArray[i] === 'number') {
                      aggregated.soc.push(Math.round(d.socArray[i]));
                   } else {
                      aggregated.soc.push(null);
                   }
                }
            } else {
                // Hourly Resolution: Aggregate 12 buckets of 5-min intervals into a 1 hour bucket
                let currentHour = null;
                let hourSums = { load: 0, solar: 0, apower_dis: 0, apower_chg: 0, grid_in: 0, grid_out: 0 };
                let hourSocSum = 0;
                let hourSocCount = 0;
                
                for (let i = 0; i < d.deviceTimeArray.length; i++) {
                   const dtStr = d.deviceTimeArray[i];
                   let hour = "00:00";
                   if (dtStr.includes(' ')) {
                       const timePart = dtStr.split(' ')[1];
                       hour = timePart.split(':')[0] + ':00';
                   } else {
                       hour = dtStr.split(':')[0] + ':00';
                   }

                   if (hour !== currentHour) {
                       if (currentHour !== null) {
                           labels.push(currentHour);
                           for (let k in aggregated) {
                             if (k === 'soc') aggregated.soc.push(hourSocCount > 0 ? Math.round(hourSocSum / hourSocCount) : 0);
                             else aggregated[k].push(hourSums[k]);
                           }
                       }
                       currentHour = hour;
                       for (let k in hourSums) hourSums[k] = 0;
                       hourSocSum = 0;
                       hourSocCount = 0;
                   }
                   
                   // Compute kW sum for this interval then convert to energy bucket (kWh div by 12)
                   hourSums.load += calculateSum(i, ['powerSolarHomeArray', 'powerGirdHomeArray', 'powerFhpHomeArray', 'powerGenHomeArray', 'powerV2lHomeArray']) / 12;
                   hourSums.solar += calculateSum(i, ['powerSolarHomeArray', 'powerSolarGirdArray', 'powerSolarFhpArray']) / 12;
                   hourSums.apower_dis += calculateSum(i, ['powerFhpHomeArray', 'powerFhpGirdArray']) / 12;
                   hourSums.apower_chg += calculateSum(i, ['powerSolarFhpArray', 'powerGirdFhpArray', 'powerGenFhpArray', 'powerV2lFhpArray']) / 12;
                   hourSums.grid_in += calculateSum(i, ['powerGirdHomeArray', 'powerGirdFhpArray']) / 12;
                   hourSums.grid_out += calculateSum(i, ['powerSolarGirdArray', 'powerFhpGirdArray']) / 12;
                   
                   if (d.socArray && d.socArray[i] !== '' && typeof d.socArray[i] === 'number') {
                     hourSocSum += d.socArray[i];
                     hourSocCount++;
                   }
                }
                if (currentHour !== null) {
                   labels.push(currentHour);
                   for (let k in aggregated) {
                      if (k === 'soc') aggregated.soc.push(hourSocCount > 0 ? Math.round(hourSocSum / hourSocCount) : 0);
                      else aggregated[k].push(hourSums[k]);
                   }
                }
            }

            d.deviceTimeArray = labels;
            d.aggregatedSocArray = aggregated.soc;
            d.kwhLoadArray = aggregated.load.map(v => Math.round(v*1000)/1000);
            d.kwhSuArray = aggregated.solar.map(v => Math.round(v*1000)/1000);
            d.kwhFhpDiArray = aggregated.apower_dis.map(v => Math.round(v*1000)/1000);
            d.kwhFhpChgArray = aggregated.apower_chg.map(v => Math.round(v*1000)/1000);
            d.kwhUtiInArray = aggregated.grid_in.map(v => Math.round(v*1000)/1000);
            d.kwhUtiOutArray = aggregated.grid_out.map(v => Math.round(v*1000)/1000);
        }

        // Calculate Aggregate Totals automatically for Weekly/Monthly/Annual scales
        const sumArray = (arr) => Array.isArray(arr) ? arr.reduce((a, b) => a + (Number(b) || 0), 0) : 0;
        const sumFactor = (this.cloudType === '1_5m') ? 12 : 1; // 5-minute kW samples must be divided by 12 to equal 1 hour of kWh energy
        d.gridUse = Math.round((sumArray(d.kwhUtiInArray) / sumFactor) * 100) / 100;
        d.solarProduction = Math.round((sumArray(d.kwhSuArray) / sumFactor) * 100) / 100;
        d.batteryDischarge = Math.round((sumArray(d.kwhFhpDiArray) / sumFactor) * 100) / 100;
        d.homeLoad = Math.round((sumArray(d.kwhLoadArray) / sumFactor) * 100) / 100;

        this.cloudData = d;
        this._buildCloudFlowSummary(d);
      } catch (err) {
        console.warn("Cloud metrics fetch error:", err.message);
        // Only show toast when the user is actively on the metrics tab — not during background pre-load
        const isActive = this.$store?.app?.activeTab === 'metrics';
        if (isActive) {
          this.$store.app.addToast('Cloud Data unavailable: ' + err.message, 'error');
        }
        this.cloudData = null;
      }
      this.cloudLoading = false;
      this.renderChart();
    },

    // ═══ Cloud Flow Summary (for card breakdowns) ═══
    // Day periods: 5-min powerXxxArray are already in cloudData — no extra API call needed.
    // Week/Month/Year: only battery charge/discharge and grid export totals are derivable.
    _buildCloudFlowSummary(d) {
      const sum = (arr) => Array.isArray(arr) ? arr.reduce((a, b) => a + (Number(b) || 0), 0) : 0;
      const rnd = (v)   => Math.round(v * 100) / 100;
      const hasEntries = (arr) => Array.isArray(arr) && arr.length > 0;
      const hasRaw = hasEntries(d.powerSolarHomeArray);

      if (hasRaw) {
        // Day (5-Min or Hourly): raw 5-min kW arrays → divide sum by 12 for kWh
        const f = 12;
        const solarToHome = rnd(sum(d.powerSolarHomeArray) / f);
        const solarToBat  = rnd(sum(d.powerSolarFhpArray)  / f);
        const solarToGrid = rnd(sum(d.powerSolarGirdArray)  / f);
        const batToHome   = rnd(sum(d.powerFhpHomeArray)    / f);
        const batToGrid   = rnd(sum(d.powerFhpGirdArray)    / f);
        const gridToBat   = rnd(sum(d.powerGirdFhpArray)    / f);
        const gridToHome  = rnd(sum(d.powerGirdHomeArray)   / f);
        this.cloudFlowSummary = {
          solar:   { total: rnd(solarToHome + solarToBat + solarToGrid), toHome: solarToHome, toBattery: solarToBat, toGrid: solarToGrid },
          battery: { total: rnd(batToHome + batToGrid), toHome: batToHome, toGrid: batToGrid, fromSolar: solarToBat, fromGrid: gridToBat, charged: rnd(solarToBat + gridToBat) },
          grid:    { total: rnd(gridToBat + gridToHome), toBattery: gridToBat, toHome: gridToHome, exported: rnd(batToGrid + solarToGrid) },
          home:    { total: rnd(solarToHome + batToHome + gridToHome), fromSolar: solarToHome, fromBattery: batToHome, fromGrid: gridToHome },
          isDetailed: true,
          // Day view arrays were returned → data present, even if energy totals are 0.
          hasData: true,
        };
      } else {
        // Week / Month / Year / Lifetime: only kWh totals for battery and grid available
        const charged   = rnd(sum(d.kwhFhpChgArray));
        const exported  = rnd(sum(d.kwhUtiOutArray));
        // BD-20 / #23 — hasData is FALSE when the cloud returned no energy
        // arrays at all (endpoint responded but coverage for this time span
        // is unimplemented / missing). That case previously rendered as
        // "0.00 kWh" — misleading. Distinguish it from a genuine "you truly
        // had zero solar production" so the UI can render a placeholder.
        const coverage = [
          d.kwhSuArray, d.kwhFhpDiArray, d.kwhUtiInArray, d.kwhLoadArray,
          d.kwhFhpChgArray, d.kwhUtiOutArray,
        ];
        const anyArrayPresent = coverage.some(hasEntries);
        this.cloudFlowSummary = {
          solar:   { total: d.solarProduction || 0 },
          battery: { total: d.batteryDischarge || 0, charged },
          grid:    { total: d.gridUse || 0, exported },
          home:    { total: d.homeLoad || 0 },
          isDetailed: false,
          hasData: anyArrayPresent,
        };
      }
    },

    renderChart() {
      this.$nextTick(() => {
        if (!this.cloudData) return;
        const container = document.getElementById('chart-mount');
        if (!container) return;
        
        if (window._fwhCloudChart) {
          window._fwhCloudChart.destroy();
          window._fwhCloudChart = null;
        }
        
        // Dynamically recreate the canvas to severe all leftover Alpine Proxy DOM bindings completely!
        container.innerHTML = '<canvas id="cloud-metrics-chart"></canvas>';
        const ctx = document.getElementById('cloud-metrics-chart');
        
        const d = this.cloudData || {};
        let labels = d.deviceTimeArray || d.deviceDayArray || d.deviceMonthArray || [];

        let dataset1 = [];
        let dataset2 = [];
        let dataset3 = [];
        let dataset4 = [];
        let label1 = 'Load';
        let label2 = 'Solar';
        let label3 = '';
        let label4 = '';

        if (this.cloudSubTab === 'system') {
           label1 = 'Home Load';
           label2 = 'Solar';
           label3 = 'Battery (Discharge+/Charge-)';
           label4 = 'Grid (Import+/Export-)';
           dataset1 = d.kwhLoadArray || [];
           dataset2 = d.kwhSuArray || [];
           
           // Combine Battery: Discharge is Positive, Charging is Negative
           const bDi = d.kwhFhpDiArray || [];
           const bCh = d.kwhFhpChgArray || [];
           dataset3 = bDi.map((v, i) => v > 0 ? v : (bCh[i] ? -Math.abs(bCh[i]) : 0));
           
           // Combine Grid: Import is Positive, Export is Negative
           const gIn = d.kwhUtiInArray || [];
           const gOut = d.kwhUtiOutArray || [];
           dataset4 = gIn.map((v, i) => v > 0 ? v : (gOut[i] ? -Math.abs(gOut[i]) : 0));
           
        } else if (this.cloudSubTab === 'home') {
           label1 = 'Consumption';
           dataset1 = d.kwhLoadArray || [];
        } else if (this.cloudSubTab === 'apower') {
           label1 = 'Discharged';
           label2 = 'Charged';
           dataset1 = d.kwhFhpDiArray || [];
           dataset2 = (d.kwhFhpChgArray || []).map(v => v ? -Math.abs(v) : 0);
        } else if (this.cloudSubTab === 'grid') {
           label1 = 'Imported';
           label2 = 'Exported';
           dataset1 = d.kwhUtiInArray || [];
           dataset2 = (d.kwhUtiOutArray || []).map(v => v ? -Math.abs(v) : 0);
        } else if (this.cloudSubTab === 'solar') {
           label1 = 'Production';
           dataset1 = d.kwhSuArray || [];
        }

        // Avoid rendering empty chart if no data
        if (labels.length === 0) return;

        // Strip Alpine Proxies, which corrupt ChartJS v4 internal data handling arrays
        const cleanLabels = JSON.parse(JSON.stringify(labels));
        const cleanData1 = JSON.parse(JSON.stringify(dataset1));
        const cleanData2 = JSON.parse(JSON.stringify(dataset2));
        const cleanData3 = JSON.parse(JSON.stringify(dataset3));
        const cleanData4 = JSON.parse(JSON.stringify(dataset4));

        const isAreaLines = (this.cloudType === '1_5m');
        const mainChartType = isAreaLines ? 'line' : 'bar';
        const lineProps = isAreaLines ? { fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 } : { borderWidth: 1 };

        window._fwhCloudChart = new Chart(ctx, {
          type: mainChartType,
          data: {
            labels: cleanLabels,
            datasets: [
              {
                label: label1,
                data: cleanData1,
                backgroundColor: isAreaLines ? 'rgba(56, 189, 248, 0.2)' : 'rgba(59, 130, 246, 0.7)',
                borderColor: 'rgba(56, 189, 248, 1)',
                ...lineProps
              },
              ...(cleanData2.length > 0 ? [{
                label: label2,
                data: cleanData2,
                backgroundColor: isAreaLines ? 'rgba(234, 179, 8, 0.2)' : 'rgba(234, 179, 8, 0.7)',
                borderColor: 'rgba(234, 179, 8, 1)',
                ...lineProps
              }] : []),
              ...(cleanData3.length > 0 ? [{
                label: label3,
                data: cleanData3,
                backgroundColor: isAreaLines ? 'rgba(34, 197, 94, 0.2)' : 'rgba(34, 197, 94, 0.7)', // Green
                borderColor: 'rgba(34, 197, 94, 1)',
                ...lineProps
              }] : []),
              ...(cleanData4.length > 0 ? [{
                label: label4,
                data: cleanData4,
                backgroundColor: isAreaLines ? 'rgba(168, 85, 247, 0.2)' : 'rgba(168, 85, 247, 0.7)', // Purple
                borderColor: 'rgba(168, 85, 247, 1)',
                ...lineProps
              }] : [])
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
               y: { beginAtZero: true }
            },
            plugins: {
              tooltip: {
                callbacks: {
                  label: (c) => {
                    const v = c.parsed.y;
                    return ` ${c.dataset.label}: ${v != null ? v.toFixed(2) : '—'} kWh`;
                  }
                }
              }
            }
          }
        });

        // Optional Secondary Chart for SOC
        const socContainer = document.getElementById('chart-soc-mount');
        if (socContainer && (this.cloudType === '1' || this.cloudType === '1_5m') && d.aggregatedSocArray) {
            if (window._fwhCloudSocChart) {
                window._fwhCloudSocChart.destroy();
                window._fwhCloudSocChart = null;
            }
            socContainer.innerHTML = '<canvas id="cloud-soc-chart"></canvas>';
            const socCtx = document.getElementById('cloud-soc-chart');
            const cleanSoc = JSON.parse(JSON.stringify(d.aggregatedSocArray));
            
            window._fwhCloudSocChart = new Chart(socCtx, {
               type: 'line',
               data: {
                  labels: cleanLabels,
                  datasets: [{
                     label: 'SOC %',
                     data: cleanSoc,
                     fill: true,
                     backgroundColor: 'rgba(34, 197, 94, 0.2)',
                     borderColor: 'rgba(34, 197, 94, 0.8)',
                     borderWidth: 2,
                     pointRadius: 0,
                     pointHoverRadius: 4,
                     tension: 0.3
                  }]
               },
               options: {
                  responsive: true,
                  maintainAspectRatio: false,
                  layout: {
                     padding: { left: 0, right: 0, top: 0, bottom: 0 }
                  },
                  scales: {
                     x: { display: false },
                     y: { 
                        min: 0, max: 100, 
                        ticks: { stepSize: 50, color: 'rgba(156,163,175,0.8)', font: { size: 10 } },
                        position: 'left'
                     }
                  },
                  plugins: {
                     legend: { display: false },
                     title: {
                        display: true,
                        text: 'Battery State of Charge (%)',
                        position: 'bottom',
                        color: 'rgba(156,163,175,0.7)',
                        font: { size: 10, weight: 'bold' },
                        padding: { top: 6, bottom: 0 }
                     }
                  }
               }
            });
        } else if (window._fwhCloudSocChart) {
            window._fwhCloudSocChart.destroy();
            window._fwhCloudSocChart = null;
        }

      });
    },

    downloadCloudData() {
      if (!this.cloudData) return;
      const dataStr = JSON.stringify(this.cloudData, null, 2);
      const blob = new Blob([dataStr], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `fwh_metrics_${this.cloudDate}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    // ════════════════════════════════════════════
    // ENERGY FLOW SANKEY METHODS
    // AP-4 compliant: no background polling — loadSankey() called on-demand only.
    // Uses same cloudDate / cloudType as Cloud Reporting (shared date state).
    // ════════════════════════════════════════════

    async loadSankey() {
      if (!this.selectedGw) return;
      this.sankeyLoading = true;
      this.sankeyReady = false;
      this.sankeyFocus = null;  // reset focus on fresh load
      const queryType = this.cloudType === '1_5m' ? '1' : this.cloudType;
      const url = `api/metrics/historical?short_id=${this.selectedGw}&type=${queryType}&timeperiod=${this.cloudDate}`;
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(resp.status + ' ' + resp.statusText);
        const result = await resp.json();
        if (result?.metrics?.status === 404 || result?.detail === 'Gateway client offline')
          throw new Error('Cloud data not found or gateway offline.');
        const d = result?.metrics || null;
        if (!d || Object.keys(d).length === 0) throw new Error('Empty metrics payload');
        this.sankeyData = d;
        this._buildFlowSummary(d);
        this._renderSankey(this._getSankeyEdges(d, null), 'kWh');
      } catch (e) {
        console.warn('[Reporting/EnergyFlow] Sankey load error:', e.message);
        const isActive = this.$store?.app?.activeTab === 'metrics';
        if (isActive) this.$store?.app?.addToast('Energy Flow unavailable: ' + e.message, 'error');
        this.sankeyData = null;
      }
      this.sankeyLoading = false;
    },


    _buildFlowSummary(d) {
      const sum = (arr) => Array.isArray(arr) ? arr.reduce((a, b) => a + (Number(b) || 0), 0) : 0;
      const f = (this.cloudType === '1_5m') ? 12 : 1;
      const rnd = (v) => Math.round(v * 100) / 100;

      const solarToHome    = rnd(sum(d.powerSolarHomeArray) / f);
      const solarToBat     = rnd(sum(d.powerSolarFhpArray)  / f);
      const solarToGrid    = rnd(sum(d.powerSolarGirdArray)  / f);
      const batToHome      = rnd(sum(d.powerFhpHomeArray)    / f);
      const batToGrid      = rnd(sum(d.powerFhpGirdArray)    / f);
      const gridToBat      = rnd(sum(d.powerGirdFhpArray)    / f);
      const gridToHome     = rnd(sum(d.powerGirdHomeArray)   / f);

      // Battery Flows: discharge total (out) + charge breakdown (in)
      const batDischarge   = rnd(batToHome + batToGrid);
      const batCharge      = rnd(gridToBat + solarToBat);

      // Grid Flows: import total (in) + export contributions (out via battery/solar)
      const gridImport     = rnd(gridToBat + gridToHome);
      const gridExportBat  = batToGrid;   // battery → grid
      const gridExportSol  = solarToGrid; // solar → grid

      this.flowSummary = {
        solar:   { total: rnd(solarToHome + solarToBat + solarToGrid), toHome: solarToHome, toBattery: solarToBat, toGrid: solarToGrid },
        battery: { total: batDischarge, toHome: batToHome, toGrid: batToGrid, fromGrid: gridToBat, fromSolar: solarToBat, netCharge: batCharge },
        grid:    { total: gridImport, toBattery: gridToBat, toHome: gridToHome, exportFromBattery: gridExportBat, exportFromSolar: gridExportSol },
        home:    { total: rnd(solarToHome + batToHome + gridToHome) },
      };
    },

    _getSankeyEdges(d, focus) {
      // focus: null = all edges, 'solar'|'battery'|'grid'|'home' = card-focused view
      const sum = (arr) => Array.isArray(arr) ? arr.reduce((a, b) => a + (Number(b) || 0), 0) : 0;
      const f = (this.cloudType === '1_5m') ? 12 : 1;
      const all = [
        { from: 'Solar',   to: 'Battery',     flow: Math.round(sum(d.powerSolarFhpArray)  / f * 1000) / 1000 },
        { from: 'Solar',   to: 'Grid Export', flow: Math.round(sum(d.powerSolarGirdArray) / f * 1000) / 1000 },
        { from: 'Solar',   to: 'Home Load',   flow: Math.round(sum(d.powerSolarHomeArray) / f * 1000) / 1000 },
        { from: 'Battery', to: 'Home Load',   flow: Math.round(sum(d.powerFhpHomeArray)   / f * 1000) / 1000 },
        { from: 'Battery', to: 'Grid Export', flow: Math.round(sum(d.powerFhpGirdArray)   / f * 1000) / 1000 },
        { from: 'Grid',    to: 'Battery',     flow: Math.round(sum(d.powerGirdFhpArray)   / f * 1000) / 1000 },
        { from: 'Grid',    to: 'Home Load',   flow: Math.round(sum(d.powerGirdHomeArray)  / f * 1000) / 1000 },
      ];
      // Apply focus filter
      let edges = all;
      if (focus === 'solar') {
        // Only edges originating FROM Solar
        edges = all.filter(e => e.from === 'Solar');
      } else if (focus === 'battery') {
        // All edges touching Battery (both inflows and outflows — Battery as pivot)
        edges = all.filter(e => e.from === 'Battery' || e.to === 'Battery');
      } else if (focus === 'grid') {
        // Grid imports + both export destinations (battery→grid export, solar→grid export)
        edges = all.filter(e => e.from === 'Grid' || e.to === 'Grid Export');
      } else if (focus === 'home') {
        // Only edges flowing INTO Home Load
        edges = all.filter(e => e.to === 'Home Load');
      }
      return edges.filter(e => e.flow > 0.005);
    },

    // Toggle card focus — clicking active card resets to full view
    focusCard(card) {
      this.sankeyFocus = (this.sankeyFocus === card) ? null : card;
      if (this.sankeyData) {
        this._renderSankey(this._getSankeyEdges(this.sankeyData, this.sankeyFocus), 'kWh');
      }
    },

    _SANKEY_NODE_COLORS: {
      'Solar':       'rgba(234,179,8,0.85)',
      'Battery':     'rgba(34,197,94,0.85)',
      'Grid':        'rgba(99,102,241,0.85)',
      'Home Load':   'rgba(56,189,248,0.85)',
      'Grid Export': 'rgba(168,85,247,0.85)',
    },

    _colorForNode(label) {
      return this._SANKEY_NODE_COLORS[label] || 'rgba(150,150,150,0.8)';
    },

    _renderSankey(edges, unit) {
      this.$nextTick(() => {
        const container = document.getElementById('reporting-sankey-mount');
        if (!container) return;
        if (this._sankeyChart) { this._sankeyChart.destroy(); this._sankeyChart = null; }
        const canvasId = 'reporting-sankey-canvas-' + Date.now();
        container.innerHTML = `<canvas id="${canvasId}"></canvas>`;
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        if (!edges || edges.length === 0) {
          container.innerHTML = '<div class="text-center text-gray-500 py-16 text-sm">No energy flow data for this period.</div>';
          return;
        }
        const self = this;
        try {
          this._sankeyChart = new Chart(ctx, {
            type: 'sankey',
            data: {
              datasets: [{
                label: 'Energy Flow',
                data: edges.map(e => ({ from: e.from, to: e.to, flow: e.flow })),
                colorFrom: (c) => self._colorForNode(c.dataset.data[c.dataIndex]?.from || ''),
                colorTo:   (c) => self._colorForNode(c.dataset.data[c.dataIndex]?.to   || ''),
                colorMode: 'gradient',
                borderWidth: 0,
                nodeWidth: 16,
                nodePadding: 12,
                labels: {
                  Solar: 'Solar', Battery: 'Battery', Grid: 'Grid',
                  'Home Load': 'Home', 'Grid Export': 'Grid Export',
                },
              }]
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: false },
                tooltip: {
                  callbacks: {
                    label: (c) => {
                      const d = c.dataset.data[c.dataIndex];
                      return ` ${d.from} → ${d.to}: ${d.flow.toFixed(2)} ${unit}`;
                    }
                  }
                }
              }
            }
          });
          this.sankeyReady = true;
        } catch (e) {
          console.error('[Reporting/EnergyFlow] Render error:', e);
          container.innerHTML = `<div class="text-center text-red-400 py-12 text-sm font-bold">Chart error: ${e.message}</div>`;
        }
      });
    },

    sankeyPrevDate() {
      const d = new Date(this.cloudDate);
      if (this.cloudType === '2') d.setDate(d.getDate() - 7);
      else if (this.cloudType === '3') d.setMonth(d.getMonth() - 1);
      else if (this.cloudType === '4') d.setFullYear(d.getFullYear() - 1);
      else d.setDate(d.getDate() - 1);
      this.cloudDate = d.toISOString().split('T')[0];
    },

    sankeyNextDate() {
      const today = new Date().toISOString().split('T')[0];
      if (this.cloudDate >= today) return;
      const d = new Date(this.cloudDate);
      if (this.cloudType === '2') d.setDate(d.getDate() + 7);
      else if (this.cloudType === '3') d.setMonth(d.getMonth() + 1);
      else if (this.cloudType === '4') d.setFullYear(d.getFullYear() + 1);
      else d.setDate(d.getDate() + 1);
      this.cloudDate = d.toISOString().split('T')[0];
    },

  };
}
