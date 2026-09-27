/* Smart Circuits Tab Alpine Component — v2 */
function smartCircuitsTab() {
  return {
    busy: false,
    circuits: [],

    async init() {
      this.$watch('$store.app.selectedGatewayObj', (val) => { if (val) this.refreshData(); });
      // Batch R.1 (2026-08-02): timer id captured so destroy() can clear
      // it under R.2 x-if lazy mount. The activeTab guard remains as
      // belt-and-braces for the current x-show pattern.
      this._refreshTimer = setInterval(() => {
        if (this.$store.app.activeTab === 'smart_circuits') this.refreshData();
      }, 5000);
      this.refreshData();
    },

    destroy() {
      if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; }
    },

    async refreshData() {
      if (!this.$store.app.selectedGateway) return;
      try {
        const resp = await fetch(`api/gateways/${this.$store.app.selectedGateway}/data`);
        if (!resp.ok) return;
        const gObj = await resp.json();
        const newCircuits = [];
        for (let i = 1; i <= 3; i++) {
          if (gObj[`smart_circuit_${i}_state`] !== undefined) {
            const cName   = gObj[`smart_circuit_${i}_name`]   || `Circuit ${i}`;
            const cPower  = gObj[`smart_circuit_${i}_power`]  || 0;
            const cEnergy = gObj[`smart_circuit_${i}_energy`] || 0;
            const isDefaultName = cName === 'Circuit 3' || cName === 'Circuits 3' || cName.trim() === '';
            if (i === 3 && cPower === 0 && cEnergy === 0 && isDefaultName) continue;
            newCircuits.push({
              id: i,
              name: cName,
              state: gObj[`smart_circuit_${i}_state`],
              mode:  gObj[`smart_circuit_${i}_mode`] || 'Manual',
              power:  cPower,
              energy: cEnergy,
            });
          }
        }
        this.circuits = newCircuits;
      } catch (err) {
        console.warn('Smart Circuits fetch failed', err);
      }
    },

    async sendCommand(slug, value) {
      if (!this.$store.app.selectedGateway) return;
      this.busy = true;
      try {
        const resp = await fetch(`api/gateways/${this.$store.app.selectedGateway}/control`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: slug, value: String(value) }),
        });
        let result;
        try { result = await resp.json(); } catch { result = {}; }
        if (!resp.ok || result.ok === false) {
          this.$store.app.addToast('Command failed: ' + (result.detail || result.error || 'HTTP ' + resp.status), 'error');
        } else {
          this.$store.app.addToast('Smart Circuit command dispatched', 'info');
        }
      } catch (e) {
        this.$store.app.addToast('Circuit exception: ' + e.message, 'error');
      } finally {
        this.busy = false;
        setTimeout(() => this.$store.app.refresh(), 1000);
      }
    },

    async toggleState(id, isOn) {
      await this.sendCommand(`smart_circuit_${id}`, isOn ? 'true' : 'false');
    },

    async setMode(id, modeStr) {
      await this.sendCommand(`smart_circuit_${id}_mode`, modeStr);
    },
  };
}
