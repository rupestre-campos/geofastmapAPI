/**
 * Shared map and vector style utilities for GeoFastMap frontend.
 * Use across collection, items, item, map_edit, map_view, style_editor, etc.
 * Include once per page: <script src="{{ base }}/static/js/geofastmap-utils.js"></script>
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
    var url = (baseUrl || '').replace(/\/$/, '') + '/styles/basemaps?t=' + Date.now();
    return fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } })
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
    // Basemap prefix matching uses URL *origin* only. When the API host also serves basemap
    // tiles, mosaic/STAC/COG proxy URLs on the same origin would be mis-clamped (z lowered
    // without fixing x/y), breaking tiles and stressing Titiler. Never rewrite our tile proxies.
    if (url.indexOf('/titiler/tiles/') !== -1) return url;
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
    map._geofastmapBasemapMaxZoom = maxZoom;
    map._geofastmapBasemapPrefixes = getBasemapUrlPrefixes(tiles || []);
    if (map._geofastmapBasemapTransformSet) return;
    map._geofastmapBasemapTransformSet = true;
    var existing = typeof map.getTransformRequest === 'function' ? map.getTransformRequest() : null;
    map.setTransformRequest(function(url, resourceType) {
      var maxZ = map._geofastmapBasemapMaxZoom;
      var prefixes = map._geofastmapBasemapPrefixes;
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
    var attr = basemapConfig.copyright ? String(basemapConfig.copyright).trim() : '';
    var basemapSrc = {
      type: 'raster',
      tiles: basemapConfig.tiles,
      tileSize: 256,
      minzoom: minZ,
      maxzoom: maxZ
    };
    if (attr) basemapSrc.attribution = attr;
    return {
      version: 8,
      sources: {
        basemap: basemapSrc
      },
      layers: [{ id: 'basemap', type: 'raster', source: 'basemap', minzoom: minZ, maxzoom: maxZ }]
    };
  }

  /**
   * Atmospheric fog for 3D terrain (tilted map): replaces default dark upper sky with a daylight blue.
   * Call after map.setTerrain(...); call clearTerrainAtmosphere when terrain is off.
   */
  function setTerrainAtmosphere(map) {
    if (!map || typeof map.setFog !== 'function') return;
    map.setFog({
      range: [1, 24],
      color: 'rgb(190, 215, 238)',
      'high-color': 'rgb(100, 165, 230)',
      'horizon-blend': 0.38,
      'space-color': 'rgb(55, 125, 205)',
      'star-intensity': 0
    });
  }

  function clearTerrainAtmosphere(map) {
    if (!map || typeof map.setFog !== 'function') return;
    map.setFog(null);
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

    var attr = basemapConfig.copyright ? String(basemapConfig.copyright).trim() : '';
    var basemapSrc = {
      type: 'raster',
      tiles: basemapConfig.tiles,
      tileSize: 256,
      minzoom: minZ,
      maxzoom: maxZ
    };
    if (attr) basemapSrc.attribution = attr;
    map.addSource('basemap', basemapSrc);
    map.addLayer({ id: 'basemap', type: 'raster', source: 'basemap', minzoom: minZ, maxzoom: maxZ }, beforeLayerId);

    if (labelsTiles && labelsTiles.length) {
      var labelsSrc = { type: 'raster', tiles: labelsTiles, tileSize: 256, minzoom: minZ, maxzoom: maxZ };
      if (attr) labelsSrc.attribution = attr;
      map.addSource('basemap-labels', labelsSrc);
      map.addLayer({ id: 'basemap-labels', type: 'raster', source: 'basemap-labels', minzoom: minZ, maxzoom: maxZ }, beforeLayerId);
    }

    setBasemapTransformRequest(map, maxZ, basemapConfig.tiles);
  }

  function _toNumberOrNull(v) {
    var n = parseFloat(v);
    return isNaN(n) ? null : n;
  }

  function _ruleCondition(rule) {
    if (!rule || !rule.when) return null;
    var field = (rule.when.field || '').trim();
    var op = (rule.when.op || 'eq').toLowerCase();
    var valueRaw = rule.when.value;
    if (!field) return null;
    var getVal = ['get', field];
    var cond = null;
    if (op === 'eq' || op === 'ne') {
      var v = valueRaw == null ? '' : String(valueRaw);
      var cmp = ['==', ['to-string', getVal], v];
      cond = op === 'ne' ? ['!', cmp] : cmp;
    } else if (op === 'in') {
      var list = (valueRaw == null ? '' : String(valueRaw)).split(',').map(function(x) { return x.trim(); }).filter(Boolean);
      cond = ['in', ['to-string', getVal], ['literal', list]];
    } else if (op === 'contains') {
      var s = valueRaw == null ? '' : String(valueRaw);
      cond = ['>=', ['index-of', s, ['to-string', getVal]], 0];
    } else if (op === 'gt' || op === 'gte' || op === 'lt' || op === 'lte') {
      var num = _toNumberOrNull(valueRaw);
      if (num == null) return null;
      var left = ['to-number', getVal];
      if (op === 'gt') cond = ['>', left, num];
      if (op === 'gte') cond = ['>=', left, num];
      if (op === 'lt') cond = ['<', left, num];
      if (op === 'lte') cond = ['<=', left, num];
    } else {
      return null;
    }
    // NOTE: We intentionally do NOT include zoom-based constraints here.
    // MapLibre GL's style validation restricts ["zoom"] usage to be an input of a
    // top-level "step" or "interpolate" expression, so using it inside "case/all"
    // conditions can raise validation errors even if rendering seems to work.
    // Zoom-based styling is supported via *Zoom stops* (e.g. lineWidthZoom) which
    // compile to interpolate(["zoom"], ...).
    return cond;
  }

  function _applyRulesToPaintValue(baseValue, rules, pickValueFn) {
    if (!rules || !rules.length) return baseValue;
    var expr = ['case'];
    var any = false;
    for (var i = 0; i < rules.length; i++) {
      var r = rules[i];
      var v = pickValueFn(r);
      if (v === undefined || v === null || v === '') continue;
      var c = _ruleCondition(r);
      if (!c) continue;
      expr.push(c, v);
      any = true;
    }
    if (!any) return baseValue;
    expr.push(baseValue);
    return expr;
  }

  function _applyRulesToZoomStops(stops, rules, pickValueFn) {
    if (!stops || stops.length < 2) return null;
    var flat = [];
    for (var i = 0; i < stops.length; i++) {
      var z = Number(stops[i][0]);
      var v = Number(stops[i][1]);
      if (!isFinite(z) || !isFinite(v)) continue;
      flat.push(z, _applyRulesToPaintValue(v, rules, pickValueFn));
    }
    return ['interpolate', ['linear'], ['zoom']].concat(flat);
  }

  function _toNumberOrNull(v) {
    if (v === undefined || v === null || v === '') return null;
    var n = (typeof v === 'number') ? v : parseFloat(v);
    return isFinite(n) ? n : null;
  }

  /**
   * Convert a style_spec (from API or form) to paint values for fill/line/point layers.
   * Returns scalars or MapLibre expressions for zoom-based rules.
   * Use for addLayer paint so edit and view render the same.
   */
  function specToPaint(spec) {
    spec = spec || {};
    var fillEnabled = spec.fillEnabled !== false;
    var lineEnabled = spec.lineEnabled !== false;
    var pointEnabled = spec.pointEnabled !== false;
    var rules = Array.isArray(spec.rules) ? spec.rules : [];
    var fillColorBase = spec.fillColor || DEFAULT_STYLE_SPEC.fillColor;
    var lineColorBase = spec.lineColor || DEFAULT_STYLE_SPEC.lineColor;
    var pointColorBase = spec.pointColor || spec.lineColor || DEFAULT_STYLE_SPEC.pointColor;
    var fillOpacityBase = (spec.fillOpacityZoom && spec.fillOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.fillOpacityZoom)
      : (spec.fillOpacity != null ? spec.fillOpacity : DEFAULT_STYLE_SPEC.fillOpacity);
    var lineWidthBase = (spec.lineWidthZoom && spec.lineWidthZoom.length >= 2)
      ? zoomStopsToExpression(spec.lineWidthZoom)
      : Math.max(0.5, spec.lineWidth != null ? spec.lineWidth : DEFAULT_STYLE_SPEC.lineWidth);
    var lineOpacityBase = (spec.lineOpacityZoom && spec.lineOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.lineOpacityZoom)
      : (spec.lineOpacity != null ? spec.lineOpacity : DEFAULT_STYLE_SPEC.lineOpacity);
    var pointRadiusBase = (spec.pointSizeZoom && spec.pointSizeZoom.length >= 2)
      ? zoomStopsToExpression(spec.pointSizeZoom)
      : Math.max(1, Math.min(40, spec.pointSize != null ? spec.pointSize : DEFAULT_STYLE_SPEC.pointSize));
    var pointOpacityBase = (spec.pointOpacityZoom && spec.pointOpacityZoom.length >= 2)
      ? zoomStopsToExpression(spec.pointOpacityZoom)
      : (spec.pointOpacity != null ? spec.pointOpacity : DEFAULT_STYLE_SPEC.pointOpacity);
    return {
      fillColor: _applyRulesToPaintValue(fillColorBase, rules, function(r) { return r && r.paint ? r.paint.fillColor : null; }),
      lineColor: _applyRulesToPaintValue(lineColorBase, rules, function(r) { return r && r.paint ? r.paint.lineColor : null; }),
      fillOpacity: (spec.fillOpacityZoom && spec.fillOpacityZoom.length >= 2)
        ? _applyRulesToZoomStops(spec.fillOpacityZoom, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.fillOpacity) : null; })
        : _applyRulesToPaintValue(fillOpacityBase, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.fillOpacity) : null; }),
      lineOpacity: (spec.lineOpacityZoom && spec.lineOpacityZoom.length >= 2)
        ? _applyRulesToZoomStops(spec.lineOpacityZoom, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.lineOpacity) : null; })
        : _applyRulesToPaintValue(lineOpacityBase, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.lineOpacity) : null; }),
      lineWidth: (spec.lineWidthZoom && spec.lineWidthZoom.length >= 2)
        ? _applyRulesToZoomStops(spec.lineWidthZoom, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.lineWidth) : null; })
        : _applyRulesToPaintValue(lineWidthBase, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.lineWidth) : null; }),
      lineDash: LINE_DASH[spec.linePattern || 'solid'] || LINE_DASH.solid,
      fillEnabled: fillEnabled,
      fillVisible: fillEnabled,
      lineEnabled: lineEnabled,
      lineVisible: lineEnabled,
      pointEnabled: pointEnabled,
      pointVisible: pointEnabled,
      pointColor: _applyRulesToPaintValue(pointColorBase, rules, function(r) { return r && r.paint ? r.paint.pointColor : null; }),
      pointRadius: (spec.pointSizeZoom && spec.pointSizeZoom.length >= 2)
        ? _applyRulesToZoomStops(spec.pointSizeZoom, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.pointSize) : null; })
        : _applyRulesToPaintValue(pointRadiusBase, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.pointSize) : null; }),
      pointOpacity: (spec.pointOpacityZoom && spec.pointOpacityZoom.length >= 2)
        ? _applyRulesToZoomStops(spec.pointOpacityZoom, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.pointOpacity) : null; })
        : _applyRulesToPaintValue(pointOpacityBase, rules, function(r) { return r && r.paint ? _toNumberOrNull(r.paint.pointOpacity) : null; }),
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
   * Get the display label for a feature (for popup list or header). Uses displayIdProperty if set.
   */
  function getFeatureDisplayLabel(feat, displayIdProperty, fallbackIdx) {
    var props = feat.properties || {};
    if (displayIdProperty && props[displayIdProperty] != null) return String(props[displayIdProperty]);
    return props.id != null ? String(props.id) : (feat.id != null ? String(feat.id) : (fallbackIdx != null ? 'Feature ' + (fallbackIdx + 1) : '—'));
  }

  /**
   * Human-readable label for special popup layer ids (e.g. style editor sketch).
   */
  function layerDisplayName(collectionId) {
    if (collectionId === '__editor__') return 'Editor sketch';
    return collectionId;
  }

  /**
   * Build popup HTML for a vector tile feature.
   * @param {Object} feat - Feature with properties and optionally id
   * @param {string} base - Base URL (e.g. window.location.origin + path prefix)
   * @param {string} collectionId - Collection id for the item link (optional; omit link if falsy)
   * @param {string|null} displayIdProperty - Optional property name to show as identifier (link text); when set, first line uses this property
   * @returns {string} HTML string for popup content
   */
  function popupHtmlForFeature(feat, base, collectionId, displayIdProperty) {
    var props = feat.properties || {};
    var id = props.id != null ? String(props.id) : (feat.id != null ? String(feat.id) : '—');
    var collectionUrl = collectionId
      ? (base + '/collections/' + encodeURIComponent(collectionId) + '?f=html')
      : '';
    var featureUrl = collectionId
      ? (base + '/collections/' + encodeURIComponent(collectionId) + '/items/' + encodeURIComponent(id) + '?f=html')
      : '';
    var displayLabel = displayIdProperty && props[displayIdProperty] != null ? String(props[displayIdProperty]) : id;
    var firstLabel = displayIdProperty ? escapeHtml(displayIdProperty) : 'ID';
    var idLine = featureUrl
      ? ('<a href="' + escapeHtml(featureUrl) + '">' + escapeHtml(displayLabel) + '</a>')
      : escapeHtml(displayLabel);
    var canEdit = false;
    try {
      if (typeof global !== 'undefined' && global._geofastmapPopupData && global._geofastmapPopupData.canEditByCollection && collectionId) {
        canEdit = !!global._geofastmapPopupData.canEditByCollection[collectionId];
      }
    } catch (err) {}
    var parts = ['<div class="map-popup">'];
    if (collectionId) {
      parts.push(
        '<div class="map-popup-row map-popup-layer"><strong>Layer</strong> ' +
        (collectionUrl ? ('<a href="' + escapeHtml(collectionUrl) + '"><code>' + escapeHtml(collectionId) + '</code></a>') : ('<code>' + escapeHtml(collectionId) + '</code>')) +
        '</div>'
      );
    }
    if (canEdit && collectionId && id !== '—') {
      parts.push(
        '<div class="map-popup-actions map-popup-actions--top">' +
        '<a href="#" class="map-popup-edit-link" data-action="geofastmap-edit-feature" data-collection="' +
        escapeHtml(collectionId) + '" data-feature-id="' + escapeHtml(id) + '">Edit feature</a></div>'
      );
    }
    parts.push('<div class="map-popup-id"><strong>' + firstLabel + '</strong> ' + idLine + '</div>');
    Object.keys(props).sort().forEach(function(k) {
      if (k === 'id') return;
      if (displayIdProperty && k === displayIdProperty) return;
      var v = props[k];
      var val = v === null || v === undefined ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
      parts.push('<div class="map-popup-row"><strong>' + escapeHtml(k) + '</strong> ' + escapeHtml(val) + '</div>');
    });
    parts.push('</div>');
    return parts.join('');
  }

  /**
   * Group features by collection id (deduped by layer+feature id). Used for layered popup.
   * @param {Array} features - queryRenderedFeatures result
   * @param {function(Object): string|null} getCollectionId - function(feat) -> collection id or null
   * @returns {{ byLayerOrder: string[], byLayer: Object.<string, Object[]> }}
   */
  function groupFeaturesByCollection(features, getCollectionId) {
    var byLayer = {};
    var byLayerOrder = [];
    var seen = {};
    for (var i = 0; i < features.length; i++) {
      var f = features[i];
      var cid = getCollectionId && getCollectionId(f);
      if (!cid) continue;
      var fid = (f.id != null ? f.id : (f.properties && f.properties.id != null ? f.properties.id : null));
      var key = cid + '|' + (fid != null ? fid : 'i' + i);
      if (seen[key]) continue;
      seen[key] = true;
      if (!byLayer[cid]) {
        byLayer[cid] = [];
        byLayerOrder.push(cid);
      }
      byLayer[cid].push(f);
    }
    return { byLayerOrder: byLayerOrder, byLayer: byLayer };
  }

  /**
   * Step 1: list of layers (collection ids) with counts. Links have data-action="layer" data-layer="<cid>".
   */
  function popupHtmlLayersStep(byLayerOrder, byLayer, base) {
    var parts = ['<div class="map-popup map-popup-step">', '<div class="map-popup-step-title">Layers</div>', '<ul class="map-popup-list">'];
    byLayerOrder.forEach(function(cid) {
      var count = byLayer[cid].length;
      var name = layerDisplayName(cid);
      var label = escapeHtml(name) + (count > 1 ? ' (' + count + ')' : '');
      parts.push('<li><a href="#" class="map-popup-link" data-action="layer" data-layer="' + escapeHtml(cid) + '">' + label + '</a></li>');
    });
    parts.push('</ul></div>');
    return parts.join('');
  }

  /**
   * Step 2: list of features in a layer. Links have data-action="feature" data-layer="<cid>" data-index="<i>".
   * @param {string|null} displayIdProperty - Optional property name to show as label for each feature
   */
  function popupHtmlFeaturesStep(collectionId, features, base, displayIdProperty) {
    var title = escapeHtml(layerDisplayName(collectionId));
    var parts = ['<div class="map-popup map-popup-step">', '<a href="#" class="map-popup-back" data-action="layers">← Back to layers</a>', '<div class="map-popup-step-title">' + title + '</div>', '<ul class="map-popup-list">'];
    features.forEach(function(feat, idx) {
      var label = getFeatureDisplayLabel(feat, displayIdProperty || null, idx);
      parts.push('<li><a href="#" class="map-popup-link" data-action="feature" data-layer="' + escapeHtml(collectionId) + '" data-index="' + idx + '">' + escapeHtml(String(label)) + '</a></li>');
    });
    parts.push('</ul></div>');
    return parts.join('');
  }

  /**
   * Step 3: single feature detail + back to features and back to layers.
   * @param {string|null} displayIdProperty - Optional property name to show as identifier in the detail
   */
  function popupHtmlFeatureStep(feat, collectionId, base, displayIdProperty) {
    var backFeatures = '<a href="#" class="map-popup-back" data-action="features" data-layer="' + escapeHtml(collectionId) + '">← Back to features</a>';
    var backLayers = '<a href="#" class="map-popup-back" data-action="layers">← Back to layers</a>';
    var cidForLink = (collectionId === '__editor__' || !collectionId) ? null : collectionId;
    return '<div class="map-popup map-popup-step">' + backFeatures + ' ' + backLayers + '<div class="map-popup-step-body">' + popupHtmlForFeature(feat, base, cidForLink, displayIdProperty || null) + '</div></div>';
  }

  /**
   * Map click: query all interactive vector layers at the point, then show grouped popups (like map view).
   * @param {object} map - MapLibre map
   * @param {object} options
   * @param {function(): string[]} options.getLayerIds - returns map layer ids (only existing layers are used)
   * @param {function(object): string|null} options.getCollectionId - MapLibre feature -> collection id (or "__editor__" for editor sketch)
   * @param {string} options.base - API base URL
   * @param {Object.<string,string>} [options.displayIdByLayer] - optional property name per collection for popup labels
   * @param {function(): Object.<string,string>} [options.getDisplayIdByLayer] - if set, called on each click to resolve labels (overrides displayIdByLayer snapshot)
   * @param {function(Array, object, object): string} [options.customRender] - if set, receives (features, lngLat, map) and returns HTML; skips feature grouping
   */
  function attachMultiLayerFeaturePopup(map, options) {
    if (!map || !options) return;
    if (map.__geofastmapMultiLayerPopupBound) return;
    map.__geofastmapMultiLayerPopupBound = true;
    var getLayerIds = options.getLayerIds;
    var getCollectionId = options.getCollectionId;
    var base = options.base || '';
    var displayIdByLayer = options.displayIdByLayer || {};
    var getDisplayIdByLayer = options.getDisplayIdByLayer;
    var customRender = options.customRender;
    var beforePopup = options.beforePopup;
    var canEditByCollection = options.canEditByCollection || {};

    function resolveDisplayIdByLayer() {
      if (typeof getDisplayIdByLayer === 'function') {
        try {
          var o = getDisplayIdByLayer();
          return o && typeof o === 'object' ? o : {};
        } catch (e) {
          return {};
        }
      }
      return displayIdByLayer;
    }

    function bindListeners() {
      function filteredLayerIds() {
        var ids = (getLayerIds && getLayerIds()) || [];
        return ids.filter(function(id) {
          try {
            return map.getLayer(id);
          } catch (e) {
            return false;
          }
        });
      }

      map.on('mousemove', function(e) {
        var layerIds = filteredLayerIds();
        var hit = layerIds.length > 0 && (function() {
          try {
            return map.queryRenderedFeatures(e.point, { layers: layerIds }).length > 0;
          } catch (err) {
            return false;
          }
        })();
        map.getCanvas().style.cursor = hit ? 'pointer' : '';
      });

      setupLayeredPopupNavigation();

      map.on('click', function(e) {
        if (typeof beforePopup === 'function') {
          try {
            if (beforePopup(e, map) === false) return;
          } catch (err) {}
        }
        var layerIds = filteredLayerIds();
        if (layerIds.length === 0) return;
        var features;
        try {
          features = map.queryRenderedFeatures(e.point, { layers: layerIds });
        } catch (err) {
          return;
        }
        if (!features || features.length === 0) return;

        if (customRender) {
          var htmlCustom = customRender(features, e.lngLat, map);
          if (!htmlCustom) return;
          if (window._geofastmapPopup) {
            try {
              window._geofastmapPopup.remove();
            } catch (err) {}
          }
          var popupC = new maplibregl.Popup({ closeButton: true, closeOnClick: true, maxWidth: '420px' });
          popupC.setLngLat(e.lngLat).setHTML(htmlCustom).addTo(map);
          window._geofastmapPopup = popupC;
          return;
        }

        if (!getCollectionId) return;

        function getCid(f) {
          return getCollectionId(f);
        }

        var grouped = groupFeaturesByCollection(features, getCid);
        if (grouped.byLayerOrder.length === 0) return;

        if (window._geofastmapPopup) {
          try {
            window._geofastmapPopup.remove();
          } catch (err) {}
        }

        var dispMap = resolveDisplayIdByLayer();
        window._geofastmapPopupData = {
          byLayerOrder: grouped.byLayerOrder,
          byLayer: grouped.byLayer,
          base: base,
          displayIdByLayer: dispMap,
          canEditByCollection: canEditByCollection
        };
        var popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, maxWidth: '420px' });
        var html;
        if (grouped.byLayerOrder.length > 1) {
          html = popupHtmlLayersStep(grouped.byLayerOrder, grouped.byLayer, base);
        } else {
          var cid0 = grouped.byLayerOrder[0];
          var feats = grouped.byLayer[cid0];
          var dispId = dispMap[cid0] || null;
          if (feats.length > 1) {
            html = popupHtmlFeaturesStep(cid0, feats, base, dispId);
          } else {
            html = popupHtmlFeatureStep(feats[0], cid0, base, dispId);
          }
        }
        popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
        window._geofastmapPopup = popup;
      });
    }

    if (typeof map.loaded === 'function' && map.loaded()) {
      bindListeners();
    } else {
      map.once('load', bindListeners);
    }
  }

  /**
   * Setup delegated click handler for layered popup navigation. Call once (e.g. on map load).
   * Expects window._geofastmapPopup (popup instance) and window._geofastmapPopupData = { byLayerOrder, byLayer, base, displayIdByLayer }.
   * displayIdByLayer: optional { collectionId: propertyName } for popup identifier per layer.
   */
  function setupLayeredPopupNavigation() {
    if (document._geofastmapLayeredPopupSetup) return;
    document._geofastmapLayeredPopupSetup = true;
    document.addEventListener('click', function(e) {
      var link = e.target && (e.target.closest ? e.target.closest('a[data-action]') : (e.target.tagName === 'A' && e.target.getAttribute('data-action') ? e.target : null));
      if (!link || !link.getAttribute('data-action')) return;
      var popupContent = e.target.closest ? e.target.closest('.maplibregl-popup-content') : null;
      if (!popupContent) return;
      var actionEarly = link.getAttribute('data-action');
      if (actionEarly === 'geofastmap-edit-feature') return;
      e.preventDefault();
      var popup = window._geofastmapPopup;
      var data = window._geofastmapPopupData;
      if (!popup || !data || !data.byLayer || !data.base) return;
      var action = link.getAttribute('data-action');
      var layer = link.getAttribute('data-layer');
      var indexStr = link.getAttribute('data-index');
      var displayIdByLayer = data.displayIdByLayer || {};
      var displayId = layer ? (displayIdByLayer[layer] || null) : null;
      var html = null;
      if (action === 'layers') {
        html = popupHtmlLayersStep(data.byLayerOrder, data.byLayer, data.base);
      } else if (action === 'layer' && layer && data.byLayer[layer]) {
        html = popupHtmlFeaturesStep(layer, data.byLayer[layer], data.base, displayId);
      } else if (action === 'feature' && layer && data.byLayer[layer] && indexStr !== null) {
        var idx = parseInt(indexStr, 10);
        if (!isNaN(idx) && data.byLayer[layer][idx]) {
          html = popupHtmlFeatureStep(data.byLayer[layer][idx], layer, data.base, displayId);
        }
      } else if (action === 'features' && layer && data.byLayer[layer]) {
        html = popupHtmlFeaturesStep(layer, data.byLayer[layer], data.base, displayId);
      }
      if (html) {
        try {
          if (typeof popup.isOpen === 'function' && !popup.isOpen()) return;
          popup.setHTML(html);
        } catch (err) { /* popup may have been removed */ }
      }
    });
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

  /**
   * Attach fullscreen toggle to the map. Call once when the map is ready.
   * Uses #map-fullscreen-btn and the map container's parent as the fullscreen target.
   * @param {object} map - MapLibre map instance
   * @param {string} [buttonId] - Optional button id (default 'map-fullscreen-btn')
   */
  function setupFullscreenForMap(map, buttonId) {
    if (!map || !map.getContainer) return;
    var bid = buttonId || 'map-fullscreen-btn';
    var btn = document.getElementById(bid);
    var container = map.getContainer();
    var wrapper = container && container.parentElement;
    if (!btn || !wrapper) return;
    function updateLabel() {
      btn.textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Fullscreen';
    }
    document.addEventListener('fullscreenchange', function() {
      updateLabel();
      if (map && typeof map.resize === 'function') map.resize();
    });
    btn.onclick = function() {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(function() {});
      } else {
        wrapper.requestFullscreen().catch(function() {});
      }
    };
  }

  /** Element84 Earth Search (and compatible) STAC API roots. */
  function isEarthSearchStacUrl(url) {
    if (!url) return false;
    return String(url).toLowerCase().indexOf('earth-search') !== -1;
  }

  var DEFAULT_EARTH_SEARCH_COLLECTION_ID = 'sentinel-2-l2a';

  global.GeofastmapUtils = {
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
    setPaintPropertySafe: setPaintPropertySafe,
    groupFeaturesByCollection: groupFeaturesByCollection,
    popupHtmlLayersStep: popupHtmlLayersStep,
    popupHtmlFeaturesStep: popupHtmlFeaturesStep,
    popupHtmlFeatureStep: popupHtmlFeatureStep,
    layerDisplayName: layerDisplayName,
    attachMultiLayerFeaturePopup: attachMultiLayerFeaturePopup,
    setupLayeredPopupNavigation: setupLayeredPopupNavigation,
    setupFullscreenForMap: setupFullscreenForMap,
    setTerrainAtmosphere: setTerrainAtmosphere,
    clearTerrainAtmosphere: clearTerrainAtmosphere,
    isEarthSearchStacUrl: isEarthSearchStacUrl,
    DEFAULT_EARTH_SEARCH_COLLECTION_ID: DEFAULT_EARTH_SEARCH_COLLECTION_ID
  };
})(typeof window !== 'undefined' ? window : this);
