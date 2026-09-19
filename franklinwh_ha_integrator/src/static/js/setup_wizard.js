/* First-run setup wizard (GH #1).
 *
 * Three steps, one unavoidable input. It orchestrates endpoints that already
 * exist rather than adding its own checks — the gateway list, /ha/status and
 * /mqtt/status are the sources of truth, and a second opinion about whether
 * MQTT works would be a second thing to be wrong.
 */
function setupWizard() {
  return {
    open: false,
    step: 1,
    busy: false,
    error: null,
    state: { steps: {} },

    email: '',
    password: '',
    gateways: [],
    account: { is_installer: false, known: false, reasons: [] },
    selected: [],

    async init() {
      await this.refresh();
      // Shown only when it is actually needed: no gateway, and not already
      // dismissed. A returning user is never asked again.
      this.open = !this.state.complete && !this.state.dismissed;
      if (this.state.steps?.gateway?.done) this.step = 2;
    },

    get contextLabel() {
      return { ha_addon: 'Home Assistant add-on', docker: 'Docker', native: 'Native' }
        [this.state.install_context] || '';
    },

    /** The gateways discovery returned.
     *
     * This read `sites[].gateways` and got nothing, because
     * _discover_gateways returns a FLAT `gateways` list and always has — the
     * headless path in main.py reads it that way. Signing in therefore
     * succeeded and reported "no gateways are linked to this account" on an
     * account with one. Reading a key nothing produces, again.
     */
    /** Whether to warn that this login may be an installer's.
     *
     * Deliberately identical to the Gateways tab's rule (gateways_tab.js):
     * prefer the cloud's own answer, and fall back to site count only when the
     * cloud said nothing — several sites on one login is what an installer looks
     * like, and also what a second home looks like, hence the softer wording.
     */
    get installerWarning() {
      const acct = this.account || {};
      if (acct.is_installer) return 'confirmed';
      if (!acct.known && this.gatewaysBySite.length > 1) return 'suspected';
      return null;
    },

    get installerWarningReasons() {
      return (this.account || {}).reasons || [];
    },

    /** Gateways grouped by site, for an account that has more than one.
     *
     * Discovery returns a flat list where each gateway carries `site` and
     * `site_id`, so a multi-site account otherwise shows every gateway in one
     * undifferentiated list — and two sites can reasonably name a gateway the
     * same thing. Single-site accounts get no heading, because a heading over
     * the only group is noise.
     */
    get gatewaysBySite() {
      const groups = new Map();
      for (const gw of this.gateways) {
        const key = gw.site_id ?? '';
        if (!groups.has(key)) {
          groups.set(key, { site_id: key, site: gw.site || gw.address || 'Site', gateways: [] });
        }
        groups.get(key).gateways.push(gw);
      }
      return [...groups.values()];
    },

    get hasMultipleSites() {
      return this.gatewaysBySite.length > 1;
    },

    get discoveredGateways() {
      return this.gateways;
    },

    /** The three findings, in one shape, used by both step 2 and step 3. */
    get checks() {
      const s = this.state.steps || {};
      return [
        { key: 'gateway', label: 'FranklinWH account and gateway', done: !!s.gateway?.done,
          detail: s.gateway?.done ? `${s.gateway.gateway_count} registered` : 'Required' },
        { key: 'ha', label: 'Home Assistant connection', done: !!s.ha?.done,
          // The entity count is the visible proof the connection works, and it
          // tells a re-install that its previous entities are still there.
          detail: s.ha?.done
            ? [(s.ha.source || 'connected'),
               s.ha.fhai_entity_count
                 ? `${s.ha.fhai_entity_count} FranklinWH entities already in Home Assistant`
                 : null].filter(Boolean).join(' · ')
            : (s.ha?.detail || 'Optional — only service calls need it') },
        { key: 'mqtt', label: 'MQTT broker', done: !!s.mqtt?.done,
          // "Not connected" tells someone with no broker nothing they can act
          // on. Entities are published over MQTT Discovery, so without one the
          // integration runs but Home Assistant sees nothing.
          //
          // The backend now separates "no broker exists" from "a broker exists
          // and we are not on it". Telling someone already running Mosquitto to
          // install Mosquitto sent them looking for a problem they did not have.
          detail: s.mqtt?.done
            ? (s.mqtt.host || 'connected')
            : (s.mqtt?.action === 'install'
                ? 'Install the Mosquitto broker add-on — entities are published over MQTT'
                : (s.mqtt?.detail || 'Set a broker on the MQTT Admin tab')) },
      ];
    },

    async refresh() {
      this.busy = true;
      try {
        const r = await fetchJSON('api/setup/state');
        if (r?.ok) this.state = r;
      } catch (e) {
        this.error = 'Could not read setup state.';
      } finally {
        this.busy = false;
      }
    },

    async signIn() {
      const email = (this.email || '').trim();
      if (!email || !this.password) {
        this.error = 'Enter the email and password you use for the FranklinWH app.';
        return;
      }
      this.busy = true;
      this.error = null;
      try {
        const r = await fetchJSON('api/gateways/discover', {
          method: 'POST',
          body: JSON.stringify({ email, password: this.password }),
        });
        if (!r?.ok) {
          this.error = r?.error || 'Sign-in failed — check the email and password.';
          return;
        }
        this.gateways = r.gateways || [];
        // The same endpoint the Gateways tab uses also reports what the account
        // *is*. Dropping it here would make the wizard the one path into FWHAI
        // that registers a customer's gateway without ever saying so.
        this.account = r.account || { is_installer: false, known: false, reasons: [] };
        // Pre-select everything found: a household with two gateways wants both,
        // and unticking is easier than hunting for the one that was missed.
        // serial is what registration needs, and what every gateway carries.
        this.selected = this.discoveredGateways.map(g => g.serial);
        if (this.selected.length === 0) {
          this.error = 'Signed in, but no gateways are linked to this account.';
        }
      } catch (e) {
        this.error = 'Could not reach the FranklinWH cloud.';
      } finally {
        this.busy = false;
      }
    },

    async registerSelected() {
      if (!this.selected.length) return;
      this.busy = true;
      this.error = null;

      // POST /api/gateways, one call per gateway.
      //
      // Not /gateways/linked: that takes a single gateway and reads the
      // credentials already in the database, which a fresh install does not
      // have — the wizard is what puts them there. It also rejected the
      // {email, password, gateways:[...]} body invented here, since it expects
      // full_serial at the top level.
      const chosen = this.discoveredGateways.filter(g => this.selected.includes(g.serial));
      const failures = [];
      let added = 0;

      for (const gw of chosen) {
        try {
          const r = await fetchJSON('api/gateways', {
            method: 'POST',
            body: JSON.stringify({
              full_serial: gw.serial,
              name: gw.name || '',
              model: gw.model || '',
              // The API takes a string; discovery returns a number.
              site_id: String(gw.site_id ?? ''),
        // Discovery already returned these. Dropping them left the dashboard
        // with only an id to show: "Site 3447" above "SITE ID: 3447".
        site_name: gw.site || '',
        site_address: gw.address || '',
              email: (this.email || '').trim(),
              password: this.password,
              poll_interval: 30,
            }),
          });
          if (r && r.ok === false) {
            failures.push(`${gw.name || gw.serial}: ${r.error || r.detail || 'failed'}`);
          } else {
            added += 1;
          }
        } catch (e) {
          // One gateway failing must not abandon the others.
          failures.push(`${gw.name || gw.serial}: ${e.message || 'failed'}`);
        }
      }

      this.busy = false;

      if (!added) {
        this.error = failures.join('; ') || 'Could not register the gateways.';
        return;
      }
      if (failures.length) {
        this.error = `Added ${added}, but: ${failures.join('; ')}`;
      }
      // The password is not kept in the page once it has been stored.
      this.password = '';
      await this.refresh();
      this.step = 2;
    },

    async next() {
      if (this.step === 1) await this.refresh();
      this.step = Math.min(3, this.step + 1);
      if (this.step === 3) await this.refresh();
    },

    async finish() {
      this.busy = true;
      try {
        await fetchJSON('api/setup/complete', { method: 'POST', body: JSON.stringify({}) });
        this.open = false;
        // The shell was started with no gateways, so its pollers are suspended.
        // Reloading is the honest way to pick them up.
        if (this.state.steps?.gateway?.done) window.location.reload();
      } finally {
        this.busy = false;
      }
    },

    async skip() {
      // Someone who configured everything by hand should not be held here.
      try {
        await fetchJSON('api/setup/skip', { method: 'POST', body: JSON.stringify({}) });
      } catch (e) { /* dismissing must not fail */ }
      this.open = false;
    },
  };
}
