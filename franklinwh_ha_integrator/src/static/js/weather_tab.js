/* Weather Tab Alpine Component — v2
   Provides Select & Configure Provider, Consolidated Telemetry, 7-Day forecast cards,
   and an animated, high-fidelity Leaflet Precipitation Radar Map. */
function weatherTab() {
  return {
    // Configurations
    config: {
      provider: 'franklin',
      owm_api_key: '',
      owm_lat: '',
      owm_lon: '',
      ha_token: '',
      ha_url: '',
      ha_entity_id: '',
      units: 'metric'
    },
    loadingConfig: false,
    savingConfig: false,
    testingConnection: false,
    testError: '',
    testSuccess: '',

    // Telemetry & Forecasts
    currentWeather: null,
    forecast: [],
    loadingCurrent: false,
    loadingForecast: false,
    activeTab: 'view', // 'view' | 'config'

    // Leaflet Interactive Map
    map: null,
    baseLayer: null,
    radarLayers: [],
    satelliteLayer: null,
    radarType: 'radar',
    radarData: null,
    radarTimestamps: [],
    currentRadarIndex: 0,
    radarPlayInterval: null,
    radarPlaySpeed: 1000, // ms per frame
    isRadarPlaying: false,
    mapLatitude: -33.8688,
    mapLongitude: 151.2093,

    get selectedGateway() {
      return this.$store.app.selectedGateway;
    },

    get activeGatewayObj() {
      return this.$store.app.selectedGatewayObj;
    },

    get isFahrenheit() {
      return this.config.units === 'imperial';
    },

    get tempUnit() {
      return this.isFahrenheit ? '°F' : '°C';
    },

    get windUnit() {
      return this.isFahrenheit ? 'mph' : 'km/h';
    },

    get formattedLastUpdate() {
      if (!this.currentWeather || !this.currentWeather.last_update) return 'Never';
      try {
        const dt = new Date(this.currentWeather.last_update);
        return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } catch (e) {
        return 'Unknown';
      }
    },

    // ── Life Cycle ───────────────────────────────────────────────────
    async init() {
      // Refresh telemetry when selected gateway changes
      this.$watch('$store.app.selectedGateway', (val) => {
        if (val) {
          this.refreshAll();
        }
      });

      // Watch theme for map basemap updates
      this.$watch('$store.app.theme', () => {
        this.updateBaseLayer();
      });

      if (this.selectedGateway) {
        await this.refreshAll();
      }
    },

    async refreshAll() {
      await this.loadConfig();
      await Promise.all([
        this.fetchCurrentWeather(),
        this.fetchForecast()
      ]);
      this.initLeafletMap();
    },

    // ── Configuration Manager ────────────────────────────────────────
    async loadConfig() {
      this.loadingConfig = true;
      try {
        const r = await fetch('api/weather/config');
        if (r.ok) {
          const d = await r.json();
          if (d.ok && d.config) {
            this.config = { ...this.config, ...d.config };
            // Read coordinate fallback
            if (this.config.owm_lat && this.config.owm_lon) {
              this.mapLatitude = parseFloat(this.config.owm_lat);
              this.mapLongitude = parseFloat(this.config.owm_lon);
            }
          }
        }
      } catch (err) {
        console.error('Failed to load weather config:', err);
      } finally {
        this.loadingConfig = false;
      }
    },

    async saveConfig() {
      this.savingConfig = true;
      this.testError = '';
      this.testSuccess = '';
      try {
        const r = await fetch('api/weather/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.config)
        });
        const res = await r.json().catch(() => ({}));
        if (r.ok && res.ok) {
          this.$store.app.addToast('Weather configurations saved successfully.', 'success');
          this.activeTab = 'view';
          // Reload everything to apply
          await this.refreshAll();
        } else {
          throw new Error(res.error || res.detail || `HTTP ${r.status}`);
        }
      } catch (err) {
        this.$store.app.addToast('Failed to save configuration: ' + err.message, 'error');
      } finally {
        this.savingConfig = false;
      }
    },

    async testWeatherConnection() {
      this.testingConnection = true;
      this.testError = '';
      this.testSuccess = '';
      try {
        const r = await fetch('api/weather/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.config)
        });
        const res = await r.json();
        if (r.ok && res.ok) {
          this.testSuccess = res.message || 'Connection test successful!';
          this.$store.app.addToast('Weather provider test succeeded!', 'success');
        } else {
          this.testError = res.error || 'Connection verification failed.';
          this.$store.app.addToast('Weather provider test failed.', 'error');
        }
      } catch (err) {
        this.testError = err.message || 'Network error during validation.';
      } finally {
        this.testingConnection = false;
      }
    },

    // ── Telemetry Fetchers ───────────────────────────────────────────
    async fetchCurrentWeather() {
      this.loadingCurrent = true;
      try {
        const r = await fetch('api/weather/current');
        if (r.ok) {
          const d = await r.json();
          if (d.ok && d.weather) {
            this.currentWeather = d.weather;
            // Update coordinates fallback if empty in config but provided in response
            if ((!this.config.owm_lat || !this.config.owm_lon) && d.weather.lat && d.weather.lon) {
              this.mapLatitude = parseFloat(d.weather.lat);
              this.mapLongitude = parseFloat(d.weather.lon);
            }
          }
        }
      } catch (err) {
        console.error('Failed to fetch current weather:', err);
      } finally {
        this.loadingCurrent = false;
      }
    },

    async fetchForecast() {
      this.loadingForecast = true;
      try {
        const r = await fetch('api/weather/forecast');
        if (r.ok) {
          const d = await r.json();
          if (d.ok && d.forecast) {
            this.forecast = d.forecast;
          }
        }
      } catch (err) {
        console.error('Failed to fetch forecast:', err);
      } finally {
        this.loadingForecast = false;
      }
    },

    // ── Leaflet Precipitation Map ────────────────────────────────────
    async initLeafletMap() {
      this.$nextTick(async () => {
        const container = document.getElementById('weather-radar-map');
        if (!container) return;

        // Clean existing map instance
        if (this.map) {
          this.stopRadarAnimation();
          this.map.remove();
          this.map = null;
          this.baseLayer = null;
          this.radarLayers = [];
          this.satelliteLayer = null;
        }

        // Create Map
        this.map = L.map('weather-radar-map', {
          zoomControl: false,
          attributionControl: false
        }).setView([this.mapLatitude, this.mapLongitude], 9);

        // Add Premium Tiles
        this.updateBaseLayer();

        // Zoom controller to bottom-right
        L.control.zoom({ position: 'bottomright' }).addTo(this.map);

        // Add Marker at Gateway Site
        L.marker([this.mapLatitude, this.mapLongitude]).addTo(this.map)
          .bindPopup('<b>Gateway Location</b>')
          .openPopup();

        // Fetch Radar Frames from RainViewer
        await this.loadRadarFrames();
      });
    },

    updateBaseLayer() {
      if (!this.map) return;

      const isDark = document.documentElement.classList.contains('dark') || 
                     (this.$store.app.theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches) ||
                     (this.$store.app.theme === 'dark');
      
      // basemap.js owns the provider choice: CARTO when a key is configured,
      // a keyless provider otherwise. Two tabs used to hold their own copies of
      // these URLs, which is how they came to disagree about theme handling.
      const baseUrl = window.fhaiBasemapUrl(isDark ? 'dark' : 'light');

      if (this.baseLayer) {
        this.baseLayer.setUrl(baseUrl);
      } else {
        this.baseLayer = L.tileLayer(baseUrl, {
          maxZoom: window.fhaiBasemapMaxZoom(),
          attribution: window.fhaiBasemapAttribution(),
        }).addTo(this.map);
      }
    },

    async loadRadarFrames() {
      try {
        const r = await fetch('https://api.rainviewer.com/public/weather-maps.json');
        if (r.ok) {
          const data = await r.json();
          this.radarData = data;
          if (data) {
            let list = (this.radarType === 'satellite' ? data.satellite?.past : data.radar?.past) || [];
            if (this.radarType === 'radar' && list.length > 5) {
              list = list.slice(-5);
            }
            this.radarTimestamps = list;
            this.currentRadarIndex = this.radarTimestamps.length - 1;
            this.updateRadarLayer();
          }
        }
      } catch (e) {
        console.warn('Failed to load RainViewer radar timeline, falling back to static nowcast:', e);
        // Fallback static
        this.radarTimestamps = [{ time: 'nowcast_10m', path: '/v2/radar/nowcast_10m' }];
        this.currentRadarIndex = 0;
        this.updateRadarLayer();
      }
    },

    setRadarType(type) {
      if (this.radarType === type) return;
      if (type === 'satellite') {
        const key = this.config.owm_api_key ? this.config.owm_api_key.trim() : '';
        if (!key) {
          this.$store.app.addToast('Cloud cover overlay requires an OpenWeatherMap API key. Please configure it in the Setup Provider tab.', 'warn');
          return;
        }
        this.stopRadarAnimation();
        this.radarType = 'satellite';

        // Remove active radar layers from the map
        this.radarLayers.forEach(layer => {
          if (this.map && this.map.hasLayer(layer)) {
            this.map.removeLayer(layer);
          }
        });
        this.radarLayers = [];

        this.radarTimestamps = [];
        this.currentRadarIndex = 0;
        this.updateRadarLayer();
      } else {
        this.radarType = 'radar';

        // Remove satellite layer if it exists
        if (this.satelliteLayer && this.map) {
          this.map.removeLayer(this.satelliteLayer);
          this.satelliteLayer = null;
        }

        if (this.radarData && this.radarData.radar && this.radarData.radar.past) {
          let list = this.radarData.radar.past;
          if (list.length > 5) {
            list = list.slice(-5);
          }
          this.radarTimestamps = list;
          this.currentRadarIndex = this.radarTimestamps.length - 1;
          this.updateRadarLayer();
        } else {
          this.loadRadarFrames();
        }
      }
    },

    updateRadarLayer() {
      if (!this.map) return;

      if (this.radarType === 'satellite') {
        const key = this.config.owm_api_key ? this.config.owm_api_key.trim() : '';
        if (!key) {
          if (this.satelliteLayer) {
            this.map.removeLayer(this.satelliteLayer);
            this.satelliteLayer = null;
          }
          return;
        }

        // Hide radar layers
        this.radarLayers.forEach(layer => {
          if (this.map.hasLayer(layer)) {
            layer.setOpacity(0);
          }
        });

        if (!this.satelliteLayer) {
          const radarUrl = `https://tile.openweathermap.org/map/clouds_new/{z}/{x}/{y}.png?appid=${key}`;
          this.satelliteLayer = L.tileLayer(radarUrl, {
            opacity: 0.65,
            zIndex: 100,
            maxZoom: 19,
            maxNativeZoom: 19
          }).addTo(this.map);
        } else {
          this.satelliteLayer.setOpacity(0.65);
        }
      } else {
        if (this.radarTimestamps.length === 0) return;

        // Hide satellite layer
        if (this.satelliteLayer) {
          this.satelliteLayer.setOpacity(0);
        }

        const needsInit = this.radarLayers.length !== this.radarTimestamps.length;
        if (needsInit) {
          // Clear old layers
          this.radarLayers.forEach(layer => {
            if (this.map.hasLayer(layer)) {
              this.map.removeLayer(layer);
            }
          });
          this.radarLayers = [];

          // Create new ones
          this.radarTimestamps.forEach((frame, index) => {
            const radarUrl = `https://tilecache.rainviewer.com${frame.path}/256/{z}/{x}/{y}/2/1_1.png`;
            const layer = L.tileLayer(radarUrl, {
              opacity: index === this.currentRadarIndex ? 0.65 : 0,
              zIndex: 100,
              maxZoom: 19,
              maxNativeZoom: 7
            }).addTo(this.map);
            this.radarLayers.push(layer);
          });
        } else {
          // Just update opacities
          this.radarLayers.forEach((layer, index) => {
            const targetOpacity = index === this.currentRadarIndex ? 0.65 : 0;
            layer.setOpacity(targetOpacity);
          });
        }
      }
    },

    toggleRadarPlay() {
      if (this.isRadarPlaying) {
        this.stopRadarAnimation();
      } else {
        this.startRadarAnimation();
      }
    },

    startRadarAnimation() {
      if (this.radarTimestamps.length <= 1) return;
      this.isRadarPlaying = true;
      this.radarPlayInterval = setInterval(() => {
        this.currentRadarIndex = (this.currentRadarIndex + 1) % this.radarTimestamps.length;
        this.updateRadarLayer();
      }, this.radarPlaySpeed);
    },

    stopRadarAnimation() {
      this.isRadarPlaying = false;
      if (this.radarPlayInterval) {
        clearInterval(this.radarPlayInterval);
        this.radarPlayInterval = null;
      }
    },

    getFormattedRadarTime() {
      if (this.radarTimestamps.length === 0) return 'Loading...';
      const item = this.radarTimestamps[this.currentRadarIndex];
      const timeVal = (item && typeof item === 'object') ? item.time : item;
      if (typeof timeVal === 'string' && timeVal.includes('nowcast')) return 'Live (Nowcast)';
      try {
        const dt = new Date(timeVal * 1000);
        return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } catch (e) {
        return 'Live';
      }
    },

    getConditionIcon(cond) {
      if (!cond) return 'fa-cloud';
      const c = cond.toLowerCase();
      if (c.includes('clear') || c.includes('sunny')) return 'fa-sun text-amber-400 animate-pulse';
      if (c.includes('rain') || c.includes('drizzle') || c.includes('showers')) return 'fa-cloud-showers-heavy text-blue-400';
      if (c.includes('storm') || c.includes('thunder')) return 'fa-cloud-bolt text-purple-400 animate-bounce';
      if (c.includes('snow') || c.includes('ice') || c.includes('freeze')) return 'fa-snowflake text-sky-200';
      if (c.includes('cloud') || c.includes('overcast')) return 'fa-cloud text-gray-400';
      return 'fa-cloud-sun text-amber-300';
    }
  };
}


