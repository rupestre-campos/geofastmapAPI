/**
 * Shared map and vector style utilities for GeoFast frontend.
 * Use across collection, items, item, map_edit, map_view, style_editor, etc.
 * Include once per page: <script src="{{ base }}/static/js/geofast-map-utils.js"></script>
 */
(function(global) {
  'use strict';

  var LINE_DASH = { solid: [1, 0], dashed: [4, 2], dotted: [1, 2] };
  var DEFAULT_STYLE_SPEC = {
    fillColor: '#58a6ff',
    lineColor: '#58a6ff',
    fillOpacity: 0.6,
    lineOpacity: 1,
    lineWidth: 1,
    linePattern: 'solid',
    fillEnabled: true,
    lineEnabled: true,
    pointEnabled: true,
    pointColor: '#58a6ff',
    pointOpacity: 0.9,
    pointSize: 8,
    pointIcon: 'circle'
  };

  var pointFilter = ['in', ['geometry-type'], ['literal', ['Point', 'MultiPoint']]];
  var notPointFilter = ['!', ['in', ['geometry-type'], ['literal', ['Point', 'MultiPoint']]]];

  function zoomStopsToExpression(stops) {
    if (!stops || stops.length < 2) return null;
    var flat = [];
    stops.forEach(function(s) { flat.push(Number(s[0]), Number(s[1])); });
    return ['interpolate', ['linear'], ['zoom']].concat(flat);
  }

  /**
   * Build basemap config keyed by id (fallback when API has no basemaps). Pass googleKey (optional) for Google layers.
   * Each value has: tiles, labels?, name?, copyright?, minZoom?, maxZoom?
   */
  function getBasemaps(googleKey) {
    var g = googleKey || '';
    var q = g ? '&key=' + g : '';
    return {
      osm: { tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], name: 'OpenStreetMap', copyright: '© OpenStreetMap contributors', minZoom: 0, maxZoom: 22 },
      streets: { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}'], name: 'Esri Streets', copyright: 'Esri', minZoom: 0, maxZoom: 22 },
      satellite: { tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], name: 'Esri Satellite', copyright: 'Esri', minZoom: 0, maxZoom: 22 },
      hybrid: {
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        labels: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Reference_Overlay/MapServer/tile/{z}/{y}/{x}',
        name: 'Esri Hybrid', copyright: 'Esri', minZoom: 0, maxZoom: 22
      },
      google_satellite: { tiles: ['https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}' + q], name: 'Google Satellite', copyright: '© Google', minZoom: 0, maxZoom: 22 },
      google_hybrid: { tiles: ['https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}' + q], name: 'Google Hybrid', copyright: '© Google', minZoom: 0, maxZoom: 22 }
    };
  }

  /**
   * Fetch basemaps from API GET /styles/basemaps. Returns Promise<{ basemaps: Array, byId: Object }>.
   * basemaps: list of { id, name, copyright, min_zoom, max_zoom, tiles, labels }; byId[id] = { tiles, labels, name, copyright, minZoom, maxZoom }.
   * Rejects on network error; use .catch() to fall back to getBasemaps(googleKey).
   */
  function fetchBasemaps(baseUrl) {
    var url = (baseUrl || '').replace(/\/$/, '') + '/styles/basemaps';
    return fetch(url, { headers: { Accept: 'application/json' } })
      .then(function(r) { if (!r.ok) throw new Error(r.statusText); return r.json(); })
      .then(function(data) {
        var list = data.basemaps || [];
        var byId = {};
        list.forEach(function(b) {
          byId[b.id] = {
            tiles: b.tiles || [],
            labels: b.labels || undefined,
            name: b.name || b.id,
            copyright: b.copyright || undefined,
            minZoom: b.min_zoom != null ? b.min_zoom : 0,
            maxZoom: b.max_zoom != null ? b.max_zoom : 22
          };
        });
        return { basemaps: list, byId: byId };
      });
  }

  /**
   * Return URL origin prefixes for basemap tile templates so we can detect basemap tile requests.
   * tiles: array of strings like 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
   */
  function getBasemapUrlPrefixes(tiles) {
    if (!tiles || !tiles.length) return [];
    var prefixes = [];
    for (var i = 0; i < tiles.length; i++) {
      var t = tiles[i];
      try {
        var withZeros = t.replace(/\{[xyz]\}/gi, '0');
        prefixes.push(new URL(withZeros).origin);
      } catch (e) {
        var beforeZ = t.indexOf('{z}') >= 0 ? t.split('{z}')[0] : t;
        if (beforeZ.indexOf('http') === 0) prefixes.push(beforeZ.replace(/\/+$/, ''));
      }
    }
    return prefixes;
  }

  /**
   * Clamp zoom in a tile URL to maxZoom. Only modifies url if it matches one of the basemap prefixes.
   * Handles path-style (.../z/x/y) and query-style (...?z=16&x=...&y=...).
   */
  function clampBasemapTileUrl(url, maxZoom, urlPrefixes) {
    if (!urlPrefixes || !urlPrefixes.length || maxZoom == null) return url;
    var isBasemap = false;
    for (var p = 0; p < urlPrefixes.length; p++) {
      if (url.indexOf(urlPrefixes[p]) === 0) { isBasemap = true; break; }
    }
    if (!isBasemap) return url;
    var out = url;
    // Path-style: .../16/1234/5678 or .../16/1234/5678.png (capture rest so we keep .png etc.)
    out = out.replace(/\/(\d+)\/(\d+)\/(\d+)(.*)$/, function(match, z, x, y, rest) {
      return '/' + Math.min(parseInt(z, 10), maxZoom) + '/' + x + '/' + y + rest;
    });
    // Query-style: z=16
    out = out.replace(/([?&])z=(\d+)/gi, function(match, pre, z) {
      return pre + 'z=' + Math.min(parseInt(z, 10), maxZoom);
    });
    return out;
  }

  /**
   * Set the map's transformRequest so basemap tile URLs have their zoom clamped to maxZoom.
   * MapLibre's raster source does not respect source maxzoom for requests; vector tile layers do.
   * This ensures we never request tiles above the basemap's max zoom.
   * Call after creating the map (with initial basemap) and applyBasemapToMap will call it when switching basemaps.
   * @param {object} map - MapLibre map instance
   * @param {number} maxZoom - basemap max zoom (e.g. 12)
   * @param {string[]} tiles - basemap tile URL templates (e.g. basemapConfig.tiles)
   */
  function setBasemapTransformRequest(map, maxZoom, tiles) {
    if (!map) return;
    map._geofastBasemapMaxZoom = maxZoom;
    map._geofastBasemapPrefixes = getBasemapUrlPrefixes(tiles || []);
    if (map._geofastBasemapTransformSet) return;
    map._geofastBasemapTransformSet = true;
    var existing = typeof map.getTransformRequest === 'function' ? map.getTransformRequest() : null;
    map.setTransformRequest(function(url, resourceType) {
      var maxZ = map._geofastBasemapMaxZoom;
      var prefixes = map._geofastBasemapPrefixes;
      var clamped = (maxZ != null && prefixes && prefixes.length) ? clampBasemapTileUrl(url, maxZ, prefixes) : url;
      if (clamped !== url) return { url: clamped };
      if (existing) return existing(url, resourceType);
      return { url: url };
    });
  }

  /**
   * Build MapLibre style object with a single raster basemap source and layer.
   * Both source and layer use the same maxzoom so that:
   * 1. The layer is not drawn above that zoom (stops tile requests above max native zoom).
   * 2. MapLibre's RasterTileSource does not apply source maxzoom to tile URLs, so capping
   *    the layer's maxzoom is what actually prevents requests for z > maxzoom.
   * basemapConfig: { tiles, minZoom?, maxZoom? } (from getBasemaps or fetchBasemaps byId[id]).
   */
  function buildMapStyleWithBasemap(basemapConfig) {
    var minZ = basemapConfig.minZoom != null ? basemapConfig.minZoom : 0;
    var maxZ = basemapConfig.maxZoom != null ? basemapConfig.maxZoom : 22;
    return {
      version: 8,
      sources: {
        basemap: {
          type: 'raster',
          tiles: basemapConfig.tiles,
          tileSize: 256,
          minzoom: minZ,
          maxzoom: maxZ
        }
      },
      layers: [{ id: 'basemap', type: 'raster', source: 'basemap', minzoom: minZ, maxzoom: maxZ }]
    };
  }

  /**
   * Populate a <select id="map-basemap"> with options from basemap list. Option value = id, text = name.
   * basemapList: array of { id, name } (from fetchBasemaps().basemaps or Object.keys(getBasemaps()) with name from byId).
   */
  function populateBasemapSelect(selectEl, basemapList, byId) {
    if (!selectEl) return;
    selectEl.innerHTML = '';
    (basemapList || []).forEach(function(b) {
      var id = typeof b === 'string' ? b : b.id;
      var name = (byId && byId[id] && byId[id].name) ? byId[id].name : (b.name || id);
      var opt = document.createElement('option');
      opt.value = id;
      opt.textContent = name;
      selectEl.appendChild(opt);
    });
  }

  /**
   * Update a copyright/attribution element (e.g. #map-basemap-copyright) with basemap copyright text.
   */
  function setBasemapCopyright(el, basemapConfig) {
    if (!el) return;
    var text = (basemapConfig && basemapConfig.copyright) ? basemapConfig.copyright : '';
    el.textContent = text;
    el.style.display = text ? '' : 'none';
  }

  /**
   * Apply a basemap to an existing map with correct minzoom/maxzoom (max native zoom).
   * Layer maxzoom is set to the basemap max so the layer is not drawn above that zoom; that
   * is what prevents MapLibre from requesting tiles above the configured max (RasterTileSource
   * does not apply source maxzoom to tile request zoom in current MapLibre).
   * Replaces the 'basemap' (and optional 'basemap-labels') source and layer.
   * Basemap layers are always inserted at the bottom (behind all other layers).
   * @param {object} map - MapLibre map instance
   * @param {object} basemapConfig - { tiles, labels?, minZoom?, maxZoom? }
   * @param {object} options - optional; { beforeLayerId: string } can hint first overlay id
   */
  function applyBasemapToMap(map, basemapConfig, options) {
    if (!map || !basemapConfig || !basemapConfig.tiles) return;
    var minZ = basemapConfig.minZoom != null ? basemapConfig.minZoom : 0;
    var maxZ = basemapConfig.maxZoom != null ? basemapConfig.maxZoom : 22;
    var labels = basemapConfig.labels;
    var labelsTiles = Array.isArray(labels) ? labels : (labels ? [labels] : null);

    if (map.getLayer('basemap')) map.removeLayer('basemap');
    if (map.getSource('basemap')) map.removeSource('basemap');
    if (map.getLayer('basemap-labels')) map.removeLayer('basemap-labels');
    if (map.getSource('basemap-labels')) map.removeSource('basemap-labels');

    var style = map.getStyle();
    var layers = style && style.layers;
    var beforeLayerId = null;
    if (layers && layers.length) {
      beforeLayerId = layers[0].id;
    } else if (options && options.beforeLayerId) {
      beforeLayerId = options.beforeLayerId;
    }

    map.addSource('basemap', {
      type: 'raster',
      tiles: basemapConfig.tiles,
      tileSize: 256,
      minzoom: minZ,
      maxzoom: maxZ
    });
    map.addLayer({ id: 'basemap', type: 'raster', source: 'basemap', minzoom: minZ, maxzoom: maxZ }, beforeLayerId);

    if (labelsTiles && labelsTiles.length) {
      map.addSource('basemap-labels', { type: 'raster', tiles: labelsTiles, tileSize: 256, minzoom: minZ, maxzoom: maxZ });
      map.addLayer({ id: 'basemap-labels', type: 'raster', source: 'basemap-labels', minzoom: minZ, maxzoom: maxZ }, beforeLayerId);
    }

    setBasemapTransformRequest(map, maxZ, basemapConfig.tiles);
  }

  /**
   * Convert a style_spec (from API or form) to paint values for fill/line/point layers.
   * Returns scalars or MapLibre expressions for zoom-based rules.
   * Use for addLayer paint so edit and view render the same.
   */
  function specToPaint(spec) {
    spec = spec || {};
    var fillOpacity = (spec.fillOpacityZoom && spec.fillOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.fillOpacityZoom)
      : (spec.fillOpacity != null ? spec.fillOpacity : DEFAULT_STYLE_SPEC.fillOpacity);
    var lineWidth = (spec.lineWidthZoom && spec.lineWidthZoom.length >= 2)
      ? zoomStopsToExpression(spec.lineWidthZoom)
      : Math.max(0.5, spec.lineWidth != null ? spec.lineWidth : DEFAULT_STYLE_SPEC.lineWidth);
    var lineOpacity = (spec.lineOpacityZoom && spec.lineOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.lineOpacityZoom)
      : (spec.lineOpacity != null ? spec.lineOpacity : DEFAULT_STYLE_SPEC.lineOpacity);
    var pointRadius = (spec.pointSizeZoom && spec.pointSizeZoom.length >= 2)
      ? zoomStopsToExpression(spec.pointSizeZoom)
      : Math.max(1, Math.min(40, spec.pointSize != null ? spec.pointSize : DEFAULT_STYLE_SPEC.pointSize));
    var pointOpacity = (spec.pointOpacityZoom && spec.pointOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.pointOpacityZoom)
      : (spec.pointOpacity != null ? spec.pointOpacity : DEFAULT_STYLE_SPEC.pointOpacity);
    var fillEnabled = spec.fillEnabled !== false;
    var lineEnabled = spec.lineEnabled !== false;
    var pointEnabled = spec.pointEnabled !== false;
    return {
      fillColor: spec.fillColor || DEFAULT_STYLE_SPEC.fillColor,
      lineColor: spec.lineColor || DEFAULT_STYLE_SPEC.lineColor,
      fillOpacity: fillOpacity,
      lineOpacity: lineOpacity,
      lineWidth: lineWidth,
      lineDash: LINE_DASH[spec.linePattern || 'solid'] || LINE_DASH.solid,
      fillEnabled: fillEnabled,
      fillVisible: fillEnabled,
      lineEnabled: lineEnabled,
      lineVisible: lineEnabled,
      pointEnabled: pointEnabled,
      pointVisible: pointEnabled,
      pointColor: spec.pointColor || spec.lineColor || DEFAULT_STYLE_SPEC.pointColor,
      pointRadius: pointRadius,
      pointOpacity: pointOpacity,
      pointIcon: spec.pointIcon || DEFAULT_STYLE_SPEC.pointIcon
    };
  }

  function escapeHtml(s) {
    if (s == null || s === undefined) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  /**
   * Build popup HTML for a vector tile feature.
   * @param {Object} feat - Feature with properties and optionally id
   * @param {string} base - Base URL (e.g. window.location.origin + path prefix)
   * @param {string} collectionId - Collection id for the item link
   * @returns {string} HTML string for popup content
   */
  function popupHtmlForFeature(feat, base, collectionId) {
    var props = feat.properties || {};
    var id = props.id != null ? String(props.id) : (feat.id != null ? String(feat.id) : '—');
    var featureUrl = base + '/collections/' + encodeURIComponent(collectionId) + '/items/' + encodeURIComponent(id) + '?f=html';
    var parts = [
      '<div class="map-popup">',
      '<div class="map-popup-id"><strong>ID</strong> <a href="' + escapeHtml(featureUrl) + '">' + escapeHtml(id) + '</a></div>'
    ];
    Object.keys(props).sort().forEach(function(k) {
      if (k === 'id') return;
      var v = props[k];
      var val = v === null || v === undefined ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
      parts.push('<div class="map-popup-row"><strong>' + escapeHtml(k) + '</strong> ' + escapeHtml(val) + '</div>');
    });
    parts.push('</div>');
    return parts.join('');
  }

  /**
   * Apply paint values (scalar or expression) to a map layer. Use after addLayer when you need to set
   * paint property and value may be an expression array.
   */
  function setPaintPropertySafe(map, layerId, property, valueOrExpression) {
    try {
      if (map.getLayer(layerId)) map.setPaintProperty(layerId, property, valueOrExpression);
    } catch (e) {}
  }

  global.GeofastMapUtils = {
    LINE_DASH: LINE_DASH,
    DEFAULT_STYLE_SPEC: DEFAULT_STYLE_SPEC,
    pointFilter: pointFilter,
    notPointFilter: notPointFilter,
    zoomStopsToExpression: zoomStopsToExpression,
    getBasemaps: getBasemaps,
    fetchBasemaps: fetchBasemaps,
    buildMapStyleWithBasemap: buildMapStyleWithBasemap,
    populateBasemapSelect: populateBasemapSelect,
    setBasemapCopyright: setBasemapCopyright,
    setBasemapTransformRequest: setBasemapTransformRequest,
    applyBasemapToMap: applyBasemapToMap,
    specToPaint: specToPaint,
    escapeHtml: escapeHtml,
    popupHtmlForFeature: popupHtmlForFeature,
    setPaintPropertySafe: setPaintPropertySafe
  };
})(typeof window !== 'undefined' ? window : this);
