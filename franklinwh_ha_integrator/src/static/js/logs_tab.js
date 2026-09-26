/* logs_tab.js — System Logs Alpine component */
function systemLogs() {
  // Column definitions for App Logs table
  const COLS = [
    { key: 'ts',      label: 'Time',    hideable: false },
    { key: 'level',   label: 'Level',   hideable: true  },
    { key: 'source',  label: 'Source',  hideable: true  },
    { key: 'message', label: 'Message', hideable: true  },
  ];

  // Restore persisted visible cols from localStorage
  const _stored = localStorage.getItem('fwh_log_cols');
  const _hidden = _stored ? JSON.parse(_stored) : [];

  return {
    activeTab: 'app',
    loading: false, auditLoading: false, securityLoading: false,
    entries: [], auditEntries: [], securityEntries: [],
    auditSearch: '', securitySearch: '',

    // ── Paging ───────────────────────────────────────────────────────────
    // The app log used to request 5000 entries and render every one, which is
    // why this tab took seconds to appear on an instance with a real log —
    // 10k+ lines parsed server-side, serialised, and turned into DOM nodes that
    // nobody scrolled to.
    //
    // Page size follows the window because that is what decides how many rows
    // can actually be looked at; a fixed 50 wastes a tall screen and overflows
    // a short one.
    pageSize: 50,
    appOffset: 0, appMatched: 0,
    auditOffset: 0, auditMatched: 0,
    securityOffset: 0, securityMatched: 0,
    summary: { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 },
    totalEntries: 0,
    showFilters: false,
    filters: { level: '', source: '', search: '', since_hours: 0 },
    showSettings: false, saving: false,
    settings: { log_level: 'INFO', log_filename: 'franklinwh.log', log_max_mb: 10, log_backups: 5 },

    // ── Sort state ─────────────────────────────────────────
    sortKey: 'ts',
    sortAsc: false,   // default: newest first

    // ── Column visibility ───────────────────────────────────
    colDefs: COLS,
    hiddenCols: new Set(_hidden),
    showColMenu: false,

    isColVisible(key) { return !this.hiddenCols.has(key); },

    toggleCol(key) {
      if (this.hiddenCols.has(key)) {
        this.hiddenCols.delete(key);
      } else {
        this.hiddenCols.add(key);
      }
      // Force Alpine reactivity — replace Set reference
      this.hiddenCols = new Set(this.hiddenCols);
      localStorage.setItem('fwh_log_cols', JSON.stringify([...this.hiddenCols]));
    },

    cycleSort(key) {
      if (this.sortKey === key) {
        this.sortAsc = !this.sortAsc;
      } else {
        this.sortKey = key;
        this.sortAsc = key !== 'ts'; // ts defaults newest-first; others asc
      }
    },

    // Alpine-compatible computed — call as a method in x-for
    sortedEntries() {
      const key = this.sortKey;
      const asc = this.sortAsc;
      return [...this.entries].sort((a, b) => {
        const av = (a[key] || '').toString();
        const bv = (b[key] || '').toString();
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
    },

    sortIcon(key) {
      if (this.sortKey !== key) return '\u21C5';
      return this.sortAsc ? '\u2191' : '\u2193';
    },

    async init() {
      await Promise.all([this.load(), this.loadAudit()]);
      // Consume any pending deep-link filter from another tab (e.g. API Metrics card click)
      this.$watch('$store.app.activeTab', (tab) => {
        if (tab !== 'logs') return;
        const pending = this.$store.app.pendingLogFilter;
        if (!pending) return;
        if (pending.level       !== undefined) this.filters.level       = pending.level;
        if (pending.source      !== undefined) this.filters.source      = pending.source;
        if (pending.search      !== undefined) this.filters.search      = pending.search;
        if (pending.since_hours !== undefined) this.filters.since_hours = pending.since_hours;
        this.showFilters = true;
        this.$store.app.pendingLogFilter = null;  // consume once
        this.load();
      });
    },

    sinceLabel() {
      if (!this.filters.since_hours) return 'All time';
      if (this.filters.since_hours <= 24) return `Last ${this.filters.since_hours}h`;
      return `Last ${this.filters.since_hours / 24}d`;
    },

    formatDate(isoString) {
      if (!isoString) return '';
      const d = new Date(isoString + (isoString.includes('Z') || isoString.includes('+') ? '' : 'Z'));
      if (isNaN(d.valueOf())) return isoString.replace('T', ' ').substring(0, 19);
      const pad = n => n.toString().padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    },

    /** Security events — their own table, their own endpoint.
     *
     * Loaded lazily on first visit to the tab rather than with the page: most
     * visits to Logs are about application output, and this is a second query
     * against a table nobody was reading at all until now.
     */
    async loadSecurityAudit() {
      this.securityLoading = true;
      try {
        const q = (this.securitySearch || '').trim();
        const r = await fetch(`api/system/security-audit-logs?limit=${this.computePageSize()}`
                              + `&offset=${this.securityOffset}`
                              + (q ? `&search=${encodeURIComponent(q)}` : ''));
        if (r.ok) {
          const data = await r.json() || {};
          this.securityEntries = data.rows || [];
          this.securityMatched = data.matched ?? this.securityEntries.length;
        }
      } catch (err) {
        console.error('Failed to load security audit', err);
      } finally {
        this.securityLoading = false;
      }
    },

    async loadAudit() {
      this.auditLoading = true;
      try {
        const q = (this.auditSearch || '').trim();
        const r = await fetch(`api/system/audit-logs?limit=${this.computePageSize()}`
                              + `&offset=${this.auditOffset}`
                              + (q ? `&search=${encodeURIComponent(q)}` : ''));
        if (r.ok) {
          const data = await r.json() || {};
          this.auditEntries = data.rows || [];
          this.auditMatched = data.matched ?? this.auditEntries.length;
        }
      } catch (err) { console.error('Failed to load audit logs', err); }
      finally { this.auditLoading = false; }
    },

    /** Rows that fit the viewport, with a little overscroll to page into. */
    computePageSize() {
      const ROW_PX = 34;          // measured: one table row at this density
      const CHROME_PX = 420;      // header, filters, summary cards, footer
      const usable = (window.innerHeight || 800) - CHROME_PX;
      const fits = Math.floor(usable / ROW_PX);
      // Floor of 15 so a short window still shows something worth reading;
      // ceiling of 200 so a very tall one does not undo the point of paging.
      this.pageSize = Math.max(15, Math.min(200, fits + 5));
      return this.pageSize;
    },

    _pageLabel(offset, matched) {
      if (!matched) return 'No entries';
      const from = offset + 1;
      const to = Math.min(offset + this.pageSize, matched);
      return `${from.toLocaleString()}–${to.toLocaleString()} of ${matched.toLocaleString()}`;
    },

    get appPageLabel()      { return this._pageLabel(this.appOffset, this.appMatched); },
    get auditPageLabel()    { return this._pageLabel(this.auditOffset, this.auditMatched); },
    get securityPageLabel() { return this._pageLabel(this.securityOffset, this.securityMatched); },

    get appHasNext()      { return this.appOffset + this.pageSize < this.appMatched; },
    get auditHasNext()    { return this.auditOffset + this.pageSize < this.auditMatched; },
    get securityHasNext() { return this.securityOffset + this.pageSize < this.securityMatched; },

    appPage(delta)      { this.appOffset      = Math.max(0, this.appOffset      + delta * this.pageSize); return this.load(); },
    auditPage(delta)    { this.auditOffset    = Math.max(0, this.auditOffset    + delta * this.pageSize); return this.loadAudit(); },
    securityPage(delta) { this.securityOffset = Math.max(0, this.securityOffset + delta * this.pageSize); return this.loadSecurityAudit(); },

    async load() {
      this.loading = true;
      try {
        const params = new URLSearchParams();
        params.append('limit', String(this.computePageSize()));
        params.append('offset', String(this.appOffset));
        if (this.filters.level)       params.append('level',       this.filters.level);
        if (this.filters.source)      params.append('source',      this.filters.source);
        if (this.filters.search)      params.append('search',      this.filters.search);
        if (this.filters.since_hours) params.append('since_hours', this.filters.since_hours);
        // Relative, not '/api/...'. Under Home Assistant ingress the add-on is
        // served from /api/hassio_ingress/<token>/, so a leading slash leaves the
        // add-on entirely and asks Home Assistant for its own /api/logs/file.
        // That returned nothing, and the tab showed "0 app entries" on an
        // instance whose log file was filling up normally.
        const r = await fetch('api/logs/file?' + params.toString());
        if (r.ok) {
          const data = await r.json();
          this.entries = data.entries || [];
          this.summary = data.summary || { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
          this.totalEntries = Object.values(this.summary).reduce((a, b) => a + b, 0);
          this.appMatched = data.matched ?? this.entries.length;
        }
      } catch (err) {
        console.error(err);
        this.$store.app.addToast('Failed to fetch logs: ' + err.message, 'error');
      } finally { this.loading = false; }
    },

    // Back to page 1 on any filter change: a narrowed result set would
    // otherwise leave the offset past its end and render an empty page.
    applyFilters() { this.appOffset = 0; this.load(); },

    clearFilters() {
      this.filters.level = ''; this.filters.source = ''; this.filters.search = '';
      this.filters.since_hours = 0;
      this.appOffset = 0;
      this.load();
    },

    setFilterLevel(level) {
      this.filters.level = this.filters.level === level ? '' : level;
      this.showFilters = true; this.applyFilters();
    },

    async openSettings() {
      try {
        const r = await fetch('api/logs/settings');
        if (r.ok) this.settings = await r.json();
        this.showSettings = true;
      } catch (e) { this.$store.app.addToast('Could not load settings', 'error'); }
    },

    async saveSettings() {
      this.saving = true;
      try {
        const r = await fetch('api/logs/settings', {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.settings)
        });
        const d = await r.json();
        if (r.ok) {
          this.$store.app.addToast(d.message || 'Settings Saved', 'info');
          this.showSettings = false;
          setTimeout(() => this.load(), 500);
        } else {
          this.$store.app.addToast(d.detail || 'Error saving', 'error');
        }
      } catch (e) { this.$store.app.addToast(e.message, 'error'); }
      finally { this.saving = false; }
    },

    exportJSON() {
      const dl = (href, name) => { const a = Object.assign(document.createElement('a'), { href, download: name }); document.body.appendChild(a); a.click(); a.remove(); };
      dl('data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(this.entries, null, 2)), 'system_logs.json');
    },

    exportCSV() {
      if (!this.entries.length) return;
      const csv = ['ts,level,source,message', ...this.entries.map(r => [r.ts, r.level, r.source, `"${r.message.replace(/"/g, '""')}"`].join(','))].join('\n');
      const a = Object.assign(document.createElement('a'), { href: 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv), download: 'system_logs.csv' });
      document.body.appendChild(a); a.click(); a.remove();
    },
  };
}
