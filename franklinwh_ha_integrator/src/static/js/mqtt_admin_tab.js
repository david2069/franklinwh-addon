// mqtt_admin_tab.js — Alpine component for MQTT Admin tab
function mqttAdminTab() {
  return {
    status: { connected: false, client_id: '', broker: '', topics_count: 0, uptime_s: 0 },
    serviceStatus: null,         // from /api/mqtt/service-status (lifecycle state + uptime)
    topics: [],
    config: {},
    entities: [],
    filteredEntities: [],
    search: '',
    filterType: '',
    filterGroup: '',
    showControls: false,
    inspectEntity: null,
    inspectPayload: null,
    configForm: {
      mqtt_enabled: false,       // now driven from DB service state
      host: '', port: 1883,
      username: '', password: '',
      client_id: '', qos: 0,
      retain_discovery: true,
      topic_prefix: '', discovery_prefix: '',
      instance_label: 'FHAI'
    },
    reconnecting: false,
    savingConfig: false,
    configSaveResult: null,
    restartRequired: false,   // set when host/port/auth saved while service is running
    activeView: 'overview',   // 'overview' | 'entities' | 'config'

    // Test connection state
    testLoading: false,
    testResult: null,

    // Broker auto-detect
    detectResult: null,
    detecting: false,

    // Publish / Unpublish
    publishing: false,
    unpublishing: false,
    showRepublishModal: false,
    showUnpublishModal: false,
    pubResult: null,
    unpublishCountdown: 0,
    unpublishCountdownPct: 100,
    _unpublishTimer: null,

    // PIN modal
    showPinModal: false,
    pinAction: 'enable',
    pinValue: '',           // generated 6-digit PIN (shown to user)
    pinInput: '',           // what user types to confirm
    pinLoading: false,
    pinError: null,
    pinSecondsLeft: 600,
    _pinCountdown: null,

    // 24-hour session token key (server-issued, stored in localStorage)
    _tokenKey: 'fwh_mqtt_session_token',

    // Uptime tick
    _uptimeTimer: null,

    // ── Computed ──────────────────────────────────────────────────────────
    get serviceState() {
      return this.serviceStatus?.service_state ?? (this.configForm.mqtt_enabled ? 'started' : 'not_configured');
    },
    get isStarted()  { return this.serviceState === 'started'; },
    get serviceStateLabel() {
      if (this.serviceState === 'started')        return 'Started';
      if (this.serviceState === 'stopped')        return 'Stopped';
      if (this.serviceState === 'not_configured') return 'Not Configured';
      return '—';
    },
    get serviceStateBadgeClass() {
      if (this.serviceState === 'started')        return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30';
      if (this.serviceState === 'stopped')        return 'bg-amber-500/15 text-amber-400 border-amber-500/30';
      return 'bg-gray-700/50 text-gray-400 border-gray-600/50';
    },
    get uptimeStr() {
      const secs = this.serviceStatus?.uptime_seconds;
      if (secs == null) return null;
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = secs % 60;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    },
    get pinTimeLabel() {
      const m = Math.floor(this.pinSecondsLeft / 60);
      const s = this.pinSecondsLeft % 60;
      return `${m}:${String(s).padStart(2, '0')} remaining`;
    },

    // ── Lifecycle ─────────────────────────────────────────────────────────
    async init() {
      await this.refresh();
      await this.loadSystemSettings();  // seeds pollInterval once from server
      await this.loadGhostAuto();
      this._uptimeTimer = setInterval(() => {
        if (this.serviceStatus?.uptime_seconds != null) this.serviceStatus.uptime_seconds++;
      }, 1000);
      // Auto-refresh rate-limit stats every 15s — only fetches when System Controls tab is active
      this._rlRefreshTimer = setInterval(() => {
        if (this.activeView === 'system') this.loadRateLimits();
      }, 15000);
    },

    async refresh() {
      const [s, svc, t, c, e, il] = await Promise.all([
        fetchJSON('api/mqtt/status'),
        fetchJSON('api/mqtt/service-status'),
        fetchJSON('api/mqtt/topics'),
        fetchJSON('api/mqtt/config'),
        fetchJSON('api/mqtt/inspector'),
        fetchJSON('api/system/instance_label'),
      ]);
      if (s) this.status = s;
      if (svc) this.serviceStatus = svc;
      if (t) this.topics = t.topics || [];
      if (c) {
        this.config = c;
        this.configForm.mqtt_enabled    = svc?.service_state === 'started';
        this.configForm.host            = c.host || '';
        this.configForm.port            = c.port || 1883;
        this.configForm.username        = c.username || '';
        this.configForm.password        = c.password || '';
        this.configForm.client_id       = c.client_id || 'franklinwh_bridge';
        this.configForm.qos             = c.qos || 0;
        this.configForm.retain_discovery = c.retain_discovery ?? true;
        this.configForm.topic_prefix    = c.topic_prefix || '';
        this.configForm.discovery_prefix = c.discovery_prefix || '';
      }
      if (il) this.configForm.instance_label = il.instance_label || 'FHAI';
      if (e) { this.entities = e.entities || []; this.applyFilters(); }
      Alpine.store('app').mqttConnected = this.status.connected || false;
      await this.loadPrefix();
    },

    applyFilters() {
      this.filteredEntities = this.entities.filter(e => {
        if (this.showControls && !e.is_control) return false;
        if (this.filterType  && e.ha_type !== this.filterType)  return false;
        if (this.filterGroup && e.group   !== this.filterGroup)  return false;
        if (this.search) {
          const q = this.search.toLowerCase();
          return e.slug.includes(q) || e.name.toLowerCase().includes(q) || (e.topic || '').includes(q);
        }
        return true;
      });
    },

    async reconnect() {
      this.reconnecting = true;
      try {
        await fetch('api/mqtt/reconnect', { method: 'POST' });
        await new Promise(r => setTimeout(r, 2000));
        await this.refresh();
        this.restartRequired = false;   // clear banner once reconnect is confirmed
      } finally {
        this.reconnecting = false;
      }
    },

    async saveConfig() {
      this.savingConfig = true;
      this.configSaveResult = null;

      // Detect whether connection-level fields changed (require restart)
      const connChanged = (
        this.configForm.host     !== (this.config.host     || '') ||
        this.configForm.port     !== (this.config.port     || 1883) ||
        this.configForm.username !== (this.config.username || '') ||
        this.configForm.password !== ''   // non-empty password field = always treat as changed
      );

      try {
        const body = {
          client_id:        this.configForm.client_id,
          qos:              this.configForm.qos,
          retain_discovery: this.configForm.retain_discovery,
          topic_prefix:     this.configForm.topic_prefix || null,
          discovery_prefix: this.configForm.discovery_prefix || null,
        };
        // Only include connection params if they were changed
        if (this.configForm.host)     body.host     = this.configForm.host;
        if (this.configForm.port)     body.port     = this.configForm.port;
        if (this.configForm.username) body.username = this.configForm.username;
        if (this.configForm.password) body.password = this.configForm.password;

        const resp = await fetch('api/mqtt/config', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        await fetch('api/system/instance_label', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ instance_label: this.configForm.instance_label || 'FHAI' }),
        });
        this.configSaveResult = await resp.json();
        if (this.configSaveResult?.ok !== false) {
          // Flag restart required only if connection params changed while service is running
          if (connChanged && this.isStarted) this.restartRequired = true;
        }
        await this.refresh();
      } catch (e) {
        this.configSaveResult = { ok: false, error: e.message };
      } finally {
        this.savingConfig = false;
        setTimeout(() => this.configSaveResult = null, 4000);
      }
    },

    // ── Broker auto-detect ────────────────────────────────────────────────
    async detectBroker() {
      this.detecting = true;
      this.detectResult = null;
      try {
        const res = await fetchJSON('api/mqtt/detect-broker');
        this.detectResult = res;
        if (res.detected && res.host) {
          this.configForm.host = res.host;
          this.configForm.port = res.port || 1883;
        }
      } catch (e) {
        this.detectResult = { detected: false, error: 'Detection failed' };
      } finally {
        this.detecting = false;
      }
    },

    // ── Test connection ────────────────────────────────────────────────────
    async testConnection() {
      this.testLoading = true;
      this.testResult = null;
      try {
        const res = await fetchJSON('api/mqtt/test', { method: 'POST', body: '{}' });
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

      this.pinValue        = '';
      this.pinInput        = '';
      this.pinError        = null;
      this.pinLoading      = true;
      this.pinSecondsLeft  = 600;
      if (this._pinCountdown) { clearInterval(this._pinCountdown); this._pinCountdown = null; }
      this.showPinModal = true;

      // Auto-generate immediately
      try {
        const res = await fetchJSON('api/mqtt/generate-pin', { method: 'POST', body: '{}' });
        if (res?.pin) {
          this.pinValue = res.pin;
          this.pinSecondsLeft = (res.ttl_minutes || 10) * 60;
          this._startPinCountdown();
          this.$nextTick(() => {
            const el = document.getElementById('mqtt-pin-input');
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
      const endpoint = action === 'enable' ? 'api/mqtt/enable' : 'api/mqtt/disable';
      const token = this._getToken();
      try {
        const res = await fetchJSON(endpoint, {
          method: 'POST',
          body: JSON.stringify({ session_token: token }),
        });
        if (res?.ok !== false) {
          if (res?.session_token) this._setSession(res.session_token);
          await this.refresh();
        } else {
          // Server rejected token — clear it and re-prompt with PIN
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
        const endpoint = this.pinAction === 'enable' ? 'api/mqtt/enable' : 'api/mqtt/disable';
        const res = await fetchJSON(endpoint, { method: 'POST', body: JSON.stringify({ pin: this.pinInput.trim() }) });
        if (res?.ok !== false) {
          if (res?.session_token) this._setSession(res.session_token);  // store 24h server token
          this.closePinModal();
          await this.refresh();
        } else {
          this.pinError = res?.detail || 'Incorrect PIN — try again';
          this.pinInput = '';
          this.$nextTick(() => { const el = document.getElementById('mqtt-pin-input'); if (el) el.focus(); });
        }
      } catch (e) {
        this.pinError = 'Incorrect PIN or action failed';
        this.pinInput = '';
      } finally {
        this.pinLoading = false;
      }
    },

    // ── Republish / Unpublish (FEM model) ───────────────────────────────
    openRepublishModal() {
      this.showRepublishModal = true;
    },

    async executeRepublish() {
      this.showRepublishModal = false;
      this.publishing = true;
      this.pubResult = null;
      try {
        const resp = await fetch('api/mqtt/publish', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          this.pubResult = { ok: true, msg: `✓ ${data.note}` };
          this.$store.app.addToast('Republish queued — discovery will update within 30s', 'info');
        } else {
          this.pubResult = { ok: false, msg: data.detail || 'Republish failed' };
          this.$store.app.addToast('Republish failed — check broker connection', 'error');
        }
        setTimeout(() => this.refresh(), 5000);
      } catch(e) {
        this.pubResult = { ok: false, msg: e.message };
      } finally {
        this.publishing = false;
        setTimeout(() => this.pubResult = null, 8000);
      }
    },

    openUnpublishModal() {
      this.showUnpublishModal = true;
      this.unpublishCountdown = 3;
      if (this._unpublishTimer) { clearInterval(this._unpublishTimer); this._unpublishTimer = null; }

      // Drive the progress bar via CSS transition (smooth drain, like FEM)
      // Step 1: reset bar to 100% with no transition so it starts full
      this.$nextTick(() => {
        const bar = this.$refs.unpublishBar;
        if (bar) {
          bar.style.transition = 'none';
          bar.style.width = '100%';
          // Step 2: next animation frame — apply 3s linear transition and drain to 0%
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              bar.style.transition = 'width 3s linear';
              bar.style.width = '0%';
            });
          });
        }
      });

      // Tick only for the countdown number text (1s intervals)
      this._unpublishTimer = setInterval(() => {
        this.unpublishCountdown = Math.max(0, this.unpublishCountdown - 1);
        if (this.unpublishCountdown <= 0) {
          clearInterval(this._unpublishTimer);
          this._unpublishTimer = null;
        }
      }, 1000);
    },

    cancelUnpublish() {
      this.showUnpublishModal = false;
      if (this._unpublishTimer) { clearInterval(this._unpublishTimer); this._unpublishTimer = null; }
      this.unpublishCountdown = 0;
      this.unpublishCountdownPct = 100;
    },

    async executeUnpublish() {
      this.showUnpublishModal = false;
      this.unpublishing = true;
      this.pubResult = null;
      try {
        const resp = await fetch('api/mqtt/unpublish', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          this.pubResult = { ok: true, msg: `✓ ${data.note}` };
          this.$store.app.addToast('All HA entities removed. Press Republish to restore.', 'warn');
        } else {
          this.pubResult = { ok: false, msg: data.detail || 'Unpublish failed — check broker connection' };
          this.$store.app.addToast('Unpublish failed — is the MQTT broker connected?', 'error');
        }
      } catch(e) {
        this.pubResult = { ok: false, msg: e.message };
      } finally {
        this.unpublishing = false;
        setTimeout(() => this.pubResult = null, 10000);
      }
    },

    uptimeLabel(s) {
      if (!s) return '—';
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return h ? `${h}h ${m}m` : `${m}m`;
    },

    // ── Entity ID Prefix Management ────────────────────────────────────────
    //  prefixInfo     : data from GET /api/mqtt/prefix (template, examples, in_sync)
    //  prefixDraft    : what the user has typed / selected in the UI
    //  prefixScanResult : result of the live MQTT scan (found count, samples)
    //  showPrefixWarnModal / showPrefixPreviewModal : modal visibility
    //  prefixWarnChecks : 3 safety checkboxes
    //  prefixPreview  : list of {old_entity_id, new_entity_id} from dry-run
    prefixInfo: { template: '', is_default: true, examples: [], in_sync: true },
    prefixDraft: '',
    prefixScanResult: null,
    ghostDevices: [],
    ghostAuto: true,
    ghostScanning: false,
    ghostClearing: false,
    ghostScanned: false,
    ghostDetail: '',
    prefixScanLoading: false,
    showPrefixWarnModal: false,
    showPrefixPreviewModal: false,
    prefixWarnChecks: [false, false, false],
    prefixPreview: [],
    prefixMigrating: false,

    async loadPrefix() {
      const res = await fetchJSON('api/mqtt/prefix');
      if (res) {
        this.prefixInfo = res;
        if (!this.prefixDraft) this.prefixDraft = res.template || 'franklinwh_{short_id}_';
      }
    },

    async openPrefixChangeFlow() {
      if (!this.prefixDraft || this.prefixDraft === this.prefixInfo.template) return;
      this.prefixScanLoading = true;
      this.prefixScanResult = null;
      try {
        const res = await fetchJSON('api/mqtt/prefix/scan');
        this.prefixScanResult = res;
      } catch (e) {
        this.prefixScanResult = { found: 0, note: 'Scan failed: ' + e.message };
      } finally {
        this.prefixScanLoading = false;
      }
      this.prefixWarnChecks = [false, false, false];
      this.showPrefixWarnModal = true;
    },

    async openPrefixPreview() {
      // Dry-run: get OLD→NEW table without touching MQTT
      try {
        const res = await fetchJSON('api/mqtt/migrate-prefix', {
          method: 'POST',
          body: JSON.stringify({ dry_run: true, new_template: this.prefixDraft }),
        });
        this.prefixPreview = res?.preview || [];
      } catch (e) {
        this.prefixPreview = [];
      }
      this.showPrefixWarnModal = false;
      this.showPrefixPreviewModal = true;
    },

    async loadGhostAuto() {
      try {
        const res = await fetchJSON('api/mqtt/discovery/auto');
        this.ghostAuto = !!res.enabled;
      } catch (e) { /* leave the default on display */ }
    },

    async setGhostAuto(enabled) {
      const previous = this.ghostAuto;
      this.ghostAuto = enabled;
      try {
        await fetchJSON('api/mqtt/discovery/auto', {
          method: 'POST', body: JSON.stringify({ enabled })
        });
        this.$store.app.addToast(
          enabled ? 'Old devices will be removed on startup' : 'Automatic removal turned off',
          'success', { autoDismiss: true });
      } catch (e) {
        this.ghostAuto = previous;
        this.$store.app.addToast('Could not save that setting', 'error');
      }
    },

    async scanGhosts() {
      this.ghostScanning = true;
      this.ghostDetail = '';
      try {
        const res = await fetchJSON('api/mqtt/discovery/ghosts');
        this.ghostDevices = res.devices || [];
        this.ghostScanned = true;
        if (res.checked === false) this.ghostDetail = res.detail || '';
      } catch (e) {
        this.ghostDetail = e.message || 'Scan failed.';
        this.$store.app.addToast('Could not scan the broker', 'error');
      } finally {
        this.ghostScanning = false;
      }
    },

    async removeGhost(device) {
      const n = device.entity_count;
      if (!confirm(`Remove "${device.device_name}" and its ${n} entity(s)?\n\n` +
                   `This clears the retained discovery messages that recreate it. ` +
                   `Your live device is not affected. Restart Home Assistant afterwards.`)) return;
      this.ghostClearing = true;
      try {
        const res = await fetchJSON('api/mqtt/discovery/clear', {
          method: 'POST',
          body: JSON.stringify({ topics: device.topics, confirm: 'clear' })
        });
        if (res.cleared > 0) {
          this.$store.app.addToast(`Removed ${res.cleared} entity config(s) — restart Home Assistant`, 'success');
          this.ghostDevices = this.ghostDevices.filter(d => d !== device);
        } else {
          this.$store.app.addToast(res.detail || 'Nothing was removed', 'warning');
        }
      } catch (e) {
        this.$store.app.addToast(e.message || 'Removal failed', 'error');
      } finally {
        this.ghostClearing = false;
      }
    },

    async runPrefixMigration(dryRun = false) {
      this.prefixMigrating = true;
      try {
        const res = await fetchJSON('api/mqtt/migrate-prefix', {
          method: 'POST',
          body: JSON.stringify({ dry_run: dryRun, new_template: this.prefixDraft }),
        });
        if (res?.ok) {
          this.showPrefixPreviewModal = false;
          this.showPrefixWarnModal = false;
          this.$store.app.addToast(
            `Prefix migrated: ${res.tombstoned} entities removed, republish queued (≤30s)`,
            'info'
          );
          await this.loadPrefix();
        } else {
          this.$store.app.addToast('Migration failed — check broker connection', 'error');
        }
      } catch (e) {
        this.$store.app.addToast('Migration error: ' + e.message, 'error');
      } finally {
        this.prefixMigrating = false;
      }
    },

    // ── System Controls — Poll Interval ───────────────────────────────────────
    pollInterval: 30,
    _appliedPollInterval: null,   // tracks last server-confirmed value
    pollSaving: false,
    pollMsg: null,

    async applyPollInterval() {
      // No-op if value hasn't changed since last server-confirmed apply
      if (this._appliedPollInterval !== null && this.pollInterval === this._appliedPollInterval) {
        this.pollMsg = { ok: true, text: `✓ Already set to ${this.pollInterval}s — no change needed` };
        return;
      }
      this.pollSaving = true;
      this.pollMsg = null;
      try {
        const resp = await fetch('api/system/poll-interval', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ poll_interval: this.pollInterval }),
        });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
          this._appliedPollInterval = data.poll_interval;
          this.pollMsg = { ok: true, text: `✓ Poll interval set to ${data.poll_interval}s across all gateways` };
        } else {
          this.pollMsg = { ok: false, text: data.detail || 'Failed to apply poll interval' };
        }
      } catch (e) {
        this.pollMsg = { ok: false, text: e.message };
      } finally {
        this.pollSaving = false;
        // Message persists — cleared on next Apply attempt or page navigation
      }
    },

    // ── System Controls — Rate Limiter Guard ─────────────────────────────
    rl: {
      calls_last_minute: 0, calls_last_hour: 0, calls_today: 0,
      limit_per_minute: 120, limit_per_hour: 1500, daily_budget: 15000,
      remaining_daily: null, is_throttled: false,
    },
    // Editable limit inputs — top-level reactive so x-model binds correctly
    rlInputPerMin: 120,
    rlInputPerHour: 1500,
    rlInputDaily: 15000,
    _rlInputSeeded: false,   // seed inputs only once; subsequent auto-refreshes must not overwrite
    rlSaving: false,
    rlLoading: false,
    rlMsg: null,
    _rlRefreshTimer: null,

    rlRows() {
      const rl = this.rl;
      const safeLimit = (v) => (v > 0 ? v : 99999);
      return [
        {
          label: 'This minute',
          used: rl.calls_last_minute,
          limit: rl.limit_per_minute || '∞',
          pct: rl.limit_per_minute > 0 ? Math.round(rl.calls_last_minute / rl.limit_per_minute * 100) : 0,
          limitInput: rl.limit_per_minute,
          _key: 'calls_per_minute',
        },
        {
          label: 'This hour',
          used: rl.calls_last_hour,
          limit: rl.limit_per_hour || '∞',
          pct: rl.limit_per_hour > 0 ? Math.round(rl.calls_last_hour / rl.limit_per_hour * 100) : 0,
          limitInput: rl.limit_per_hour,
          _key: 'calls_per_hour',
        },
        {
          label: 'Today',
          used: rl.calls_today,
          limit: rl.daily_budget || '∞',
          pct: rl.daily_budget > 0 ? Math.round(rl.calls_today / rl.daily_budget * 100) : 0,
          limitInput: rl.daily_budget,
          _key: 'daily_budget',
        },
      ];
    },

    // Refresh usage counters only — safe for auto-refresh, never overwrites user inputs
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
          // Seed editable inputs only on first load — never overwrite user-in-progress edits
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

    // Full settings load — called ONCE at init to seed the poll interval input
    async loadSystemSettings() {
      await this.loadRateLimits();
      try {
        const pi = await fetchJSON('api/system/poll-interval');
        if (pi?.poll_interval) {
          this.pollInterval = pi.poll_interval;
          this._appliedPollInterval = pi.poll_interval;  // seed dirty-check baseline
        }
      } catch (e) { /* silent */ }
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
          this.rlMsg = { ok: true, text: `✓ Limits applied to ${data.gateways_updated} gateway(s) and persisted` };
          // Re-seed inputs from confirmed server values
          this._rlInputSeeded = false;
          await this.loadRateLimits();
        } else {
          this.rlMsg = { ok: false, text: data.detail || 'Failed to apply limits' };
        }
      } catch (e) {
        this.rlMsg = { ok: false, text: e.message };
      } finally {
        this.rlSaving = false;
        // Message persists — cleared on next Apply
      }
    },

    async resetRateLimits() {
      this.rlSaving = true;
      this.rlMsg = null;
      try {
        const resp = await fetch('api/system/rate-limits/reset', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.status === 'ok') {
          this.rlMsg = { ok: true, text: '✓ Rate limits reset to defaults (120/min, 1500/hr, 15000/day)' };
          // Re-seed editable inputs from defaults
          this._rlInputSeeded = false;
          await this.loadRateLimits();
        } else {
          this.rlMsg = { ok: false, text: data.detail || 'Reset failed' };
        }
      } catch (e) {
        this.rlMsg = { ok: false, text: e.message };
      } finally {
        this.rlSaving = false;
        // Message persists — cleared on next Apply
      }
    },
  };
}


