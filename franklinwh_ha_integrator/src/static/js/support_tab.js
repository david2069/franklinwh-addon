/* support_tab.js — Admin (formerly "SysAdmin", renamed 2026-08-08 BD-06): Feature Flags, Backup, DB Browser, Deps, Processes */
function systemSupport() {
  return {
    // ── Sub-tab navigation ────────────────────────────────────────────────────
    sysTab: 'system',  // 'system' | 'features' | 'storage' | 'database' | 'environment'

    // ── Deps & Processes ─────────────────────────────────────────────────────
    dependencies: [], pythonVersion: '', loading: true,
    searchQuery: '', processMetrics: {}, tasks: [],
    refreshingProcesses: false, restartingTask: null,
    processInterval: null, showRestartModal: false, restartingApp: false,

    // ── Feature Flags ────────────────────────────────────────────────────────
    features: null, featuresLoading: true, featuresSaving: false,
    featuresEdit: {
      metrics_enabled: true, metrics_retention_days: 14,
      backup_enabled: true, backup_retention_days: 7,
      backup_interval_hours: 6, prune_vacuum_enabled: true,
      toast_auto_dismiss: true, toast_dismiss_delay_seconds: 10,
      automations_pin_enabled: true,
      suppressed_tabs: "",
      enabled_modules: []
    },

    // ── Backups ──────────────────────────────────────────────────────────────
    backups: null, backupsLoading: false, runningBackup: false, deletingBackup: null,
    restoringBackup: null,
    // Restore modal
    showRestoreModal: false, restoreTarget: null, restoring: false,
    // WAL status
    walStatus: null, walLoading: false,

    // ── DB Browser ───────────────────────────────────────────────────────────
    dbTables: [], dbSizeMb: 0, dbLoading: false,
    selectedTable: null, tableData: null, tableLoading: false,
    tableOffset: 0, tableLimit: 50,
    dbSort: 'size_bytes', dbSortDir: -1,  // default: largest table first

    // ── Prune ────────────────────────────────────────────────────────────────
    runningPrune: false, pruneResult: null, showPruneModal: false,
    prunePreview: null, prunePreviewLoading: false,


    // ── Computed ─────────────────────────────────────────────────────────────
    get filteredDeps() {
      if (!this.searchQuery) return this.dependencies;
      const q = this.searchQuery.toLowerCase();
      return this.dependencies.filter(d =>
        (d.name || '').toLowerCase().includes(q) || (d.summary || '').toLowerCase().includes(q)
      );
    },

    get sortedDbTables() {
      const key = this.dbSort;
      const dir = this.dbSortDir;
      return [...this.dbTables].sort((a, b) => {
        const av = a[key] != null ? a[key] : (key === 'name' ? '' : 0);
        const bv = b[key] != null ? b[key] : (key === 'name' ? '' : 0);
        if (typeof av === 'number' || key === 'row_count' || key === 'size_bytes') {
          return dir * ((Number(av) || 0) - (Number(bv) || 0));
        }
        return dir * String(av).localeCompare(String(bv));
      });
    },

    get metricsWarning() {
      return this.features && this.features.environment === 'ha_addon' && this.features.metrics_enabled;
    },

    // ── Formatters ───────────────────────────────────────────────────────────
    formatUptime(s) {
      if (!s) return '--';
      return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ${s%60}s`;
    },
    formatDate(iso) {
      if (!iso) return 'Never';
      try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
    },
    formatDateShort(iso) {
      if (!iso) return '—';
      try {
        const d = new Date(iso);
        return d.toLocaleDateString('en-AU', {day:'2-digit',month:'2-digit'}) + ' ' +
               d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      } catch(e) { return String(iso).substring(0, 16); }
    },
    formatBytes(b) {
      if (!b) return '—';
      if (b < 1024) return b + ' B';
      if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
      return (b / 1048576).toFixed(1) + ' MB';
    },
    envLabel(env) {
      return { docker: 'Docker Standalone', ha_addon: 'HA Add-on', dev: 'Dev / Bare Metal' }[env] || env;
    },
    envColor(env) {
      return { docker: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
               ha_addon: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
               dev: 'text-blue-400 bg-blue-500/10 border-blue-500/30' }[env] || 'text-gray-400';
    },

    // ── DB Sort helpers ───────────────────────────────────────────────────────
    setDbSort(key) {
      if (this.dbSort === key) {
        this.dbSortDir *= -1;
      } else {
        this.dbSort = key;
        this.dbSortDir = key === 'name' ? 1 : -1;
      }
    },
    sortIcon(key) {
      if (this.dbSort !== key) return 'fa-sort text-gray-700';
      return this.dbSortDir === 1 ? 'fa-sort-up text-primary-light' : 'fa-sort-down text-primary-light';
    },

    // ── Feature Flags ─────────────────────────────────────────────────────────
    async fetchFeatures() {
      this.featuresLoading = true;
      try {
        const res = await fetch('api/system/features');
        if (res.ok) {
          this.features = await res.json();
          this.featuresEdit = {
            metrics_enabled: this.features.metrics_enabled,
            metrics_retention_days: this.features.metrics_retention_days,
            backup_enabled: this.features.backup_enabled,
            backup_retention_days: this.features.backup_retention_days,
            backup_interval_hours: this.features.backup_interval_hours,
            prune_vacuum_enabled: this.features.prune_vacuum_enabled,
            toast_auto_dismiss: this.features.toast_auto_dismiss,
            toast_dismiss_delay_seconds: this.features.toast_dismiss_delay_seconds,
            automations_pin_enabled: this.features.automations_pin_enabled,
            suppressed_tabs: this.features.suppressed_tabs || "",
            enabled_modules: this.features.enabled_modules || []
          };
        }
      } catch(e) { console.error('fetchFeatures', e); }
      finally { this.featuresLoading = false; }
    },

    async saveFeatures() {
      this.featuresSaving = true;
      try {
        const res = await fetch('api/system/features', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.featuresEdit)
        });
        const data = await res.json();
        if (res.ok) {
          this.$store.app.addToast(`Saved: ${data.changed.join(', ') || 'no changes'}`, 'success');
          // Update global Alpine app store immediately so changes are reactive
          this.$store.app.automationsPinEnabled = this.featuresEdit.automations_pin_enabled;
          this.$store.app.suppressedTabs = this.featuresEdit.suppressed_tabs;
          this.$store.app.enabledModules = this.featuresEdit.enabled_modules;
          await this.fetchFeatures();
          await this.fetchBackups();
        } else {
          this.$store.app.addToast(data.detail || 'Save failed', 'error');
        }
      } catch(e) { this.$store.app.addToast('Network error saving settings', 'error'); }
      finally { this.featuresSaving = false; }
    },

    isTabChecked(tab) {
      if (!this.featuresEdit.suppressed_tabs) return false;
      return this.featuresEdit.suppressed_tabs.split(',').map(s => s.trim().toLowerCase()).includes(tab.toLowerCase());
    },

    toggleTabSuppression(tab) {
      let current = this.featuresEdit.suppressed_tabs
        ? this.featuresEdit.suppressed_tabs.split(',').map(s => s.trim().toLowerCase())
        : [];
      if (current.includes(tab.toLowerCase())) {
        current = current.filter(t => t !== tab.toLowerCase());
      } else {
        current.push(tab.toLowerCase());
      }
      this.featuresEdit.suppressed_tabs = current.filter(Boolean).join(',');

      // Derive enabled_modules from the new suppressed_tabs list to keep them in sync
      const MODULE_TAB_MAP = {
        "weather": ["weather"],
        "solar": ["solar_setup"],
        "smart_dispatch": ["smart_dispatch"],
        "dynamic_pricing": ["dynamic_pricing", "pricing"],
        "automations": ["automations"],
        "metrics": ["metrics"],
        "security": ["security"]
      };
      const suppressedSet = new Set(current);
      const enabled = [];
      for (const [mod, tabs] of Object.entries(MODULE_TAB_MAP)) {
        if (!tabs.some(t => suppressedSet.has(t.toLowerCase()))) {
          enabled.push(mod);
        }
      }
      this.featuresEdit.enabled_modules = enabled;
    },

    isModuleEnabled(mod) {
      return this.featuresEdit.enabled_modules && this.featuresEdit.enabled_modules.includes(mod);
    },

    toggleModule(mod) {
      let current = [...(this.featuresEdit.enabled_modules || [])];
      if (current.includes(mod)) {
        current = current.filter(m => m !== mod);
      } else {
        current.push(mod);
      }
      this.featuresEdit.enabled_modules = current;

      // Sync suppressed_tabs based on disabled modules
      const MODULE_TAB_MAP = {
        "weather": ["weather"],
        "solar": ["solar_setup"],
        "smart_dispatch": ["smart_dispatch"],
        "dynamic_pricing": ["dynamic_pricing", "pricing"],
        "automations": ["automations"],
        "metrics": ["metrics"],
        "security": ["security"]
      };
      const ALL_MODULES = Object.keys(MODULE_TAB_MAP);
      const disabledModules = ALL_MODULES.filter(m => !current.includes(m));
      const suppressedList = [];
      for (const m of disabledModules) {
        suppressedList.push(...MODULE_TAB_MAP[m]);
      }
      // Keep any currently suppressed tabs that are NOT optional modules (e.g. dashboard, gateways etc.)
      const nonModuleTabs = ["dashboard", "control", "smart_circuits", "battery", "schedule", "gateways", "mqtt_admin", "ha_entities", "home_automation", "power", "health", "logs", "terminal", "apimetrics"];
      const currentSuppressed = this.featuresEdit.suppressed_tabs
        ? this.featuresEdit.suppressed_tabs.split(',').map(s => s.trim().toLowerCase())
        : [];
      for (const t of currentSuppressed) {
        if (nonModuleTabs.includes(t) && !suppressedList.includes(t)) {
          suppressedList.push(t);
        }
      }
      this.featuresEdit.suppressed_tabs = suppressedList.filter(Boolean).join(',');
    },

    // ── Backups ───────────────────────────────────────────────────────────────
    async fetchBackups() {
      this.backupsLoading = true;
      try {
        const res = await fetch('api/system/backups');
        if (res.ok) this.backups = await res.json();
      } catch(e) { console.error('fetchBackups', e); }
      finally { this.backupsLoading = false; }
    },

    async runBackupNow() {
      this.runningBackup = true;
      try {
        const res = await fetch('api/system/backups/run', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          this.$store.app.addToast('Backup completed successfully', 'success');
          await this.fetchBackups();
          await this.fetchFeatures();
        } else {
          this.$store.app.addToast(data.detail || 'Backup failed', 'error');
        }
      } catch(e) { this.$store.app.addToast('Network error triggering backup', 'error'); }
      finally { this.runningBackup = false; }
    },

    async deleteBackup(filename) {
      if (!confirm(`Delete ${filename}? This cannot be undone.`)) return;
      this.deletingBackup = filename;
      try {
        const res = await fetch(`api/system/backups/${filename}`, { method: 'DELETE' });
        const data = await res.json();
        if (res.ok) {
          this.$store.app.addToast(`Deleted ${filename}`, 'success');
          await this.fetchBackups();
        } else {
          this.$store.app.addToast(data.detail || 'Delete failed', 'error');
        }
      } catch(e) { this.$store.app.addToast('Network error deleting backup', 'error'); }
      finally { this.deletingBackup = null; }
    },

    async probeBackup(arc) {
      arc.probing = true;
      try {
        const res = await fetch(`api/system/backups/${arc.filename}/probe`);
        if (res.ok) {
          arc.probe = await res.json();
          // Force Alpine reactivity by reassigning the archives array
          if (this.backups) this.backups = { ...this.backups };
        }
      } catch(e) { console.error('probeBackup', e); }
      finally { arc.probing = false; }
    },

    openRestoreModal(arc) {
      this.restoreTarget = arc;
      // Auto-probe if not done yet
      if (!arc.probe && !arc.probing) this.probeBackup(arc);
      this.showRestoreModal = true;
    },

    async confirmRestore() {
      if (!this.restoreTarget) return;
      this.restoring = true;
      const filename = this.restoreTarget.filename;
      try {
        const res = await fetch(`api/system/backups/${filename}/restore`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          this.showRestoreModal = false;
          this.$store.app.addToast(`Restoring from ${filename}… App will restart.`, 'info');
          setTimeout(() => window.location.reload(), 5000);
        } else {
          this.$store.app.addToast(data.detail || 'Restore failed', 'error');
          this.restoring = false;
        }
      } catch(e) {
        // App may have restarted — reload
        this.$store.app.addToast('Restore initiated. Reconnecting…', 'info');
        setTimeout(() => window.location.reload(), 5000);
      }
    },

    async fetchWalStatus() {
      this.walLoading = true;
      try {
        const res = await fetch('api/system/db/wal-status');
        if (res.ok) this.walStatus = await res.json();
      } catch(e) { console.error('fetchWalStatus', e); }
      finally { this.walLoading = false; }
    },



    // ── DB Browser ────────────────────────────────────────────────────────────
    async fetchDbTables() {
      this.dbLoading = true;
      try {
        const res = await fetch('api/system/db/tables');
        if (res.ok) {
          const data = await res.json();
          this.dbTables = data.tables || [];
          this.dbSizeMb = data.db_size_mb || 0;
        }
      } catch(e) { console.error('fetchDbTables', e); }
      finally { this.dbLoading = false; }
    },

    async browseTable(name) {
      this.selectedTable = name;
      this.tableOffset = 0;
      await this.fetchTablePage();
    },

    async fetchTablePage() {
      if (!this.selectedTable) return;
      this.tableLoading = true;
      try {
        const res = await fetch(`api/system/db/tables/${this.selectedTable}?limit=${this.tableLimit}&offset=${this.tableOffset}`);
        if (res.ok) this.tableData = await res.json();
      } catch(e) { console.error('fetchTablePage', e); }
      finally { this.tableLoading = false; }
    },

    tablePrev() {
      if (this.tableOffset > 0) {
        this.tableOffset = Math.max(0, this.tableOffset - this.tableLimit);
        this.fetchTablePage();
      }
    },
    tableNext() {
      if (this.tableData && this.tableOffset + this.tableLimit < this.tableData.total) {
        this.tableOffset += this.tableLimit;
        this.fetchTablePage();
      }
    },

    // ── Prune ─────────────────────────────────────────────────────────────────
    async fetchPrunePreview() {
      this.prunePreviewLoading = true;
      try {
        const res = await fetch('api/system/db/prune-preview');
        if (res.ok) this.prunePreview = await res.json();
        else this.$store.app.addToast('Prune preview failed', 'error');
      } catch(e) { this.$store.app.addToast('Network error loading preview', 'error'); }
      finally { this.prunePreviewLoading = false; }
    },

    async confirmRunPrune() {
      this.showPruneModal = false;
      this.runningPrune = true;
      this.pruneResult = null;
      this.prunePreview = null; // clear preview after real prune
      try {
        const res = await fetch('api/system/db/prune', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          this.pruneResult = data;
          const rows = Object.values(data.pruned || {}).reduce((a, b) => a + b, 0);
          this.$store.app.addToast(`Pruned ${rows} rows, reclaimed ${data.reclaimed_mb} MB`, 'success');
          await this.fetchDbTables();
          await this.fetchFeatures();
          await this.fetchBackups();
        } else {
          this.$store.app.addToast(data.detail || 'Prune failed', 'error');
        }
      } catch(e) { this.$store.app.addToast('Network error running prune', 'error'); }
      finally { this.runningPrune = false; }
    },


    // ── Processes ─────────────────────────────────────────────────────────────
    async fetchProcesses() {
      if (this.showRestartModal || this.restartingApp) return;
      this.refreshingProcesses = true;
      try {
        const res = await fetch('api/system/processes');
        const data = await res.json();
        if (res.ok) { this.processMetrics = data.metrics || {}; this.tasks = data.tasks || []; }
      } catch(err) { console.error('Failed to fetch process metrics', err); }
      finally { this.refreshingProcesses = false; }
    },

    async confirmRestartApplication() {
      this.restartingApp = true;
      try {
        const res = await fetch('api/system/restart', { method: 'POST' });
        const text = await res.json();
        if (res.ok) {
          this.$store.app.addToast(text.message, 'info');
          setTimeout(() => window.location.reload(), 4000);
        } else {
          this.$store.app.addToast('Failed to initiate restart', 'error');
          this.restartingApp = false;
        }
      } catch(err) {
        this.$store.app.addToast('Restart sequence initiated. Reconnecting...', 'info');
        setTimeout(() => window.location.reload(), 4000);
      }
    },

    async restartTask(name) {
      if (this.restartingTask) return;
      if (!confirm(`Restart '${name}'? This will disconnect active sockets.`)) return;
      this.restartingTask = name;
      try {
        const res = await fetch(`api/system/processes/${name}/restart`, { method: 'POST' });
        const text = await res.json();
        if (res.ok && text.ok) {
          this.$store.app.addToast(`Restarted ${name}`, 'info');
          await this.fetchProcesses();
        } else {
          this.$store.app.addToast(text.error || `Failed to restart ${name}`, 'error');
        }
      } catch(err) { this.$store.app.addToast(`Network error restarting ${name}`, 'error'); }
      finally { this.restartingTask = null; }
    },

    // ── Init ──────────────────────────────────────────────────────────────────
    async init() {
      await Promise.all([
        this.fetchFeatures(),
        this.fetchBackups(),
        this.fetchDbTables(),
        this.fetchWalStatus(),
        (async () => {
          try {
            const res = await fetch('api/system/dependencies');
            const data = await res.json();
            if (res.ok) { this.dependencies = data.dependencies || []; this.pythonVersion = data.python_version || 'Unknown'; }
          } catch(err) { console.error('deps', err); }
          finally { this.loading = false; }
        })()
      ]);
      this.fetchProcesses();
      this.processInterval = setInterval(() => this.fetchProcesses(), 10000);
    },


    destroy() { if (this.processInterval) clearInterval(this.processInterval); }
  };
}
