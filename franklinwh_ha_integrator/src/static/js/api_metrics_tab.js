// api_metrics_tab.js — Alpine component for API Metrics tab


function apiMetricsTab() {

  // ── Static CloudFront PoP coordinate database (prefix -> lat/lon/city)
  // Lookup key = first 3 chars of PoP code (e.g. SYD62-P1 -> SYD)
  const POP_COORDS = {
    SYD: { lat: -33.8688, lon:  151.2093, city: 'Sydney, AU' },
    MEL: { lat: -37.8136, lon:  144.9631, city: 'Melbourne, AU' },
    BNE: { lat: -27.4698, lon:  153.0251, city: 'Brisbane, AU' },
    PER: { lat: -31.9505, lon:  115.8605, city: 'Perth, AU' },
    ADL: { lat: -34.9285, lon:  138.6007, city: 'Adelaide, AU' },
    AKL: { lat: -36.8485, lon:  174.7633, city: 'Auckland, NZ' },
    CHC: { lat: -43.5321, lon:  172.6362, city: 'Christchurch, NZ' },
    SIN: { lat:   1.3521, lon:  103.8198, city: 'Singapore' },
    KUL: { lat:   3.1390, lon:  101.6869, city: 'Kuala Lumpur, MY' },
    NRT: { lat:  35.6762, lon:  139.6503, city: 'Tokyo, JP' },
    OSA: { lat:  34.6937, lon:  135.5023, city: 'Osaka, JP' },
    ICN: { lat:  37.5665, lon:  126.9780, city: 'Seoul, KR' },
    HKG: { lat:  22.3193, lon:  114.1694, city: 'Hong Kong' },
    TPE: { lat:  25.0330, lon:  121.5654, city: 'Taipei, TW' },
    MUM: { lat:  19.0760, lon:   72.8777, city: 'Mumbai, IN' },
    DEL: { lat:  28.6139, lon:   77.2090, city: 'Delhi, IN' },
    LAX: { lat:  34.0522, lon: -118.2437, city: 'Los Angeles, US' },
    SFO: { lat:  37.7749, lon: -122.4194, city: 'San Francisco, US' },
    SEA: { lat:  47.6062, lon: -122.3321, city: 'Seattle, US' },
    IAD: { lat:  38.9519, lon:  -77.4480, city: 'Ashburn VA, US' },
    ORD: { lat:  41.8781, lon:  -87.6298, city: 'Chicago, US' },
    DFW: { lat:  32.7767, lon:  -96.7970, city: 'Dallas, US' },
    ATL: { lat:  33.7490, lon:  -84.3880, city: 'Atlanta, US' },
    MIA: { lat:  25.7617, lon:  -80.1918, city: 'Miami, US' },
    JFK: { lat:  40.6413, lon:  -73.7781, city: 'New York, US' },
    GRU: { lat: -23.5505, lon:  -46.6333, city: 'Sao Paulo, BR' },
    LHR: { lat:  51.5074, lon:   -0.1278, city: 'London, UK' },
    AMS: { lat:  52.3676, lon:    4.9041, city: 'Amsterdam, NL' },
    FRA: { lat:  50.1109, lon:    8.6821, city: 'Frankfurt, DE' },
    CDG: { lat:  48.8566, lon:    2.3522, city: 'Paris, FR' },
    MAD: { lat:  40.4168, lon:   -3.7038, city: 'Madrid, ES' },
    MXP: { lat:  45.4654, lon:    9.1859, city: 'Milan, IT' },
    ARN: { lat:  59.3293, lon:   18.0686, city: 'Stockholm, SE' },
    JNB: { lat: -26.2041, lon:   28.0473, city: 'Johannesburg, ZA' },
    DXB: { lat:  25.2048, lon:   55.2708, city: 'Dubai, AE' },
  };

  return {
    gateways: [],
    selectedGw: '',
    metrics: null,
    metricTimeframe: 1, // 0 = all time, 1 = today, 7 = 7 days, 30 = 30 days

    // Internal section tab
    internalTab: 'overview',

    // Deep-link window state
    errLinkWindow: 0,

    // PoP map state
    popMapPeriod: 1,
    popMapData: null,
    popMapLoading: false,
    popMapUnknown: [],
    popMapDominant: null,
    popMapView: 'map',          // kept for compat — map is now always shown
    popTransitionsOpen: true,   // floating transitions panel expanded/collapsed
    edgeDetailOpen: false,       // right-column Edge Detail accordion (CF Edge + Errors & Retries)
    popMapOpen: false,          // modal open/closed
    _leafletMap: null,
    _markerGroup: null,     // L.layerGroup — efficient clearLayers() vs marker.remove() loop
    _leafletInited: false,
    _fetchSeq: 0,           // incremented each fetch; stale renders are discarded

    // Per-table trace filters (D2 fix — independent filters for snake_case vs camelCase tables)
    methodFilter: '',
    endpointFilter: '',

    // Rate Limiter Guard
    rl: {
      calls_last_minute: 0, calls_last_hour: 0, calls_today: 0,
      limit_per_minute: 120, limit_per_hour: 1500, daily_budget: 15000,
      remaining_daily: null, is_throttled: false,
    },
    rlInputPerMin:  120,
    rlInputPerHour: 1500,
    rlInputDaily:   15000,
    _rlInputSeeded: false,
    rlSaving: false,
    rlLoading: false,
    rlMsg: null,
    _rlRefreshTimer: null,

    // Lifecycle
    async init() {
      const gwData = await fetchJSON('api/gateways');
      if (gwData && gwData.length > 0) {
        this.gateways = gwData;
        this.selectedGw = this.gateways[0].short_id;
        await this.load();
        // fetchPopMap deferred — only called when modal is opened
      }
      await this.loadRateLimits();
      this._rlRefreshTimer = setInterval(() => this.loadRateLimits(), 30000);
    },

    destroy() {
      if (this._rlRefreshTimer) clearInterval(this._rlRefreshTimer);
      if (this._leafletMap) {
        this._leafletMap.remove();
        this._leafletMap = null;
        this._markerGroup = null;
        this._leafletInited = false;
      }
    },

    async load() {
      if (!this.selectedGw) return;
      this.metrics = null;
      // Hit the aggregated timeline endpoint to prevent restart zeroing
      const url = 'api/metrics/cloud/history?short_id=' + this.selectedGw + '&since_days=' + this.metricTimeframe;
      const data = await fetchJSON(url);
      if (data) {
        this.metrics = data;
      }
    },

    exportData(format) {
      if (!this.metrics) return;
      let content = '';
      let mimeType = '';
      let filename = `franklinwh_api_metrics_${this.selectedGw}_${new Date().toISOString().split('T')[0]}`;

      if (format === 'json') {
        content = JSON.stringify(this.metrics, null, 2);
        mimeType = 'application/json';
        filename += '.json';
      } else if (format === 'csv') {
        content = 'Type,Name,Count\n';
        if (this.metrics.methods) {
            for (const [m, c] of Object.entries(this.metrics.methods)) content += `Library Method,${m},${c}\n`;
        }
        if (this.metrics.endpoints) {
            for (const [e, c] of Object.entries(this.metrics.endpoints)) content += `Cloud Endpoint,${e},${c}\n`;
        }
        mimeType = 'text/csv';
        filename += '.csv';
      }

      const blob = new Blob([content], { type: mimeType });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    async onTimeframeChange() {
      await this.load();
    },

    async onGatewayChange() {
      await this.load();
      // Destroy Leaflet + clear cached data; modal will re-fetch on next open
      if (this._leafletMap) {
        this._leafletMap.stop();
        this._leafletMap.closePopup();
        this._leafletMap.remove();
        this._leafletMap = null;
        this._markerGroup = null;
        this._leafletInited = false;
      }
      this.popMapData = null;
      this.popMapDominant = null;
    },

    openPopMap() {
      // Sync modal period to the parent timeframe selection (Batch 1 — Item 4)
      this.popMapPeriod = this.metricTimeframe;
      this.popMapOpen = true;
      var self = this;
      if (!this.popMapData) {
        // First open (or after gateway change) — fetch and render
        this.fetchPopMap();
      } else {
        // Already have data — fix Leaflet tile size after modal becomes visible
        setTimeout(function() {
          if (self._leafletMap) self._leafletMap.invalidateSize();
        }, 80);
      }
    },

    closePopMap() {
      if (this._leafletMap) {
        this._leafletMap.stop();
        this._leafletMap.closePopup();
      }
      this.popMapOpen = false;
    },

    getTotalErrors() {
      if (!this.metrics?.errors) return 0;
      return Object.values(this.metrics.errors).reduce((a, b) => a + b, 0);
    },

    getCacheHitPct() {
      if (!this.metrics?.edge || this.metrics.edge.requests === 0) return 0;
      return Math.round((this.metrics.edge.cache_hits / this.metrics.edge.requests) * 100);
    },

    getUptimeLabel() {
      if (!this.metrics?.uptime?.uptime_seconds) return '—';
      const s = this.metrics.uptime.uptime_seconds;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return h ? h + 'h ' + m + 'm' : m + 'm';
    },

    openLogsFiltered(level, search, since_hours) {
      this.$store.app.pendingLogFilter = { level, search, since_hours: since_hours || 0 };
      this.$store.app.setTab('logs');
    },

    windowLabel(hours) {
      if (!hours) return 'Session';
      if (hours <= 24) return hours + 'h';
      return (hours / 24) + 'd';
    },

    // Computed filter helpers — independent per table (D2 fix: split methodFilter / endpointFilter)
    filteredMethods() {
      const f = this.methodFilter.toLowerCase().trim();
      const entries = Object.entries(this.metrics?.python_methods || {}).sort((a, b) => b[1] - a[1]);
      return f ? entries.filter(([k]) => k.toLowerCase().includes(f)) : entries;
    },

    filteredEndpoints() {
      const f = this.endpointFilter.toLowerCase().trim();
      const entries = Object.entries(this.metrics?.endpoints || {}).sort((a, b) => b[1] - a[1]);
      return f ? entries.filter(([k]) => k.toLowerCase().includes(f)) : entries;
    },

    // PoP Map
    async fetchPopMap() {
      if (!this.selectedGw) return;
      this.popMapLoading = true;
      // Sequence number: any render from an older fetch is discarded
      this._fetchSeq += 1;
      var mySeq = this._fetchSeq;
      var renderData = null;
      try {
        var url = 'api/metrics/edge-pops?short_id=' + this.selectedGw + '&since_days=' + this.popMapPeriod;
        var data = await fetchJSON(url);
        if (data && mySeq === this._fetchSeq) {
          this.popMapData = data;
          var counts = data.pop_counts || {};
          var dom = Object.entries(counts).sort(function(a, b) { return b[1] - a[1]; })[0];
          this.popMapDominant = dom ? dom[0] : null;
          renderData = data;
        }
      } catch (e) { /* silent */ }
      finally {
        if (mySeq === this._fetchSeq) this.popMapLoading = false;
      }
      if (renderData && mySeq === this._fetchSeq) {
        // setTimeout instead of $nextTick: guarantees browser has painted
        // the now-visible map container before Leaflet measures its size.
        var self = this;
        setTimeout(function() {
          if (mySeq === self._fetchSeq) self._renderLeafletMap(renderData, POP_COORDS);
        }, 80);
      }
    },

    _renderLeafletMap(data, coords) {
      var el = document.getElementById('cf-pop-map');
      if (!el || typeof L === 'undefined') return;

      var counts = data.pop_counts || {};
      if (Object.keys(counts).length === 0) return;

      if (!this._leafletInited) {
        // Hard world bounds prevent tile coords exceeding 2^z range (causing 400s)
        var worldBounds = [[-85, -180], [85, 180]];
        this._leafletMap = L.map('cf-pop-map', {
          zoomControl: true,
          worldCopyJump: false,
          maxBounds: worldBounds,
          maxBoundsViscosity: 1.0,   // hard stop — no panning past edge
        }).setView([-25, 133], 4);
        // See basemap.js — CARTO needs a key now, so the provider depends on
        // whether one is configured.
        L.tileLayer(window.fhaiBasemapUrl('dark'), {
          attribution: window.fhaiBasemapAttribution(),
          subdomains: 'abcd',
          maxZoom: Math.min(18, window.fhaiBasemapMaxZoom()),
          noWrap: true,              // prevents tile wrapping
          bounds:  worldBounds,      // never request tiles outside bbox → kills 400s
        }).addTo(this._leafletMap);
        // LayerGroup: clearLayers() is O(1) and releases all event handlers
        this._markerGroup = L.layerGroup().addTo(this._leafletMap);
        this._leafletInited = true;
      } else {
        // stop() cancels any in-flight fitBounds animation — without this, Leaflet
        // fires viewreset/zoom events during animation that try to reposition
        // popups against markers that clearLayers() is about to remove.
        this._leafletMap.stop();
        this._leafletMap.closePopup();
        this._markerGroup.clearLayers();
      }

      // Force layout recalculation; setTimeout(80) in fetchPopMap guarantees visible container
      this._leafletMap.invalidateSize();

      var maxCount = Math.max.apply(null, Object.values(counts).concat([1]));
      var unknownPops = [];
      var markers = [];
      var self = this;

      Object.entries(counts).forEach(function(entry) {
        var code = entry[0];
        var count = entry[1];
        var prefix = code.substring(0, 3).toUpperCase();
        var coord = coords[prefix];
        if (!coord) { unknownPops.push({ code: code, count: count }); return; }

        var isCurrent = code === data.current_pop;
        var radius = Math.round(8 + (count / maxCount) * 24);

        var marker = L.circleMarker([coord.lat, coord.lon], {
          radius: radius,
          fillColor: isCurrent ? '#f97316' : '#3b82f6',
          color:     isCurrent ? '#fed7aa' : '#93c5fd',
          weight: 2, opacity: 0.9, fillOpacity: 0.65,
        }).bindPopup(
          '<div style="font-family:monospace;font-size:11px;line-height:1.5;color:#111">' +
          '<strong style="font-size:13px">' + code + '</strong><br>' +
          coord.city + '<br>' +
          '<span style="color:#1d4ed8;font-weight:bold">' + count.toLocaleString() + ' requests</span>' +
          (isCurrent ? '<br><span style="color:#ea580c">&#9679; Current active PoP</span>' : '') +
          '</div>',
          { maxWidth: 200 }
        );
        self._markerGroup.addLayer(marker);
        markers.push(marker);
      });

      if (markers.length > 1) {
        var group = L.featureGroup(markers);
        // animate:false — eliminates zoom/viewreset event race that triggers
        // popup._updatePosition on cleared markers (_latLngToNewLayerPoint null)
        this._leafletMap.fitBounds(group.getBounds().pad(0.3), { maxZoom: 8, animate: false });
      } else if (markers.length === 1) {
        this._leafletMap.setView(markers[0].getLatLng(), 6, { animate: false });
      }

      this.popMapUnknown = unknownPops;
    },


    // Rate Limiter Guard
    async loadRateLimits() {
      this.rlLoading = true;
      try {
        const data = await fetchJSON('api/system/rate-limits');
        if (data) {
          this.rl = {
            ...data,
            limit_per_minute: data.limit_per_minute ?? 120,
            limit_per_hour:   data.limit_per_hour   ?? 1500,
            daily_budget:     data.daily_budget      ?? 15000,
          };
          if (!this._rlInputSeeded) {
            this.rlInputPerMin  = this.rl.limit_per_minute;
            this.rlInputPerHour = this.rl.limit_per_hour;
            this.rlInputDaily   = this.rl.daily_budget;
            this._rlInputSeeded = true;
          }
        }
      } catch (e) { /* silent */ }
      finally { this.rlLoading = false; }
    },

    async applyRateLimits() {
      this.rlSaving = true;
      this.rlMsg = null;
      const body = {
        calls_per_minute: this.rlInputPerMin,
        calls_per_hour:   this.rlInputPerHour,
        daily_budget:     this.rlInputDaily,
      };
      try {
        const resp = await fetch('api/system/rate-limits', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
          this.rlMsg = { ok: true, text: 'Limits applied to ' + data.gateways_updated + ' gateway(s)' };
          this._rlInputSeeded = false;
          await this.loadRateLimits();
        } else {
          this.rlMsg = { ok: false, text: data.detail || 'Failed to apply limits' };
        }
      } catch (e) {
        this.rlMsg = { ok: false, text: e.message };
      } finally {
        this.rlSaving = false;
      }
    },

    async resetRateLimits() {
      this.rlSaving = true;
      this.rlMsg = null;
      try {
        const resp = await fetch('api/system/rate-limits/reset', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
          this.rlMsg = { ok: true, text: 'Rate limits reset to defaults' };
          this._rlInputSeeded = false;
          await this.loadRateLimits();
        } else {
          this.rlMsg = { ok: false, text: data.detail || 'Reset failed' };
        }
      } catch (e) {
        this.rlMsg = { ok: false, text: e.message };
      } finally {
        this.rlSaving = false;
      }
    },
  };
}