/* Home Assistant device name — see src/routes/api_mqtt.py.
 *
 * Separate from the entity prefix and far safer: renaming a device does not
 * move its entities or their history, because entity ids follow the unique_id.
 * It is a label.
 *
 * It is a setting because it was a decision in the code, and that decision
 * changed three times in one release cycle.
 */
function deviceNameCard() {
  return {
    draft: '',
    saved: '',
    busy: false,
    message: '',
    error: false,
    presets: [
      { t: 'FranklinWH {model} {serial4}', label: 'FranklinWH aGate X-01-AU 0091' },
      { t: '{name}', label: 'FHP  (the name you gave it)' },
      { t: '{name} {model}', label: 'FHP aGate X-01-AU' },
      { t: 'FranklinWH aGate {serial4}', label: 'FranklinWH aGate 0091' },
    ],

    get preview() {
      // Rendered locally so the effect is visible before saving. The server
      // renders the same tokens against the real gateway.
      return (this.draft || '')
        .replace('{model}', 'aGate X-01-AU')
        .replace('{serial4}', '0091')
        .replace('{short_id}', '24170091')
        .replace('{serial}', '10060006A02F24170091')
        .replace('{name}', 'FHP')
        .replace(/\s+/g, ' ')
        .trim();
    },

    async load() {
      try {
        const r = await fetch('api/mqtt/device-name');
        const d = await r.json();
        this.draft = this.saved = (d && d.template) || '';
      } catch (e) { /* leave blank rather than assert a default */ }
    },

    async save() {
      this.busy = true;
      this.error = false;
      try {
        const r = await fetch('api/mqtt/device-name', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ template: this.draft }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'rejected');
        this.saved = d.template;
        this.message = d.detail || 'Saved.';
      } catch (e) {
        this.error = true;
        this.message = String(e.message || e);
      } finally {
        this.busy = false;
      }
    },
  };
}