/* Basemap key settings — see src/routes/api_map.py.
 *
 * Defined here rather than inline in the template because a deferred script
 * must have registered every x-data function before Alpine initialises, and
 * weather_tab.js already loads on that path.
 */
function basemapSettings() {
  return {
    hasKey: false,
    masked: '',
    providerLabel: '',
    editing: false,
    draft: '',
    busy: false,
    message: '',
    error: false,

    async load() {
      try {
        const r = await fetch('api/map/basemap');
        const d = await r.json();
        this.apply(d);
      } catch (e) {
        this.providerLabel = 'unknown';
      }
    },

    apply(d) {
      this.hasKey = !!(d && d.has_key);
      this.masked = (d && d.masked) || '';
      this.providerLabel = (d && d.provider_label) || '';
    },

    async save() {
      // A blank draft means "unchanged" server-side, so an accidental save
      // cannot wipe a working key.
      this.busy = true;
      this.error = false;
      try {
        const r = await fetch('api/map/basemap', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ carto_api_key: this.draft }),
        });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.detail || 'save failed');
        this.apply(d);
        this.draft = '';
        this.editing = false;
        this.message = d.changed
          ? 'Key saved. Reload the page for the maps to pick it up.'
          : 'No change — the existing key was kept.';
      } catch (e) {
        this.error = true;
        this.message = 'Could not save the key.';
      } finally {
        this.busy = false;
      }
    },

    async clearKey() {
      this.busy = true;
      this.error = false;
      try {
        const r = await fetch('api/map/basemap', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ clear: true }),
        });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error('clear failed');
        this.apply(d);
        this.message = 'Key removed. Reload the page — maps fall back to keyless tiles.';
      } catch (e) {
        this.error = true;
        this.message = 'Could not remove the key.';
      } finally {
        this.busy = false;
      }
    },
  };
}
