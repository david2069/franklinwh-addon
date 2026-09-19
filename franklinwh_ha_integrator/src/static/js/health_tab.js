/* health_tab.js — Health tab Alpine component */
function healthTab() {
  return {
    health:   {},
    process:  {},      // /api/system/processes → { metrics:{uptime_seconds,memory_mb,cpu_percent}, tasks:[] }
    pricing:  null,    // /api/pricing/current snapshot
    wal:      null,    // /api/system/db/wal-status
    backups:  null,    // /api/system/backups summary

    async init() { await this.refresh(); },

    async refresh() {
      const [health, proc, pricingResp, wal, bkp] = await Promise.all([
        fetchJSON('api/health/detail'),
        fetchJSON('api/system/processes').catch(() => null),
        fetchJSON('api/pricing/current').catch(() => null),
        fetchJSON('api/system/db/wal-status').catch(() => null),
        fetchJSON('api/system/backups').catch(() => null),
      ]);
      if (health)  this.health  = health;
      if (proc)    this.process = proc;
      // Pricing API returns {ok, data} envelope — unwrap
      if (pricingResp?.ok) this.pricing = pricingResp.data;
      if (wal)     this.wal     = wal;
      if (bkp)     this.backups = bkp;
    },

    // ── Helpers ─────────────────────────────────────────────────────────
    // NOTE: formatUptime duplicated from support_tab.js intentionally.
    // Backlog item queued 2026-04-14: extract to shared app.js utility.
    formatUptime(s) {
      if (!s && s !== 0) return '—';
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      if (h > 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
      if (h > 0)  return `${h}h ${m}m`;
      return `${m}m`;
    },

    relativeTime(isoStr) {
      if (!isoStr) return '—';
      const diffS = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
      if (diffS < 0)    return 'just now';
      if (diffS < 60)   return `${diffS}s ago`;
      if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
      if (diffS < 86400) return `${Math.floor(diffS / 3600)}h ago`;
      return `${Math.floor(diffS / 86400)}d ago`;
    },

    dbSizeColour(sizeMb) {
      if (sizeMb > 600) return 'bg-red-500';
      if (sizeMb > 300) return 'bg-amber-400';
      return 'bg-emerald-500';
    },

    dbBarWidth(sizeMb) {
      const pct = Math.min(100, (sizeMb / 800) * 100);
      return `${pct.toFixed(1)}%`;
    },

    tariffColour(tariff) {
      const t = (tariff || '').toUpperCase();
      if (t === 'PEAK')     return 'text-red-400 bg-red-500/10 border-red-500/30';
      if (t === 'OFF_PEAK') return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
      return 'text-amber-400 bg-amber-500/10 border-amber-500/30';  // SHOULDER / default
    },
  };
}
