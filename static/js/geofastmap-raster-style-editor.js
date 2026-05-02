/**
 * Raster collection Titiler style controls for the collection edit map (same tile URL builder as STAC studio).
 * GET /collections/{id}/rasters/tiles/WebMercatorQuad/...
 */
(function (global) {
  var _cfg = null;

  function el(id) {
    return document.getElementById(id);
  }

  function getMap() {
    return _cfg && _cfg.map;
  }

  function tileAssetKeysAll() {
    var sel = el('stac-asset-select');
    if (!sel) return [];
    var out = [];
    for (var i = 0; i < sel.options.length; i++) {
      var v = sel.options[i].value;
      if (v && v !== '__mosaic__') out.push(v);
    }
    return out;
  }

  function substituteAssetKeysInExpression(expr, tileKeys) {
    if (!expr || !tileKeys || !tileKeys.length) return null;
    var sorted = tileKeys.slice().sort(function (a, b) {
      return b.length - a.length;
    });
    var used = [];
    var seen = {};
    var out = '';
    var i = 0;
    while (i < expr.length) {
      var atBoundary = i === 0 || !/[A-Za-z0-9_-]/.test(expr[i - 1]);
      var matchedKey = null;
      var matchedLen = 0;
      if (atBoundary) {
        for (var j = 0; j < sorted.length; j++) {
          var k = sorted[j];
          if (expr.slice(i, i + k.length) !== k) continue;
          var end = i + k.length;
          var cend = expr[end];
          if (cend !== undefined && /[A-Za-z0-9_-]/.test(cend)) continue;
          matchedKey = k;
          matchedLen = k.length;
          break;
        }
      }
      if (matchedKey) {
        if (!seen[matchedKey]) {
          seen[matchedKey] = used.length + 1;
          used.push(matchedKey);
        }
        out += 'b' + seen[matchedKey];
        i += matchedLen;
      } else {
        out += expr[i];
        i++;
      }
    }
    if (used.length === 0) return null;
    return { assets: used, expr: out };
  }

  function substituteBandNamesInExpression(expr, assetKey) {
    return expr;
  }

  function titilerExpressionValidationError(normalizedExpr, opts) {
    opts = opts || {};
    if (!normalizedExpr || !String(normalizedExpr).trim()) return null;
    var s = String(normalizedExpr).trim();
    var stripped = s;
    stripped = stripped.replace(/\bb\d+\b/gi, ' ');
    stripped = stripped.replace(/\d+\.?\d*([eE][+-]?\d+)?/g, ' ');
    stripped = stripped.replace(/[+\-*/(),]/g, ' ');
    stripped = stripped.trim();
    if (stripped) {
      var parts = stripped.split(/\s+/).filter(Boolean);
      for (var i = 0; i < parts.length; i++) {
        var p = parts[i];
        if (/^[a-z_][a-z0-9_]*$/i.test(p)) {
          return 'Unresolved name "' + p + '". Use b1, b2, … for bands.';
        }
      }
    }
    var mac = opts.multiAssetCount;
    if (typeof mac === 'number' && mac > 0) {
      var reM = /\bb(\d+)\b/gi;
      var m;
      var maxB = 0;
      while ((m = reM.exec(s)) !== null) {
        var bi = parseInt(m[1], 10);
        if (bi > maxB) maxB = bi;
      }
      if (maxB > mac) {
        return 'Expression uses b' + maxB + ' but only ' + mac + ' asset layer(s) are stacked.';
      }
    }
    return null;
  }

  function normalizeExpressionForTitiler(raw, primaryAssetKey) {
    var keys = tileAssetKeysAll();
    var multi = substituteAssetKeysInExpression(raw, keys);
    if (multi && multi.assets.length >= 2) {
      return {
        path: 'unsupported',
        assets: multi.assets,
        normalizedExpr: multi.expr,
        multiAssetCount: multi.assets.length,
        cogAssetKey: primaryAssetKey,
        unsupportedMultiItem: true,
      };
    }
    if (multi && multi.assets.length === 1) {
      var afterBand = substituteBandNamesInExpression(multi.expr, multi.assets[0]);
      return {
        path: 'cog',
        assets: null,
        normalizedExpr: afterBand,
        multiAssetCount: 0,
        cogAssetKey: multi.assets[0],
      };
    }
    var norm = substituteBandNamesInExpression(raw, primaryAssetKey);
    return {
      path: 'cog',
      assets: null,
      normalizedExpr: norm,
      multiAssetCount: 0,
      cogAssetKey: primaryAssetKey,
    };
  }

  function collectionTileTemplate() {
    return (
      _cfg.base +
      '/collections/' +
      encodeURIComponent(_cfg.collectionId) +
      '/rasters/tiles/WebMercatorQuad/{z}/{x}/{y}.png'
    );
  }

  function buildCollectionTileUrl(assetKey) {
    var path = collectionTileTemplate();
    var qs = [];
    if (assetKey === '__mosaic__') {
      qs.push('mode=mosaic');
      if (_cfg.mosaicVersionId) qs.push('mv=' + encodeURIComponent(_cfg.mosaicVersionId));
    } else {
      qs.push('mode=item');
      qs.push('feature_id=' + encodeURIComponent(assetKey));
    }

    var rescaleEl = el('stac-rescale');
    var rescaleVal = rescaleEl && rescaleEl.value.trim() ? rescaleEl.value.trim() : '';
    function pushRescaleOnce() {
      if (rescaleVal) qs.push('rescale=' + encodeURIComponent(rescaleVal));
    }
    function pushRescaleRgb() {
      if (!rescaleVal) return;
      var enc = encodeURIComponent(rescaleVal);
      qs.push('rescale=' + enc);
      qs.push('rescale=' + enc);
      qs.push('rescale=' + enc);
    }

    var cf = el('stac-color-formula');
    if (cf && cf.value.trim()) qs.push('color_formula=' + encodeURIComponent(cf.value.trim()));

    function appendColormapName(qsArr) {
      var cm = el('stac-colormap');
      if (cm && cm.value) qsArr.push('colormap_name=' + encodeURIComponent(cm.value));
    }

    var modeEl = el('stac-render-mode');
    var mode = modeEl ? modeEl.value : 'rgb_bands';

    if (mode === 'expression') {
      var ex = el('stac-expression');
      if (ex && ex.value.trim()) {
        var rawEx = ex.value.trim();
        var n = normalizeExpressionForTitiler(rawEx, assetKey);
        if (n.unsupportedMultiItem) {
          return path + '?' + qs.join('&');
        }
        var vopts = n.multiAssetCount
          ? { multiAssetCount: n.multiAssetCount }
          : { assetKey: n.cogAssetKey };
        if (!titilerExpressionValidationError(n.normalizedExpr, vopts)) {
          qs.push('expression=' + encodeURIComponent(n.normalizedExpr));
        }
      }
      pushRescaleOnce();
      appendColormapName(qs);
    } else if (mode === 'single') {
      var s = el('stac-band-single');
      if (s && s.value) qs.push('bidx=' + encodeURIComponent(String(s.value)));
      pushRescaleOnce();
      appendColormapName(qs);
    } else if (mode === 'rgb_assets') {
      var ar = el('stac-asset-r');
      var ag = el('stac-asset-g');
      var ab = el('stac-asset-b');
      if (ar && ar.value) qs.push('assets=' + encodeURIComponent(String(ar.value)));
      if (ag && ag.value) qs.push('assets=' + encodeURIComponent(String(ag.value)));
      if (ab && ab.value) qs.push('assets=' + encodeURIComponent(String(ab.value)));
      qs.push('asset_as_band=true');
      pushRescaleRgb();
    } else {
      var br = el('stac-band-r');
      var bg = el('stac-band-g');
      var bb = el('stac-band-b');
      if (br && br.value) qs.push('bidx=' + encodeURIComponent(String(br.value)));
      if (bg && bg.value) qs.push('bidx=' + encodeURIComponent(String(bg.value)));
      if (bb && bb.value) qs.push('bidx=' + encodeURIComponent(String(bb.value)));
      pushRescaleRgb();
    }
    return qs.length ? path + '?' + qs.join('&') : path;
  }

  function removeRasterLayer() {
    var map = getMap();
    if (!map) return;
    var lid = _cfg.layerId;
    var sid = _cfg.sourceId;
    if (map.getLayer(lid)) map.removeLayer(lid);
    if (map.getSource(sid)) map.removeSource(sid);
  }

  function getRasterOpacity01() {
    var op = el('stac-raster-opacity');
    if (!op) return 1;
    var v = parseInt(op.value, 10);
    if (!isFinite(v)) return 1;
    return Math.max(0, Math.min(1, v / 100));
  }

  function addOrRefreshRaster() {
    if (!_cfg || !_cfg.titilerConfigured || !_cfg.tileAssetsLen) return;
    var map = getMap();
    if (!map || !map.loaded()) return;
    syncColorFormula();
    updateExpressionPreview();
    var sel = el('stac-asset-select');
    if (!sel || !sel.value) return;
    var assetKey = sel.value;
    var modeEl = el('stac-render-mode');
    var mode = modeEl ? modeEl.value : 'rgb_bands';
    if (mode === 'expression') {
      var exEl = el('stac-expression');
      if (exEl && exEl.value.trim()) {
        var nEx = normalizeExpressionForTitiler(exEl.value.trim(), assetKey);
        if (nEx.unsupportedMultiItem) {
          removeRasterLayer();
          return;
        }
        var voptsEx = nEx.multiAssetCount
          ? { multiAssetCount: nEx.multiAssetCount }
          : { assetKey: nEx.cogAssetKey };
        if (titilerExpressionValidationError(nEx.normalizedExpr, voptsEx)) {
          removeRasterLayer();
          return;
        }
      }
    }
    var tileUrl = buildCollectionTileUrl(assetKey);
    var srcId = _cfg.sourceId;
    var layerId = _cfg.layerId;
    if (map.getSource(srcId)) {
      map.getSource(srcId).setTiles([tileUrl]);
    } else {
      map.addSource(srcId, {
        type: 'raster',
        tiles: [tileUrl],
        tileSize: 256,
        maxzoom: 18,
        attribution: 'Titiler',
      });
      var beforeId;
      try {
        var layers = map.getStyle().layers || [];
        for (var li = 0; li < layers.length; li++) {
          if (layers[li].id === 'tiles-fill' || layers[li].id === 'tiles-line' || layers[li].id === 'tiles-circle') {
            beforeId = layers[li].id;
            break;
          }
        }
      } catch (e1) {}
      map.addLayer(
        {
          id: layerId,
          type: 'raster',
          source: srcId,
          paint: { 'raster-opacity': 0.9 },
        },
        beforeId
      );
    }
    var roPaint = getRasterOpacity01();
    if (map.getLayer(layerId) && isFinite(roPaint)) map.setPaintProperty(layerId, 'raster-opacity', roPaint);
  }

  function maybeRefresh() {
    addOrRefreshRaster();
  }

  function bandTokenCf() {
    var r = el('stac-cf-band-r');
    var g = el('stac-cf-band-g');
    var b = el('stac-cf-band-b');
    if (!r || !g || !b) return '';
    return (r.checked ? 'r' : '') + (g.checked ? 'g' : '') + (b.checked ? 'b' : '');
  }

  function syncColorFormula() {
    var hidden = el('stac-color-formula');
    var preview = el('stac-cf-preview');
    if (!hidden) return;
    var modeEl = document.querySelector('input[name="stac-cf-mode"]:checked');
    var mode = modeEl ? modeEl.value : 'builder';
    if (mode === 'custom') {
      var cu = el('stac-cf-custom');
      hidden.value = cu && cu.value.trim() ? cu.value.trim() : '';
      if (preview) preview.textContent = hidden.value ? 'Sent to Titiler: ' + hidden.value : 'No color formula.';
      return;
    }
    var incG = el('stac-cf-include-gamma');
    var incS = el('stac-cf-include-sigmoidal');
    var incSat = el('stac-cf-include-sat');
    var gOn = incG && incG.checked;
    var sOn = incS && incS.checked;
    var satOn = incSat && incSat.checked;
    var bands = bandTokenCf();
    var needBands = gOn || sOn;
    if (needBands && !bands) {
      hidden.value = '';
      if (preview) preview.textContent = 'Gamma and/or sigmoidal enabled: select at least one band (R, G, or B).';
      return;
    }
    var parts = [];
    if (gOn) {
      var gv = el('stac-cf-gamma-val');
      parts.push('gamma ' + bands + ' ' + (gv ? gv.value : '1'));
    }
    if (sOn) {
      var st = el('stac-cf-sig-str');
      var bi = el('stac-cf-sig-bias');
      parts.push('sigmoidal ' + bands + ' ' + (st ? st.value : '0') + ' ' + (bi ? bi.value : '0'));
    }
    if (satOn) {
      var sv = el('stac-cf-sat-val');
      parts.push('saturation ' + (sv ? String(sv.value) : '1'));
    }
    hidden.value = parts.join(', ');
    if (preview) {
      if (!hidden.value) preview.textContent = 'No color formula (enable steps below, or use Custom text).';
      else preview.textContent = 'Sent to Titiler: ' + hidden.value;
    }
  }

  function setCfModeUi(mode) {
    var bw = el('stac-cf-builder-wrap');
    var cw = el('stac-cf-custom-wrap');
    if (bw) bw.style.display = mode === 'builder' ? 'block' : 'none';
    if (cw) cw.style.display = mode === 'custom' ? 'block' : 'none';
    syncColorFormula();
    maybeRefresh();
  }

  function initColorFormulaPanel() {
    function cfMaybeRefresh() {
      syncColorFormula();
      maybeRefresh();
    }
    var mb = el('stac-cf-mode-builder');
    var mc = el('stac-cf-mode-custom');
    if (mb)
      mb.addEventListener('change', function () {
        if (mb.checked) setCfModeUi('builder');
      });
    if (mc)
      mc.addEventListener('change', function () {
        if (mc.checked) setCfModeUi('custom');
      });
    ['stac-cf-include-gamma', 'stac-cf-include-sigmoidal', 'stac-cf-include-sat'].forEach(function (id) {
      var e = el(id);
      if (e) e.addEventListener('change', cfMaybeRefresh);
    });
    ['stac-cf-gamma-val', 'stac-cf-sig-str', 'stac-cf-sig-bias', 'stac-cf-sat-val'].forEach(function (id) {
      var e = el(id);
      if (!e) return;
      e.addEventListener('input', syncColorFormula);
      e.addEventListener('change', cfMaybeRefresh);
    });
    var cu = el('stac-cf-custom');
    if (cu) {
      cu.addEventListener('input', syncColorFormula);
      cu.addEventListener('change', cfMaybeRefresh);
    }
    ['stac-cf-band-r', 'stac-cf-band-g', 'stac-cf-band-b'].forEach(function (id) {
      var e = el(id);
      if (e) e.addEventListener('change', cfMaybeRefresh);
    });
    syncColorFormula();
  }

  function populateBandSelect(selectEl, chosenValue) {
    if (!selectEl) return;
    var prev = chosenValue || selectEl.value;
    selectEl.innerHTML = '';
    var max = 12;
    for (var j = 1; j <= max; j++) {
      var o2 = document.createElement('option');
      o2.value = String(j);
      o2.textContent = 'b' + String(j);
      selectEl.appendChild(o2);
    }
    if (prev && selectEl.querySelector('option[value="' + prev + '"]')) selectEl.value = prev;
  }

  function refreshBandSelectors() {
    populateBandSelect(el('stac-band-r'), '1');
    populateBandSelect(el('stac-band-g'), '2');
    populateBandSelect(el('stac-band-b'), '3');
    populateBandSelect(el('stac-band-single'), '1');
  }

  function applyRescaleDefaultForMode(mode) {
    var rsc = el('stac-rescale');
    if (!rsc) return;
    if (mode === 'expression') rsc.value = '-1,1';
    else if (mode === 'rgb_bands') rsc.value = '';
    else rsc.value = '0,4000';
  }

  function setMode(mode, opts) {
    opts = opts || {};
    var rgbBands = el('stac-rgb-bands-block');
    var rgbAssets = el('stac-rgb-assets-block');
    var single = el('stac-single-block');
    var expr = el('stac-expr-block');
    var cmapBlock = el('stac-colormap-block');
    function show(elx, on) {
      if (!elx) return;
      elx.classList.toggle('is-open', !!on);
    }
    show(rgbBands, mode === 'rgb_bands');
    show(rgbAssets, mode === 'rgb_assets');
    show(single, mode === 'single');
    show(expr, mode === 'expression');
    show(cmapBlock, mode === 'single' || mode === 'expression');
    if (!opts.skipRescaleDefault) applyRescaleDefaultForMode(mode);
    updateExpressionPreview();
  }

  function updateExpressionPreview() {
    var ex = el('stac-expression');
    var prev = el('stac-expression-preview');
    var sel = el('stac-asset-select');
    if (!prev) return;
    prev.style.color = '';
    if (!ex || !sel || !ex.value.trim()) {
      prev.textContent = '';
      return;
    }
    var raw = ex.value.trim();
    var n = normalizeExpressionForTitiler(raw, sel.value);
    if (n.unsupportedMultiItem) {
      prev.style.color = 'var(--danger)';
      prev.textContent =
        'Multi-item expressions across raster items are not supported here; use one COG item or a single-band expression.';
      return;
    }
    var vopts = n.multiAssetCount ? { multiAssetCount: n.multiAssetCount } : { assetKey: n.cogAssetKey };
    var err = titilerExpressionValidationError(n.normalizedExpr, vopts);
    var sub = n.normalizedExpr;
    if (err) {
      prev.style.color = 'var(--danger)';
      prev.textContent = err;
      return;
    }
    prev.textContent = 'Normalized: ' + sub;
  }

  function populateRgbAssetSelects() {
    var rSel = el('stac-asset-r');
    var gSel = el('stac-asset-g');
    var bSel = el('stac-asset-b');
    if (!rSel || !gSel || !bSel) return;
    var keys = tileAssetKeysAll();
    function fill(sel) {
      sel.innerHTML = '';
      keys.forEach(function (k) {
        var opt = document.createElement('option');
        opt.value = k;
        opt.textContent = k;
        sel.appendChild(opt);
      });
    }
    fill(rSel);
    fill(gSel);
    fill(bSel);
    if (keys[0]) rSel.value = keys[0];
    if (keys[1]) gSel.value = keys[1];
    if (keys[2]) bSel.value = keys[2];
  }

  function exportRasterStyleSpec() {
    var spec = {};
    var mode = el('stac-render-mode').value;
    var mainSel = el('stac-asset-select');
    var mainKey = mainSel && mainSel.value;
    if (mainKey) spec.asset = mainKey;
    var rescale = el('stac-rescale').value.trim();
    if (rescale) spec.rescale = rescale.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
    var cm = el('stac-colormap');
    if (cm && cm.value) spec.colormap_name = cm.value;
    var cf = el('stac-color-formula');
    if (cf && cf.value.trim()) spec.color_formula = cf.value.trim();

    if (mode === 'expression') {
      var ex = el('stac-expression');
      if (ex && ex.value.trim()) {
        var n = normalizeExpressionForTitiler(ex.value.trim(), mainKey);
        if (!n.unsupportedMultiItem) spec.expression = n.normalizedExpr;
      }
    } else if (mode === 'single') {
      var b = el('stac-band-single');
      if (b && b.value) spec.bidx = [String(b.value)];
    } else if (mode === 'rgb_assets') {
      var ar = el('stac-asset-r');
      var ag = el('stac-asset-g');
      var ab = el('stac-asset-b');
      var trip = [];
      if (ar && ar.value) trip.push(String(ar.value));
      if (ag && ag.value) trip.push(String(ag.value));
      if (ab && ab.value) trip.push(String(ab.value));
      if (trip.length) spec.assets = trip;
    } else if (mode === 'rgb_bands') {
      var br = el('stac-band-r');
      var bg = el('stac-band-g');
      var bb = el('stac-band-b');
      var parts = [];
      if (br && br.value) parts.push(String(br.value));
      if (bg && bg.value) parts.push(String(bg.value));
      if (bb && bb.value) parts.push(String(bb.value));
      if (parts.length) spec.bidx = parts;
    }
    return spec;
  }

  function applySpec(spec) {
    if (!spec || typeof spec !== 'object') return;
    var aSel = el('stac-asset-select');
    if (spec.asset && aSel) {
      for (var ai = 0; ai < aSel.options.length; ai++) {
        if (aSel.options[ai].value === spec.asset) {
          aSel.value = spec.asset;
          break;
        }
      }
    }
    if (spec.rescale) {
      var rsc = el('stac-rescale');
      if (rsc) rsc.value = Array.isArray(spec.rescale) ? spec.rescale.join(',') : String(spec.rescale);
    }
    if (spec.colormap_name && el('stac-colormap')) el('stac-colormap').value = spec.colormap_name;
    if (spec.color_formula && el('stac-cf-custom')) {
      el('stac-cf-custom').value = spec.color_formula;
      var mc = el('stac-cf-mode-custom');
      if (mc) mc.checked = true;
      setCfModeUi('custom');
    }
    if (spec.expression && el('stac-expression')) {
      el('stac-render-mode').value = 'expression';
      el('stac-expression').value = spec.expression;
      setMode('expression', { skipRescaleDefault: true });
    } else if (spec.assets && Array.isArray(spec.assets) && spec.assets.length >= 3) {
      el('stac-render-mode').value = 'rgb_assets';
      setMode('rgb_assets', { skipRescaleDefault: true });
      populateRgbAssetSelects();
      if (el('stac-asset-r')) el('stac-asset-r').value = spec.assets[0];
      if (el('stac-asset-g')) el('stac-asset-g').value = spec.assets[1];
      if (el('stac-asset-b')) el('stac-asset-b').value = spec.assets[2];
    } else if (spec.bidx && Array.isArray(spec.bidx)) {
      if (spec.bidx.length >= 3) {
        el('stac-render-mode').value = 'rgb_bands';
        setMode('rgb_bands', { skipRescaleDefault: true });
        refreshBandSelectors();
        if (el('stac-band-r')) el('stac-band-r').value = String(spec.bidx[0]);
        if (el('stac-band-g')) el('stac-band-g').value = String(spec.bidx[1]);
        if (el('stac-band-b')) el('stac-band-b').value = String(spec.bidx[2]);
      } else if (spec.bidx.length === 1) {
        el('stac-render-mode').value = 'single';
        setMode('single', { skipRescaleDefault: true });
        refreshBandSelectors();
        if (el('stac-band-single')) el('stac-band-single').value = String(spec.bidx[0]);
      }
    }
    syncColorFormula();
    updateExpressionPreview();
    refreshBandSelectors();
    maybeRefresh();
  }

  function init(config) {
    if (!config || !config.map || !config.collectionId) return;
    _cfg = {
      map: config.map,
      base: config.base,
      collectionId: config.collectionId,
      mosaicVersionId: config.mosaicVersionId || '',
      titilerConfigured: !!config.titilerConfigured,
      tileAssetsLen: config.tileAssetsLen || 0,
      sourceId: config.sourceId || 'collection-raster-underlay',
      layerId: config.layerId || 'collection-raster-underlay',
    };

    initColorFormulaPanel();
    populateRgbAssetSelects();
    refreshBandSelectors();

    var modeEl0 = el('stac-render-mode');
    if (modeEl0) {
      setMode(modeEl0.value, {});
      modeEl0.addEventListener('change', function () {
        setMode(this.value, {});
        maybeRefresh();
      });
    }

    ['stac-band-r', 'stac-band-g', 'stac-band-b', 'stac-band-single', 'stac-expression', 'stac-rescale', 'stac-colormap', 'stac-asset-r', 'stac-asset-g', 'stac-asset-b'].forEach(function (id) {
      var e = el(id);
      if (!e) return;
      e.addEventListener('change', function () {
        maybeRefresh();
      });
    });
    var stacExprEl = el('stac-expression');
    if (stacExprEl)
      stacExprEl.addEventListener('input', function () {
        updateExpressionPreview();
      });

    var assetSel = el('stac-asset-select');
    if (assetSel)
      assetSel.addEventListener('change', function () {
        refreshBandSelectors();
        updateExpressionPreview();
        maybeRefresh();
      });

    var applyBtn = el('stac-apply-raster');
    if (applyBtn) applyBtn.addEventListener('click', function () { maybeRefresh(); });

    var opEl = el('stac-raster-opacity');
    if (opEl)
      opEl.addEventListener('input', function () {
        var map = getMap();
        var lid = _cfg.layerId;
        if (map && map.getLayer(lid)) {
          map.setPaintProperty(lid, 'raster-opacity', parseInt(this.value, 10) / 100);
        }
      });

    var basemapSel = document.getElementById('map-basemap');
    if (basemapSel)
      basemapSel.addEventListener('change', function () {
        window.setTimeout(maybeRefresh, 50);
      });

    function onMapReady() {
      maybeRefresh();
    }
    if (_cfg.map.loaded()) onMapReady();
    else _cfg.map.once('load', onMapReady);
  }

  global.GeofastmapRasterStyleEditor = {
    init: init,
    getSpec: function () {
      return { style_spec: exportRasterStyleSpec() };
    },
    applySpec: applySpec,
    refreshLayer: maybeRefresh,
  };
  global.__geofastExportRasterStyleSpec = function () {
    return { style_spec: exportRasterStyleSpec() };
  };
})(typeof window !== 'undefined' ? window : this);
