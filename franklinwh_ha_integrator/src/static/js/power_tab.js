// power_tab.js — Alpine component for Power / Hardware tab
function powerTab() {
  return {
    data: null,
    profile: null,
    livePower: null,
    loading: false,
    
    // Grid compliance state
    complianceList: [],
    complianceDetails: [],
    activeProfileName: '',
    loadingCompliance: false,
    showTechnical: false,

    // Islanding modal state
    showOffGridModal: false,
    ackOffGridRisk: false,
    busy: false,
    pendingTargetState: null,
    pendingIsGridConnected: false,  // legacy compat: true = currently connected, going off-grid
    pendingGridState: 'Connected',  // DEF-GRID-STATE-ENUM: full GridConnectionState string
    reconnectSoc: 5,
    pollTimer: null,

    // Outage dwell timer — prevents premature 'check your panel' messages during aGate safety sequencing
    outageFirstSeen: null,     // ms timestamp when Outage state was first detected
    outageIsConfirmed: false,  // true only after OUTAGE_DWELL_MS has elapsed
    OUTAGE_DWELL_MS: 120000,   // 2 minutes: aGate stability monitoring window

    // Command pending guard — prevents re-clicking while aGate processes the islanding command
    gridPending: false,
    gridCommandPendingSince: null,
    GRID_COMMAND_TIMEOUT_MS: 180000, // 3 min safety timeout to auto-clear pending


    // Live Polling State
    livePollActive: false,
    liveSampleCount: 0,
    liveMaxSamples: 10,
    liveIntervalSeconds: 30,
    liveNextFetchCountdown: 0,
    liveIntervalTimer: null,
    countdownTimer: null,

    async init() {
      if (this.$store.app.selectedGateway) {
        await this.fetchProfile();
        this.load();
      }
      this.$watch('$store.app.selectedGateway', async () => {
        this.data = null;
        this.livePower = null;
        this.profile = null;
        this.stopLivePoll();
        await this.fetchProfile();
        this.load();
      });

      // Background Alpine refresh loop every 10 seconds
      this.pollTimer = setInterval(() => {
          if (this.$store.app.selectedGateway && !this.loading && !this.showOffGridModal && !this.showTechnical) {
              this.loadSilent();
          }
      }, 10000);
    },

    destroy() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      if (this.liveIntervalTimer) clearInterval(this.liveIntervalTimer);
      if (this.countdownTimer) clearInterval(this.countdownTimer);
    },

    // DEV-ONLY: Override grid state for T3 UI testing without real hardware.
    // Usage from browser console: Alpine.$data(document.querySelector('[x-data]')).setTestGridState('NotGridTied')
    // Valid values: 'Connected' | 'SimulatedOffGrid' | 'Outage' | 'NotGridTied'
    setTestGridState(state) {
      const valid = ['Connected', 'SimulatedOffGrid', 'Outage', 'NotGridTied'];
      if (!valid.includes(state)) { console.warn('[DEV] Invalid state. Use:', valid.join(' | ')); return; }
      this.data = { ...this.data, grid_status: state };
      console.info('[DEV] grid_status overridden to:', state);
    },

    async fetchProfile() {
      const g = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway);
      if (g && g.profile) this.profile = g.profile;
    },

    async load() {
      if (!this.$store.app.selectedGateway) return;
      this.loading = true;
      await this.loadSilent();
      await this.fetchLivePower(); // Do one initial fetch of live relays
      this.fetchCompliance();
      this.loading = false;
    },

    async loadSilent() {
      if (!this.$store.app.selectedGateway) return;
      try {
         // STANDARD FAST-POLL: Only hit the local cache /data endpoint! Zero Cloud API impact.
         const response = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway + '/data');
         if (response) {
             const prevStatus = this.data?.grid_status;
             // Merge carefully to preserve extended data already gathered
             this.data = { ...this.data, ...response };
             this.isRefreshing = false;

             // Outage dwell tracking — only confirm after OUTAGE_DWELL_MS to avoid premature messaging
             const now = Date.now();
             if (response.grid_status === 'Outage') {
                 if (!this.outageFirstSeen) this.outageFirstSeen = now;
                 this.outageIsConfirmed = (now - this.outageFirstSeen) >= this.OUTAGE_DWELL_MS;
             } else {
                 this.outageFirstSeen = null;
                 this.outageIsConfirmed = false;
             }

             // Pending guard: clear if state changed or safety timeout elapsed
             if (this.gridPending) {
                 const stateChanged = response.grid_status !== prevStatus;
                 const timedOut = this.gridCommandPendingSince &&
                                  (now - this.gridCommandPendingSince) > this.GRID_COMMAND_TIMEOUT_MS;
                 if (stateChanged || timedOut) {
                     this.gridPending = false;
                     this.gridCommandPendingSince = null;
                 }
             }
         }
      } catch (error) {
          console.error("Failed to silently refresh telemetry", error);
      }
    },

    async fetchLivePower() {
      if (!this.$store.app.selectedGateway) return;
      try {
         const response = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway + '/telemetry/live_power');
         if (response) {
             this.livePower = response;
         }
      } catch (error) {
          console.error("Failed to fetch extended metrics", error);
      }
    },
    
    startLivePoll() {
      if (this.livePollActive) return;
      this.livePollActive = true;
      this.liveSampleCount = 0;
      this.tickLivePoll();
      
      this.countdownTimer = setInterval(() => {
         if (this.liveNextFetchCountdown > 0) this.liveNextFetchCountdown--;
      }, 1000);
    },
    
    async tickLivePoll() {
      if (!this.livePollActive) return;
      
      this.liveSampleCount++;
      await this.fetchLivePower();
      
      if (this.liveSampleCount >= this.liveMaxSamples) {
         this.stopLivePoll();
         return;
      }
      
      this.liveNextFetchCountdown = this.liveIntervalSeconds;
      if (this.liveIntervalTimer) clearTimeout(this.liveIntervalTimer);
      this.liveIntervalTimer = setTimeout(() => this.tickLivePoll(), this.liveIntervalSeconds * 1000);
    },
    
    stopLivePoll() {
      this.livePollActive = false;
      if (this.liveIntervalTimer) clearTimeout(this.liveIntervalTimer);
      if (this.countdownTimer) clearInterval(this.countdownTimer);
      this.liveNextFetchCountdown = 0;
    },

    async fetchCompliance() {
       this.loadingCompliance = true;
       try {
           const activeRes = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway + '/compliance/2');
           if (activeRes) {
               this.activeProfileName = activeRes.complianceRuleTypeName || activeRes.complianceName || 'User Defined';
               if (activeRes.catalog) {
                   this.complianceDetails = activeRes.catalog.map(g => ({
                       groupName: g.headLine || 'General',
                       itemList: (g.parameters && g.parameters[0] && g.parameters[0].content) ? g.parameters[0].content : []
                   }));
               } else {
                   this.complianceDetails = activeRes.groupList || [];
               }
           }
           const listRes = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway + '/compliance/1');
           if (listRes && Array.isArray(listRes)) {
               this.complianceList = listRes.map(p => p.complianceName || p.name);
           } else {
               if (listRes && listRes.list) {
                   this.complianceList = listRes.list.map(p => p.complianceName || p.name);
               } else if (this.activeProfileName) {
                   this.complianceList = [this.activeProfileName];
               }
           }
       } catch (err) {
           console.error("Failed to load Compliance APIs", err);
           this.activeProfileName = 'Unknown Status';
           this.complianceDetails = [];
           this.complianceList = [];
       }
       this.loadingCompliance = false;
    },

    exportComplianceCSV() {
        if (!this.complianceDetails || this.complianceDetails.length === 0) return;
        let csv = "Group,Name,Value,Unit\n";
        this.complianceDetails.forEach(group => {
            group.itemList.forEach(item => {
                const groupName = group.groupName.replace(/"/g, '""');
                const itemName = item.name.replace(/"/g, '""');
                const val = (item.value || '').toString().replace(/"/g, '""');
                const unit = (item.unit || '').toString().replace(/"/g, '""');
                csv += `"${groupName}","${itemName}","${val}","${unit}"\n`;
            });
        });
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.setAttribute("download", `grid_profile_${this.activeProfileName.replace(/\W+/g, '_')}.csv`);
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    },

    async toggleGrid() {
        // GridConnectionState (DEF-GRID-STATE-ENUM): 'Connected' | 'SimulatedOffGrid' | 'Outage' | 'NotGridTied'
        // Modal opens for all states — Outage/NotGridTied show disabled Execute + reason text.
        this.pendingGridState = this.data.grid_status || 'Connected';
        this.pendingTargetState = this.pendingGridState === 'Connected' ? 'Off-Grid' : 'On-Grid';
        // Keep pendingIsGridConnected for any legacy references (true = currently connected, going off-grid)
        this.pendingIsGridConnected = this.pendingGridState === 'Connected';
        this.reconnectSoc = this.data.offgrid_reserve_soc || 5;
        this.ackOffGridRisk = false;
        this.showOffGridModal = true;
    },
    
    async executeToggleGrid() {
        this.busy = true;
        const payload = {
            offgrid: this.pendingGridState === 'Connected',  // true = going off-grid; false = reconnecting
            soc: this.reconnectSoc
        };

        const res = await fetchJSON('api/gateways/' + this.$store.app.selectedGateway + '/profile/islanding', {
            method: 'POST', body: JSON.stringify(payload)
        });
        this.busy = false;

        if (res && res.ok) {
            this.$store.app.addToast(
                `${this.pendingTargetState} command queued — allow a few seconds for relays to actuate.`,
                'success'
            );
            this.showOffGridModal = false;
            this.ackOffGridRisk = false;
            // Lock the trigger button until the next poll confirms state has changed
            this.gridPending = true;
            this.gridCommandPendingSince = Date.now();
            await this.load();

        } else {
            this.$store.app.addToast(
                'Grid islanding command failed: ' + (res?.error || 'Unknown error'),
                'error'
            );
        }
    },

    relayIcon(state) {
      return state ? '#zap' : '#zap-off';
    },

    relayColor(state) {
      return state ? 'var(--ok)' : 'var(--text-muted)';
    },
  };
}
