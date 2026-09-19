// home_automation_tab.js — Alpine component for Home Automation tab
function homeAutomationTab() {
  return {
    // ── Sub-tab ──────────────────────────────────────────────────────────────
    activeView: 'status',  // 'status' | 'config'

    // ── Status & Browser state ────────────────────────────────────────────
    status: null,
    statusLoading: true,
    statusError: null,
    _uptimeTimer: null,

    entities: [],
    totalEntities: 0,
    totalPages: 1,
    currentPage: 1,
    pageSize: 50,
    entitiesLoading: false,
    entitiesError: null,
    filterDomain: '',
    filterSearch: '',
    fhaiOnly: false,
    _allDomains: [],  // populated after each entity load

    // ── Legacy Config tab state ──────────────────────────────────────────
    configData: null,
    configLoading: false,
    configForm: { ha_host: '', ha_token: '', fhai_host: '' },
    savingConfig: false,
    configSaveResult: null,
    showToken: false,
    testLoading: false,
    testResult: null,

    // ── Phase 2: Multi-HA & Multi-Device state ────────────────────────────
    haInstances: [],
    haInstancesLoading: false,
    notificationDevices: [],
    notificationDevicesLoading: false,
    usersList: [],

    // Modals & Forms
    showInstanceModal: false,
    instanceModalMode: 'add', // 'add' | 'edit'
    instanceForm: { id: null, alias: '', host: '', token: '', enabled: true, is_default: false },
    savingInstance: false,
    instanceError: null,
    instanceSuccess: null,
    instanceTestResult: null,
    instanceTesting: false,

    showDeviceModal: false,
    deviceModalMode: 'add', // 'add' | 'edit'
    deviceForm: { id: null, ha_instance_id: '', alias: '', service_target: '', enabled: true, owner_username: '' },
    savingDevice: false,
    deviceError: null,
    deviceSuccess: null,
    
    // Discovered services list for selected HA instance
    discoveredTargets: [],
    discoveryLoading: false,
    discoveryError: null,

    // Unified E2E Test State
    unifiedTestLoading: false,
    unifiedTestResult: null,

    // ── PIN modal state ──────────────────────────────────────────────────
    showPinModal: false,
    pinAction: 'enable',
    pinValue: '',           // generated PIN displayed to user (string)
    pinInput: '',           // what user types
    pinLoading: false,      // generating or submitting
    pinError: null,
    pinSecondsLeft: 600,
    _pinCountdown: null,

    // 24-hour session token key (server-issued, stored in localStorage)
    _tokenKey: 'fwh_ha_session_token',

    // ── Computed helpers ─────────────────────────────────────────────────
    get isConnected()  { return this.status?.connected === true; },
    get isStarted()    { return (this.status?.service_state ?? this.configData?.service_state) === 'started'; },
    get serviceState() { return this.status?.service_state ?? this.configData?.service_state ?? 'not_configured'; },
    get serviceStateLabel() {
      const s = this.serviceState;
      if (s === 'started')        return 'Started';
      if (s === 'stopped')        return 'Stopped';
      if (s === 'not_configured') return 'Not Configured';
      return '—';
    },
    get serviceStateBadgeClass() {
      const s = this.serviceState;
      if (s === 'started')        return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30 border';
      if (s === 'stopped')        return 'bg-amber-500/15 text-amber-400 border-amber-500/30 border';
      return 'bg-gray-700/50 text-gray-400 border-gray-600/50 border';
    },
    get uptimeStr() {
      const secs = this.status?.uptime_seconds ?? this.configData?.uptime_seconds;
      return secs != null ? this._formatUptime(secs) : null;
    },
    get pageStart() { return (this.currentPage - 1) * this.pageSize + 1; },
    get pageEnd()   { return Math.min(this.currentPage * this.pageSize, this.totalEntities); },
    get availableDomains() { return this._allDomains; },
    get pinTimeLabel() {
      const m = Math.floor(this.pinSecondsLeft / 60);
      const s = this.pinSecondsLeft % 60;
      return `${m}:${String(s).padStart(2, '0')} remaining`;
    },

    // ── Lifecycle ────────────────────────────────────────────────────────
    async init() {
      await Promise.all([
        this.loadStatus(),
        this.loadEntities(1),
        this.loadConfig()
      ]);
      this._startUptimeTick();
    },

    _startUptimeTick() {
      this._uptimeTimer = setInterval(() => {
        if (this.status?.uptime_seconds != null) this.status.uptime_seconds++;
        if (this.configData?.uptime_seconds != null) this.configData.uptime_seconds++;
      }, 1000);
    },

    _formatUptime(secs) {
      if (secs < 60)   return secs + 's';
      if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      return h + 'h ' + m + 'm';
    },

    // ── Status & Browser ─────────────────────────────────────────────────
    async loadStatus() {
      this.statusLoading = true;
      this.statusError = null;
      try {
        const data = await fetchJSON('api/ha/status');
        this.status = data;
      } catch (e) {
        this.statusError = 'Failed to reach /api/ha/status';
      } finally {
        this.statusLoading = false;
      }
    },

    async loadEntities(page = 1) {
      this.entitiesLoading = true;
      this.entitiesError = null;
      this.currentPage = page;
      const params = new URLSearchParams({
        page,
        limit: this.pageSize,
        ...(this.filterDomain && { domain: this.filterDomain }),
        ...(this.filterSearch && { search: this.filterSearch }),
      });
      try {
        const data = await fetchJSON(`api/ha/entities?${params}`);
        if (data) {
          this.entities      = data.entities ?? [];
          this.totalEntities = data.total    ?? 0;
          this.totalPages    = data.pages    ?? 1;
          
          if (data.domains && data.domains.length) {
            this._allDomains = data.domains;
          } else if (!this.filterDomain && !this.filterSearch) {
            const seen = new Set(this.entities.map(e => e.domain).filter(Boolean));
            this._allDomains.forEach(d => seen.add(d));
            this._allDomains = [...seen].sort();
          }
        }
      } catch (e) {
        this.entitiesError = 'Failed to reach /api/ha/entities';
        this.entities = [];
      } finally {
        this.entitiesLoading = false;
      }
    },

    prevPage() { if (this.currentPage > 1) this.loadEntities(this.currentPage - 1); },
    nextPage() { if (this.currentPage < this.totalPages) this.loadEntities(this.currentPage + 1); },
    onFilterChange() { this.loadEntities(1); },

    stateClass(entity) {
      const s = entity.state?.toLowerCase();
      if (s === 'on' || s === 'online' || s === 'above_horizon') return 'text-emerald-400 font-bold';
      if (s === 'off' || s === 'offline' || s === 'unavailable') return 'text-gray-500';
      if (!isNaN(parseFloat(s))) return 'text-blue-300';
      return 'text-gray-300';
    },

    formatState(entity) {
      const s = entity.state;
      const u = entity.unit;
      if (s === 'unavailable') return '—';
      if (u) return `${s} ${u}`;
      return s;
    },

    // ── Phase 2: Multi-HA & Multi-Device CRUD API Resolution ──────────────
    async loadConfig() {
      this.configLoading = true;
      try {
        const data = await fetchJSON('api/ha/config');
        this.configData = data;
        this.configForm.ha_host = data.ha_host || '';
        this.configForm.ha_token = '';
        this.configForm.fhai_host = data.fhai_host || '';
        if (this.status) {
          this.status.service_state = data.service_state;
          this.status.uptime_seconds = data.uptime_seconds;
        }

        // Parallel load of HA instances, Devices, and Users list
        await Promise.all([
          this.loadHAInstances(),
          this.loadNotificationDevices(),
          this.loadUsersList()
        ]);
      } catch (e) {
        console.warn('Failed to load HA config:', e);
      } finally {
        this.configLoading = false;
      }
    },

    async loadHAInstances() {
      this.haInstancesLoading = true;
      try {
        const res = await fetchJSON('api/ha/instances');
        if (res && res.instances) {
          this.haInstances = res.instances;
        }
      } catch (e) {
        console.error('Failed to load HA instances:', e);
      } finally {
        this.haInstancesLoading = false;
      }
    },

    async loadNotificationDevices() {
      this.notificationDevicesLoading = true;
      try {
        const res = await fetchJSON('api/ha/devices');
        if (res && res.devices) {
          this.notificationDevices = res.devices;
        }
      } catch (e) {
        console.error('Failed to load notification devices:', e);
      } finally {
        this.notificationDevicesLoading = false;
      }
    },

    async loadUsersList() {
      try {
        const res = await fetchJSON('api/security/users');
        if (Array.isArray(res)) {
          this.usersList = res;
        } else if (res && res.users) {
          this.usersList = res.users;
        }
      } catch (e) {
        console.warn('Failed to load user profiles for device routing:', e);
      }
    },

    // HA Instance Modal Controls
    openInstanceModal(inst = null) {
      this.instanceError = null;
      this.instanceSuccess = null;
      this.instanceTestResult = null;
      
      if (inst) {
        this.instanceModalMode = 'edit';
        this.instanceForm = {
          id: inst.id,
          alias: inst.alias,
          host: inst.host,
          token: '********', // masked token
          enabled: inst.enabled === 1 || inst.enabled === true,
          is_default: inst.is_default === 1 || inst.is_default === true
        };
      } else {
        this.instanceModalMode = 'add';
        this.instanceForm = {
          id: null,
          alias: '',
          host: '',
          token: '',
          enabled: true,
          is_default: false
        };
      }
      this.showInstanceModal = true;
    },

    closeInstanceModal() {
      this.showInstanceModal = false;
    },

    async testInstanceForm() {
      this.instanceTesting = true;
      this.instanceTestResult = null;
      this.instanceError = null;
      try {
        // Use temporary validation check
        const body = {
          id: this.instanceForm.id,
          alias: this.instanceForm.alias,
          host: this.instanceForm.host,
          token: this.instanceForm.token
        };
        const res = await fetchJSON('api/ha/instances', {
          method: 'POST',
          body: JSON.stringify(body)
        });
        
        if (res && res.ok) {
          this.instanceTestResult = { ok: true, message: res.message };
        } else {
          this.instanceTestResult = { ok: false, error: res?.detail || 'Connection test failed.' };
        }
      } catch (e) {
        this.instanceTestResult = { ok: false, error: e.message || 'Connection failed.' };
      } finally {
        this.instanceTesting = false;
      }
    },

    async saveHAInstance() {
      this.savingInstance = true;
      this.instanceError = null;
      this.instanceSuccess = null;
      try {
        const body = {
          id: this.instanceForm.id,
          alias: this.instanceForm.alias,
          host: this.instanceForm.host,
          token: this.instanceForm.token,
          enabled: this.instanceForm.enabled,
          is_default: this.instanceForm.is_default
        };
        const res = await fetchJSON('api/ha/instances', {
          method: 'POST',
          body: JSON.stringify(body)
        });
        if (res && res.ok) {
          this.instanceSuccess = res.message;
          await this.loadHAInstances();
          await this.loadNotificationDevices(); // refresh in case of cascade changes
          setTimeout(() => {
            this.closeInstanceModal();
          }, 1000);
        } else {
          this.instanceError = res?.detail || 'Failed to save HA instance.';
        }
      } catch (e) {
        this.instanceError = e.message || 'Error occurred while saving.';
      } finally {
        this.savingInstance = false;
      }
    },

    async deleteHAInstance(id) {
      if (!confirm('Are you sure you want to delete this HA Instance? All mapped companion devices will be removed too.')) {
        return;
      }
      try {
        const res = await fetchJSON(`api/ha/instances/${id}`, { method: 'DELETE' });
        if (res && res.ok) {
          this.$store.app.addToast('Home Assistant instance deleted', 'success', { autoDismiss: true });
          await this.loadHAInstances();
          await this.loadNotificationDevices();
        } else {
          this.$store.app.addToast(res?.detail || 'Failed to delete instance', 'error');
        }
      } catch (e) {
        this.$store.app.addToast('Error deleting instance: ' + e.message, 'error');
      }
    },

    // Device Modal Controls
    openDeviceModal(dev = null) {
      this.deviceError = null;
      this.deviceSuccess = null;
      this.discoveredTargets = [];
      this.discoveryError = null;

      if (dev) {
        this.deviceModalMode = 'edit';
        this.deviceForm = {
          id: dev.id,
          ha_instance_id: dev.ha_instance_id,
          alias: dev.alias,
          service_target: dev.service_target,
          enabled: dev.enabled === 1 || dev.enabled === true,
          owner_username: dev.owner_username || ''
        };
        this.loadDiscoveredTargets(dev.ha_instance_id);
      } else {
        this.deviceModalMode = 'add';
        // Auto-select first active instance if available
        const defaultInst = this.haInstances.find(i => i.is_default) || this.haInstances[0];
        this.deviceForm = {
          id: null,
          ha_instance_id: defaultInst ? defaultInst.id : '',
          alias: '',
          service_target: '',
          enabled: true,
          owner_username: ''
        };
        if (defaultInst) {
          this.loadDiscoveredTargets(defaultInst.id);
        }
      }
      this.showDeviceModal = true;
    },

    closeDeviceModal() {
      this.showDeviceModal = false;
    },

    async onDeviceInstanceChange() {
      this.deviceForm.service_target = '';
      if (this.deviceForm.ha_instance_id) {
        await this.loadDiscoveredTargets(this.deviceForm.ha_instance_id);
      } else {
        this.discoveredTargets = [];
      }
    },

    async loadDiscoveredTargets(instanceId) {
      this.discoveryLoading = true;
      this.discoveryError = null;
      this.discoveredTargets = [];
      try {
        const res = await fetchJSON(`api/ha/instances/${instanceId}/targets`);
        if (res && res.ok) {
          this.discoveredTargets = res.targets || [];
        } else {
          this.discoveryError = res?.error || 'Failed to fetch notify services from Home Assistant.';
        }
      } catch (e) {
        this.discoveryError = e.message || 'Connection error to local integrations proxy.';
      } finally {
        this.discoveryLoading = false;
      }
    },

    async saveNotificationDevice() {
      this.savingDevice = true;
      this.deviceError = null;
      this.deviceSuccess = null;
      try {
        const payload = { ...this.deviceForm };
        if (!payload.owner_username) payload.owner_username = null;
        const res = await fetchJSON('api/ha/devices', {
          method: 'POST',
          body: JSON.stringify(payload)
        });
        if (res && res.ok) {
          this.deviceSuccess = res.message;
          await this.loadNotificationDevices();
          setTimeout(() => {
            this.closeDeviceModal();
          }, 1000);
        } else {
          this.deviceError = res?.detail || 'Failed to save companion device.';
        }
      } catch (e) {
        this.deviceError = e.message || 'Error occurred while saving device.';
      } finally {
        this.savingDevice = false;
      }
    },

    async deleteNotificationDevice(id) {
      if (!confirm('Remove this device notification route?')) {
        return;
      }
      try {
        const res = await fetchJSON(`api/ha/devices/${id}`, { method: 'DELETE' });
        if (res && res.ok) {
          this.$store.app.addToast('Device route removed successfully', 'success', { autoDismiss: true });
          await this.loadNotificationDevices();
        } else {
          this.$store.app.addToast(res?.detail || 'Failed to remove device route', 'error');
        }
      } catch (e) {
        this.$store.app.addToast('Error removing device route: ' + e.message, 'error');
      }
    },

    // Unified E2E Test Dispatcher
    async triggerUnifiedTest(eventType = 'test') {
      this.unifiedTestLoading = true;
      this.unifiedTestResult = null;
      try {
        const res = await fetchJSON('api/automation/notifications/test', {
          method: 'POST',
          body: JSON.stringify({ event_type: eventType })
        });
        const data = res.result || res;
        this.unifiedTestResult = { ok: true, data };
        // A 200 here means the endpoint ran, not that a phone was reached. It
        // reported "dispatched successfully" while every configured device was
        // silently skipped and only the legacy fallback was tried. Count what
        // actually landed.
        const rows = Array.isArray(data.results) ? data.results : [];
        const ok   = rows.filter(r => r && r.ok).length;
        const bad  = rows.length - ok;
        if (rows.length && !ok) {
          this.$store.app.addToast(`Push test reached no devices (${bad} failed)`, 'error');
        } else if (bad) {
          this.$store.app.addToast(`Push test sent to ${ok} device(s), ${bad} failed`, 'warning');
        } else {
          if (!rows.length) {
            // No per-device results means nothing was dispatched. Reporting
            // "sent to 1 device" here is how the previous fix still lied.
            this.$store.app.addToast('Push test dispatched, but no device results were returned — check the add-on log', 'warning');
          } else {
            this.$store.app.addToast(`Push test sent to ${ok} device(s)`, 'success', { autoDismiss: true });
          }
        }
      } catch (e) {
        this.unifiedTestResult = { ok: false, error: e.message || 'Test dispatch failed.' };
        this.$store.app.addToast('Unified push test failed to trigger', 'error');
      } finally {
        this.unifiedTestLoading = false;
      }
    },

    // ── Legacy Form Fallbacks ─────────────────────────────────────────────
    async saveConfig() {
      this.savingConfig = true;
      this.configSaveResult = null;
      try {
        const body = {};
        if (this.configForm.ha_host.trim())  body.ha_host  = this.configForm.ha_host.trim();
        if (this.configForm.ha_token.trim()) body.ha_token = this.configForm.ha_token.trim();
        if (this.configForm.fhai_host.trim()) body.fhai_host = this.configForm.fhai_host.trim();
        const res = await fetchJSON('api/ha/config', { method: 'POST', body: JSON.stringify(body) });
        this.configSaveResult = res;
        if (res?.ok !== false) {
          await this.loadConfig();
          this.configForm.ha_token = '';
        }
      } catch (e) {
        this.configSaveResult = { ok: false, error: e.message };
      } finally {
        this.savingConfig = false;
        setTimeout(() => this.configSaveResult = null, 5000);
      }
    },

    async testConnection() {
      this.testLoading = true;
      this.testResult = null;
      try {
        const res = await fetchJSON('api/ha/test', { method: 'POST', body: '{}' });
        this.testResult = res;
      } catch (e) {
        this.testResult = { ok: false, error: e.message };
      } finally {
        this.testLoading = false;
      }
    },

    // ── PIN Modal — Automations-style 24h session ─────────────────────────
    _sessionValid() {
      return !!localStorage.getItem(this._tokenKey);
    },
    _setSession(token) {
      if (token) localStorage.setItem(this._tokenKey, token);
    },
    _clearSession() {
      localStorage.removeItem(this._tokenKey);
    },
    _getToken() {
      return localStorage.getItem(this._tokenKey) || null;
    },

    async openPinModal(action) {
      this.pinAction = action;

      // Skip PIN modal if session still valid — call action directly
      if (this._sessionValid()) {
        await this._executeAction(action);
        return;
      }

      this.pinValue    = '';
      this.pinInput    = '';
      this.pinError    = null;
      this.pinLoading  = true;
      this.pinSecondsLeft = 600;
      if (this._pinCountdown) { clearInterval(this._pinCountdown); this._pinCountdown = null; }
      this.showPinModal = true;

      // Auto-generate PIN immediately on open
      try {
        const res = await fetchJSON('api/ha/generate-pin', { method: 'POST', body: '{}' });
        if (res?.pin) {
          this.pinValue = res.pin;
          this.pinSecondsLeft = (res.ttl_minutes || 10) * 60;
          this._startPinCountdown();
          this.$nextTick(() => {
            const el = document.getElementById('ha-pin-input');
            if (el) el.focus();
          });
        } else {
          this.pinError = 'Failed to generate PIN — try again';
        }
      } catch (e) {
        this.pinError = 'Server error generating PIN — try again';
      } finally {
        this.pinLoading = false;
      }
    },

    closePinModal() {
      this.showPinModal = false;
      if (this._pinCountdown) { clearInterval(this._pinCountdown); this._pinCountdown = null; }
      this.pinValue = '';
      this.pinInput = '';
      this.pinError = null;
    },

    _startPinCountdown() {
      this._pinCountdown = setInterval(() => {
        if (this.pinSecondsLeft > 0) {
          this.pinSecondsLeft--;
        } else {
          clearInterval(this._pinCountdown);
          this.pinError = 'PIN expired — close and try again';
          this.pinValue = '';
        }
      }, 1000);
    },

    async _executeAction(action) {
      const endpoint = action === 'enable' ? 'api/ha/enable' : 'api/ha/disable';
      const token = this._getToken();
      try {
        const res = await fetchJSON(endpoint, {
          method: 'POST',
          body: JSON.stringify({ session_token: token }),
        });
        if (res?.ok !== false) {
          if (res?.session_token) this._setSession(res.session_token);
          await this.loadStatus();
          await this.loadConfig();
        } else {
          this._clearSession();
          await this.openPinModal(action);
        }
      } catch (e) {
        this._clearSession();
        this.$store.app.addToast('Action failed — please retry', 'error');
      }
    },

    async submitPin() {
      if (this.pinLoading || !this.pinInput.trim()) return;
      this.pinLoading = true;
      this.pinError = null;
      try {
        const endpoint = this.pinAction === 'enable' ? 'api/ha/enable' : 'api/ha/disable';
        const res = await fetchJSON(endpoint, { method: 'POST', body: JSON.stringify({ pin: this.pinInput.trim() }) });
        if (res?.ok !== false) {
          if (res?.session_token) this._setSession(res.session_token);  // store 24h server token
          this.closePinModal();
          await this.loadStatus();
          await this.loadConfig();
        } else {
          this.pinError = res?.detail || 'Incorrect PIN — try again';
          this.pinInput = '';
          this.$nextTick(() => { const el = document.getElementById('ha-pin-input'); if (el) el.focus(); });
        }
      } catch (e) {
        this.pinError = 'Incorrect PIN or action failed';
        this.pinInput = '';
      } finally {
        this.pinLoading = false;
      }
    },
  };
}
