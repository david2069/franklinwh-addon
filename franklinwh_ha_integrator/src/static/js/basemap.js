/* Basemap tile URLs, in one place.
 *
 * CARTO's public basemaps now require an API key: without one the tiles come
 * back stamped "API KEY REQUIRED" diagonally across the map, which is what the
 * Weather & Radar and API Metrics maps were showing.
 *
 * The key is per-instance and is entered in Settings, never shipped in the
 * source — the add-on repository is public, and a key committed there would be
 * scraped within hours. window.FHAI_CARTO_KEY is injected by the server from
 * that setting, so it is empty until someone enters one.
 *
 * With no key we fall back to a keyless provider rather than rendering a
 * watermarked map: a map that works is worth more than a brand, and an install
 * that has not been configured should still look finished.
 *
 * Two call sites used to hold their own copies of these URLs, which is how they
 * came to disagree about the theme handling.
 */
(function () {
  'use strict';

  const CARTO = {
    dark: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    light: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  };

  // Esri's canvas basemaps need no key and carry no watermark. Their dark
  // variant is close enough to CARTO's that the panels do not need restyling.
  const KEYLESS = {
    dark: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    light: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  };

  const ATTRIB = {
    carto: '&copy; OpenStreetMap contributors &copy; CARTO',
    keyless: 'Tiles &copy; Esri',
  };

  function key() {
    return (window.FHAI_CARTO_KEY || '').trim();
  }

  /** Tile URL template for the given theme ('dark' | 'light'). */
  window.fhaiBasemapUrl = function (theme) {
    const variant = theme === 'light' ? 'light' : 'dark';
    const k = key();
    if (!k) return KEYLESS[variant];
    // Leaflet substitutes {s}/{z}/{x}/{y}/{r}; the query string is untouched.
    return CARTO[variant] + '?api_key=' + encodeURIComponent(k);
  };

  /** Attribution matching whichever provider fhaiBasemapUrl returned. */
  window.fhaiBasemapAttribution = function () {
    return key() ? ATTRIB.carto : ATTRIB.keyless;
  };

  /** Esri's canvas tiles stop at 16; CARTO goes to 19. */
  window.fhaiBasemapMaxZoom = function () {
    return key() ? 19 : 16;
  };
})();
