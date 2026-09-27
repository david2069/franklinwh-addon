// gateways_tab.js — Alpine component for Gateways tab
function gatewaysTab() {
  return {
    get gateways() { return this.$store.app.gateways || []; },
    get groupedSites() {
      const sitesMap = {};
      const gwList = this.$store.app.gateways || [];
      for (const gw of gwList) {
        const sId = gw.site_id || 'ungrouped';
        if (!sitesMap[sId]) {
          sitesMap[sId] = {
            site_id: sId,
            site_name: gw.site_name || (sId === 'ungrouped' ? 'Ungrouped Gateways' : 'Site ' + sId),
            gateways: []
          };
        }
        sitesMap[sId].gateways.push(gw);
      }
      return Object.values(sitesMap);
    },
    loading: true,
    panelOpen: false,
    editMode: false,
    saving: false,
    showPassword: false,
    validateModal: { show: false, ok: false, error: '', warning: '', gateways: [] },
    deleteTarget: null,
    form: { full_serial: '', name: '', model: '', email: '', password: '' },

    // ── Cloud Discovery Modal ─────────────────────────────────────────────────
    discoveryModal: {
      // Sign-in step. Declared here because an undeclared property is not
      // reactive in Alpine — the form would never appear.
      needsCredentials: false,
      email: '',
      password: '',
      signingIn: false,
      open: false, loading: false, error: null,
      sites: [], selected: [], search: '',
      accountEmail: '', cached: false,
      onboarding: false, progressCurrent: 0, progressTotal: 0, progressDetail: '',
      // Step 1 picks a site, step 2 picks aGates within it.
      //
      // The site step is not cosmetic. An installer account can be linked to
      // every customer they have ever commissioned, and the previous flow listed
      // all of those sites' gateways together in one selectable list — one
      // careless "Select All" would have enrolled a stranger's battery. Scoping
      // selection to a single chosen site makes that impossible rather than
      // merely unlikely.
      step: 1,
      siteId: null,
      // Separate search boxes per step. An installer account can carry hundreds
      // of sites and thousands of gateways, so both lists need filtering — and
      // sharing one box would silently carry a site query into the gateway list.
      siteSearch: '',
      // Account classification from the login result. `known: false` means the
      // cloud told us nothing — treat that as "unsure", never as "homeowner".
      account: { is_installer: false, known: false, reasons: [] },
    },

    /** Sites filtered by name, address or id — for accounts with long lists. */
    get filteredSites() {
      const q = (this.discoveryModal.siteSearch || '').toLowerCase().trim();
      const sites = this.discoveryModal.sites || [];
      if (!q) return sites;
      return sites.filter(s =>
        (s.site_name || '').toLowerCase().includes(q) ||
        (s.address || '').toLowerCase().includes(q) ||
        String(s.site_id || '').toLowerCase().includes(q) ||
        // Also match a site by a gateway inside it, so searching a serial you
        // have on a label finds the site it belongs to.
        (s.gateways || []).some(gw =>
          (gw.serial || '').toLowerCase().includes(q) ||
          (gw.name || '').toLowerCase().includes(q)));
    },

    /** Whether to warn about enrolling gateways that may not be the user's.
     *
     * Prefers the cloud's own answer: the login endpoint serves homeowners and
     * installers alike, and the response says which this account is. When it
     * says nothing, fall back to site count — several sites on one login is
     * what an installer looks like, and also what a second home looks like,
     * which is why the fallback wording is softer.
     */
    get installerWarning() {
      const acct = this.discoveryModal.account || {};
      if (acct.is_installer) return 'confirmed';
      if (!acct.known && this.discoverySiteCount > 1) return 'suspected';
      return null;
    },

    get installerWarningReasons() {
      return (this.discoveryModal.account || {}).reasons || [];
    },

    get discoverySiteCount() {
      return (this.discoveryModal.sites || []).length;
    },

    get chosenSite() {
      const id = this.discoveryModal.siteId;
      if (id === null || id === undefined) return null;
      return (this.discoveryModal.sites || []).find(s => String(s.site_id) === String(id)) || null;
    },

    /** Gateways offered for selection — only ever those of the chosen site. */
    get discoveryGateways() {
      const site = this.chosenSite;
      if (!site) return [];
      const q = this.discoveryModal.search.toLowerCase().trim();
      const list = site.gateways || [];
      if (!q) return list;
      return list.filter(gw =>
        (gw.name || '').toLowerCase().includes(q) ||
        (gw.serial || '').toLowerCase().includes(q));
    },

    chooseSite(siteId) {
      // Changing site clears the selection: carrying serials across would let
      // gateways from a previously viewed site be onboarded silently.
      this.discoveryModal.siteId = siteId;
      this.discoveryModal.selected = [];
      this.discoveryModal.search = '';
      this.discoveryModal.step = 2;
    },

    backToSites() {
      if (this.discoveryModal.onboarding) return;
      this.discoveryModal.step = 1;
      this.discoveryModal.selected = [];
    },

    get filteredDiscoverySites() {
      const q = this.discoveryModal.search.toLowerCase().trim();
      if (!q) return this.discoveryModal.sites;
      return this.discoveryModal.sites
        .map(site => {
          const gws = site.gateways.filter(gw =>
            gw.name.toLowerCase().includes(q) ||
            gw.serial.toLowerCase().includes(q) ||
            site.site_name.toLowerCase().includes(q)
          );
          return gws.length ? { ...site, gateways: gws } : null;
        })
        .filter(Boolean);
    },

    get allVisibleSelected() {
      const visible = this.filteredDiscoverySites.flatMap(s => s.gateways).map(g => g.serial);
      return visible.length > 0 && visible.every(s => this.discoveryModal.selected.includes(s));
    },

    allSiteSelected(site) {
      return site.gateways.every(gw => this.discoveryModal.selected.includes(gw.serial));
    },

    toggleGateway(serial) {
      const idx = this.discoveryModal.selected.indexOf(serial);
      if (idx === -1) this.discoveryModal.selected.push(serial);
      else this.discoveryModal.selected.splice(idx, 1);
    },

    toggleSelectAll() {
      const visible = this.filteredDiscoverySites.flatMap(s => s.gateways).map(g => g.serial);
      if (this.allVisibleSelected) {
        this.discoveryModal.selected = this.discoveryModal.selected.filter(s => !visible.includes(s));
      } else {
        this.discoveryModal.selected = [...new Set([...this.discoveryModal.selected, ...visible])];
      }
    },

    toggleSiteAll(site) {
      const serials = site.gateways.map(g => g.serial);
      if (this.allSiteSelected(site)) {
        this.discoveryModal.selected = this.discoveryModal.selected.filter(s => !serials.includes(s));
      } else {
        this.discoveryModal.selected = [...new Set([...this.discoveryModal.selected, ...serials])];
      }
    },

    /** Sign in, then discover.
     *
     * A fresh install has no credentials, so Discover returned "Complete the
     * setup wizard first" — a dead end naming a wizard that does not exist for
     * credentials. Entering them is the first thing the add-on needs, so the
     * modal asks for them here rather than reporting their absence.
     */
    async submitDiscoveryCredentials() {
      const email = (this.discoveryModal.email || '').trim();
      const password = this.discoveryModal.password || '';
      if (!email || !password) {
        this.discoveryModal.error = 'Enter the email and password you use for the FranklinWH app.';
        return;
      }
      this.discoveryModal.signingIn = true;
      this.discoveryModal.error = null;
      try {
        const r = await fetchJSON('api/gateways/discover', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        });
        if (!r?.ok) {
          this.discoveryModal.error = r?.error || 'Sign-in failed — check the email and password.';
          return;
        }
        // Credentials are stored when a gateway is registered, so they are kept
        // here for that step rather than written before anything is chosen.
        this.discoveryModal.needsCredentials = false;
        this.discoveryModal.accountEmail = email;
        await this.loadDiscovery();
        if (this.discoveryModal.sites.length === 1) {
          this.chooseSite(this.discoveryModal.sites[0].site_id);
        }
      } catch (e) {
        this.discoveryModal.error = 'Could not reach the FranklinWH cloud.';
      } finally {
        this.discoveryModal.signingIn = false;
      }
    },

    async openDiscoveryModal() {
      this.discoveryModal.open = true;
      this.discoveryModal.needsCredentials = false;
      this.discoveryModal.email = '';
      this.discoveryModal.password = '';
      this.discoveryModal.signingIn = false;
      this.discoveryModal.selected = [];
      this.discoveryModal.search = '';
      this.discoveryModal.step = 1;
      this.discoveryModal.siteId = null;
      this.discoveryModal.siteSearch = '';
      this.discoveryModal.account = { is_installer: false, known: false, reasons: [] };
      await this.loadDiscovery();
      // One site is not a choice — skip the step rather than asking a question
      // with a single answer.
      if (this.discoveryModal.sites.length === 1) {
        this.chooseSite(this.discoveryModal.sites[0].site_id);
      }
    },

    closeDiscoveryModal() {
      if (this.discoveryModal.onboarding) return;
      Object.assign(this.discoveryModal, { open: false, sites: [], selected: [], error: null,
                                           search: '', step: 1, siteId: null, siteSearch: '',
                                           account: { is_installer: false, known: false, reasons: [] } });
    },

    async loadDiscovery() {
      this.discoveryModal.loading = true;
      this.discoveryModal.error = null;
      this.discoveryModal.sites = [];
      this.discoveryModal.needsCredentials = false;
      try {
        const resp = await fetch('api/gateways/cloud/available');
        const data = await resp.json();
        if (data.needs_credentials) {
          // Not an error — the account simply has not been signed in yet, which
          // on a fresh install is the expected state and the first thing to do.
          this.discoveryModal.needsCredentials = true;
        } else if (!data.ok) {
          this.discoveryModal.error = data.error || 'Discovery failed';
        } else {
          this.discoveryModal.sites = data.sites || [];
          this.discoveryModal.accountEmail = data.account_email || '';
          this.discoveryModal.account = data.account || { is_installer: false, known: false, reasons: [] };
          this.discoveryModal.cached = data.cached || false;
        }
      } catch (e) {
        this.discoveryModal.error = 'Network error: ' + e.message;
      } finally {
        this.discoveryModal.loading = false;
      }
    },

    async onboardSelected() {
      const selected = [...this.discoveryModal.selected];
      if (!selected.length) return;
      const gwMap = {};
      for (const site of this.discoveryModal.sites)
        for (const gw of site.gateways)
          gwMap[gw.serial] = { ...gw, site_id: site.site_id };

      this.discoveryModal.onboarding = true;
      this.discoveryModal.progressTotal = selected.length;
      this.discoveryModal.progressCurrent = 0;
      const failed = [];

      for (let i = 0; i < selected.length; i++) {
        const serial = selected[i];
        const info = gwMap[serial] || {};
        this.discoveryModal.progressCurrent = i + 1;
        this.discoveryModal.progressDetail = `Adding ${info.name || serial}…`;
        try {
          const resp = await fetch('api/gateways/linked', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ full_serial: serial, name: info.name || '', model: info.model || '', site_id: info.site_id || '' }),
          });
          if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            failed.push({ serial, error: err.detail || `HTTP ${resp.status}` });
            this.discoveryModal.progressDetail = `⚠ Failed: ${info.name || serial} — continuing…`;
          }
        } catch (e) {
          failed.push({ serial, error: e.message });
        }
        if (selected.length > 1) await new Promise(r => setTimeout(r, 500));
      }

      if (selected.length > 1) {
        try { await fetch('api/system/restart', { method: 'POST' }); } catch (_) {}
      }

      this.discoveryModal.onboarding = false;
      await this.$store.app.refresh();
      this.closeDiscoveryModal();
      if (failed.length) {
        Alpine.store('app').addToast(`${failed.length} gateway(s) failed to onboard.`, 'warn');
      } else {
        Alpine.store('app').addToast(`${selected.length} gateway(s) added successfully.`, 'success');
      }
    },

    // ── Gateway edit/add panel ───────────────────────────────────────────────
    openAdd() {
      this.editMode = false;
      this.form = { full_serial: '', name: '', model: '', email: '', password: '' };
      this.panelOpen = true;
    },

    openEdit(gw) {
      this.editMode = true;
      this.form = {
        full_serial: gw.full_serial,
        name: gw.name || '',
        model: gw.model || '',
        email: gw.email || '',
        password: '',
        short_id: gw.short_id,
        _has_credentials: gw.has_credentials || false
      };
      this.panelOpen = true;
    },

    async saveGateway() {
      this.saving = true;
      try {
        const url = this.editMode ? `api/gateways/${this.form.short_id}` : 'api/gateways/linked';
        const method = this.editMode ? 'PATCH' : 'POST';
        const resp = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.form),
        });
        const data = await resp.json();
        if (resp.ok) {
          this.panelOpen = false;
          await this.$store.app.refresh();
          Alpine.store('app').addToast(this.editMode ? 'Gateway updated.' : 'Gateway added.', 'success');
        } else {
          Alpine.store('app').addToast(data.detail || data.error || 'Save failed.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast(e.message, 'error');
      } finally {
        this.saving = false;
      }
    },

    confirmDelete(gw) {
      this.deleteTarget = gw;
    },

    async deleteGateway(shortId) {
      this.deleteTarget = null;
      try {
        const resp = await fetch(`api/gateways/${shortId}`, { method: 'DELETE' });
        if (resp.ok) {
          await this.$store.app.refresh();
          Alpine.store('app').addToast(`Gateway ${shortId} removed.`, 'success');
        } else {
          const data = await resp.json().catch(() => ({}));
          Alpine.store('app').addToast(data.detail || 'Delete failed.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast(e.message, 'error');
      }
    },

    async startGateway(shortId) {
      try {
        const resp = await fetch(`api/gateways/${shortId}/start`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          await this.$store.app.refresh();
          Alpine.store('app').addToast(`Poll loop started for gateway ${shortId}.`, 'success');
        } else {
          Alpine.store('app').addToast(data.detail || 'Start failed.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast(e.message, 'error');
      }
    },

    async stopGateway(shortId) {
      try {
        const resp = await fetch(`api/gateways/${shortId}/stop`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          await this.$store.app.refresh();
          Alpine.store('app').addToast(`Poll loop stopped for gateway ${shortId}.`, 'success');
        } else {
          Alpine.store('app').addToast(data.detail || 'Stop failed.', 'error');
        }
      } catch (e) {
        Alpine.store('app').addToast(e.message, 'error');
      }
    },

    async validateCreds(shortId) {
      try {
        const resp = await fetch(`api/gateways/${shortId}/validate`, { method: 'POST' });
        const data = await resp.json().catch(() => ({ ok: false, error: 'Network error' }));
        this.validateModal = {
          show: true,
          ok: data.ok,
          error: data.error || '',
          warning: data.warning || '',
          gateways: data.gateways || []
        };
      } catch (e) {
        Alpine.store('app').addToast(`Validation error: ${e.message}`, 'error');
      }
    },

    async refresh() {
      await this.$store.app.refresh();
    },

    async togglePoll(gw) {
      const action = gw.polling ? 'stop' : 'start';
      const resp = await fetch(`api/gateways/${gw.full_serial}/${action}`, { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (data.ok !== false) {
        await this.$store.app.refresh();
        Alpine.store('app').addToast(`Poll ${action}ed for ${gw.name || gw.full_serial}.`, 'success');
      } else {
        Alpine.store('app').addToast(data.error || 'Action failed.', 'error');
      }
    },

    statusColor(gw) {
      if (gw.polling && gw.last_poll_ok) return 'var(--ok)';
      if (gw.polling && !gw.last_poll_ok) return 'var(--danger)';
      return 'var(--text-muted)';
    },

    statusLabel(gw) {
      if (!gw.polling) return 'Stopped';
      if (gw.last_poll_ok) return 'Polling';
      return 'Error';
    },

    async initTab() {
      this.loading = true;
      await this.$store.app.refresh();
      this.loading = false;
    },
  };
}


/* Competing FranklinWH integration — see src/services/integration_conflicts.py.
 *
 * Checks Home Assistant's LOADED components, not what HACS has downloaded.
 * Those are different things: a HACS download with no config entry sits on disk
 * and polls nothing, so reporting it would be a false alarm. /api/config's
 * `components` only lists integrations HA actually set up.
 */
function integrationConflicts() {
  return {
    conflicts: [],
    confirming: false,
    confirm: '',
    busy: false,
    message: '',
    error: false,

    async check() {
      try {
        const r = await fetch('api/ha/conflicts');
        const d = await r.json();
        this.conflicts = (d && d.conflicts) || [];
      } catch (e) {
        this.conflicts = [];   // cannot look ≠ something is wrong
      }
    },

    async doDisable(domain) {
      this.busy = true;
      this.error = false;
      try {
        const r = await fetch('api/ha/conflicts/disable', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ domain, confirm: this.confirm }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'failed');
        this.message = d.detail || 'Disabled.';
        this.confirming = false;
        this.confirm = '';
        await this.check();
      } catch (e) {
        this.error = true;
        this.message = 'Could not disable it — do it from Settings → Devices & Services.';
      } finally {
        this.busy = false;
      }
    },
  };
}
