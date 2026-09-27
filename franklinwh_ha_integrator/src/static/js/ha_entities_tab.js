// ha_entities_tab.js — Alpine component for HA Entities tab
function haEntitiesTab() {
  // Maps entity slug → field in $store.app.currentGateway (gateway summary from /api/gateways)
  // Only the 5 fast-poll summary fields are available here.
  // Extended fields (daily totals, SoH, frequency, voltage) come from /api/gateways/{id}/data
  // and are not yet wired into the store — those slugs correctly show '—' for now.
  const SLUG_MAP = {
    battery_soc:                 g => g.battery_soc,
    battery_power_kw:            g => g.battery_kw,
    solar_power_kw:              g => g.solar_kw,
    grid_power_kw:               g => g.grid_kw,
    home_load_kw:                g => g.home_kw,
    // Extended fields — not in summary; shown as '—' until store is enriched
    battery_charge_today_kwh:    null,
    battery_discharge_today_kwh: null,
    solar_today_kwh:             null,
    grid_frequency_hz:           null,
    battery_soh:                 null,
    battery_temperature_c:       null,
    battery_cycles:              null,
    grid_voltage_v:              null,
    generator_power_kw:          null,
  };

  return {
    all: [],
    filtered: [],
    search: '',
    filterType: '',
    filterGroup: '',
    showControls: false,

    async init() {
      const data = await fetchJSON('api/entities');
      if (data) { this.all = data; this.applyFilter(); }
    },

    applyFilter() {
      this.filtered = this.all.filter(e => {
        if (this.showControls && !e.is_control) return false;
        if (this.filterType  && e.ha_type     !== this.filterType)  return false;
        if (this.filterGroup && e.state_group  !== this.filterGroup) return false;
        if (this.search) {
          const q = this.search.toLowerCase();
          return e.slug.includes(q) || e.name.toLowerCase().includes(q);
        }
        return true;
      });
    },

    liveValue(slug, store) {
      const gw = (store || Alpine.store('app')).currentGateway;
      if (!gw) return '—';
      const getter = SLUG_MAP[slug];
      if (!getter) return '—';  // Extended field not yet in summary store
      const v = getter(gw);
      if (v === null || v === undefined) return '—';
      return typeof v === 'number' ? (Number.isInteger(v) ? v.toFixed(1) : v.toFixed(2)) : String(v);
    },
  };
}


/* Leftover FranklinWH entities — see src/services/entity_adoption.py.
 *
 * Read-only. Adoption (renaming a registry row to inherit an old entity id, and
 * with it the recorder history keyed on that id) is deliberately not wired to a
 * button yet: it is a bulk rewrite of the user's entity registry and wants a
 * preview first.
 */
function entitySurvey() {
  return {
    loaded: false,
    busy: false,
    foreign: [],
    orphaned: [],
    note: '',

    get allLeftovers() {
      // Orphans first: their ids are actually free, so they are the ones the
      // user can do something about today.
      return [...this.orphaned, ...this.foreign];
    },

    async load() {
      this.busy = true;
      try {
        const r = await fetch('api/ha/entity-survey');
        const d = await r.json();
        // "could not look" must not render as "nothing found".
        if (d && d.checked) {
          this.foreign = d.foreign || [];
          this.orphaned = d.orphaned || [];
          this.note = d.note || '';
          this.loaded = true;
        }
      } catch (e) {
        /* leave the card hidden rather than assert an empty result */
      } finally {
        this.busy = false;
      }
    },
  };
}
