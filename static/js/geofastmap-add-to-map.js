/**
 * Add collection layers to saved maps (vector dynamic tiles or raster mosaic/item).
 */
(function (global) {
  'use strict';

  function el(id) {
    return document.getElementById(id);
  }

  function prefixId(prefix, suffix) {
    return prefix + '-' + suffix;
  }

  function loadMaps(base, selectEl) {
    if (!selectEl) return;
    selectEl.innerHTML = '<option value="">Loading…</option>';
    fetch(base + '/maps', { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var list = data.maps || [];
        selectEl.innerHTML = list.length ? '' : '<option value="">No maps yet</option>';
        list.forEach(function (m) {
          var opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.name || m.id;
          selectEl.appendChild(opt);
        });
      })
      .catch(function () {
        selectEl.innerHTML = '<option value="">Load failed</option>';
      });
  }

  function syncChoice(prefix) {
    var ch = document.querySelector('input[name="' + prefix + '-choice"]:checked');
    var isExisting = ch && ch.value === 'existing';
    var newWrap = el(prefixId(prefix, 'new-wrap'));
    var existingWrap = el(prefixId(prefix, 'existing-wrap'));
    if (newWrap) newWrap.style.display = isExisting ? 'none' : '';
    if (existingWrap) existingWrap.style.display = isExisting ? '' : 'none';
  }

  function showMsg(msgEl, text, isError) {
    if (!msgEl) return;
    msgEl.textContent = text || '';
    msgEl.style.color = isError ? 'var(--danger)' : '';
  }

  function createMap(base, name, layers) {
    return fetch(base + '/maps', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        name: name,
        description: null,
        thumbnail: null,
        definition: { layers: layers, bbox: null, basemap: null },
      }),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw j;
        return j;
      });
    });
  }

  function appendLayerToMap(base, mapId, layer) {
    return fetch(base + '/maps/' + encodeURIComponent(mapId), {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    })
      .then(function (r) {
        if (!r.ok) throw new Error('Map not found');
        return r.json();
      })
      .then(function (mapData) {
        var def = mapData.definition || {};
        var layers = Array.isArray(def.layers) ? def.layers.slice() : [];
        layer.order = layers.length;
        layers.push(layer);
        return fetch(base + '/maps/' + encodeURIComponent(mapId), {
          method: 'PUT',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({
            definition: { layers: layers, bbox: def.bbox || null, basemap: def.basemap || null },
          }),
        });
      })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw j; });
        return r.json();
      });
  }

  function fetchRasterInfo(base, collectionId) {
    return fetch(base + '/collections/' + encodeURIComponent(collectionId) + '/rasters', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    }).then(function (r) {
      if (!r.ok) throw new Error('Could not load raster metadata');
      return r.json();
    });
  }

  function buildRasterLayer(base, collectionId, info, featureId) {
    var cid = String(collectionId || '').trim();
    var fid = featureId ? String(featureId).trim() : '';
    var spec = { rasterOpacity: 1 };
    var isDem = info.collection_is_dem === true;
    var demEnc = info.collection_dem_encoding === 'terrarium' ? 'terrarium' : 'mapbox';

    if (fid) {
      var tilesUrl =
        base +
        '/collections/' +
        encodeURIComponent(cid) +
        '/rasters/tiles/WebMercatorQuad/{z}/{x}/{y}.png?mode=item&feature_id=' +
        encodeURIComponent(fid);
      var hint = null;
      if (info.items && info.items.length) {
        for (var i = 0; i < info.items.length; i++) {
          if (String(info.items[i].id) === fid) {
            hint = info.items[i].map_layer || null;
            break;
          }
        }
      }
      return {
        collection_id: cid,
        layer_id: 'raster-item-' + cid + '-' + fid,
        order: 0,
        style_spec: spec,
        popup: false,
        popup_id_property: null,
        raster_tiles: true,
        raster_collection_mode: 'item',
        raster_feature_id: fid,
        tiles_url: tilesUrl,
        terrain_enabled: !!(hint && hint.terrain_enabled === true),
        terrain_exaggeration: hint && hint.terrain_enabled === true ? 1.0 : null,
        terrain_encoding: hint && hint.terrain_encoding ? hint.terrain_encoding : null,
        terrain_raster_overlay:
          hint && (hint.terrain_raster_overlay === true || hint.terrain_raster_overlay === false)
            ? hint.terrain_raster_overlay
            : hint && hint.terrain_enabled === true
              ? true
              : undefined,
      };
    }

    if (info.item_count > 1 || info.mosaic_tiles_url) {
      return {
        collection_id: cid,
        layer_id: 'raster-mosaic-' + cid,
        order: 0,
        style_spec: spec,
        popup: false,
        popup_id_property: null,
        raster_tiles: true,
        raster_collection_mode: 'mosaic',
        tiles_url: info.mosaic_tiles_url || null,
        terrain_enabled: isDem,
        terrain_encoding: demEnc,
        terrain_exaggeration: isDem ? 1.0 : null,
        terrain_raster_overlay: isDem ? true : undefined,
      };
    }

    if (info.items && info.items.length === 1) {
      return buildRasterLayer(base, cid, info, info.items[0].id);
    }
    throw new Error('No raster items in this collection');
  }

  /**
   * @param {object} opts
   * @param {string} opts.base
   * @param {string} [opts.prefix='add-to-map']
   * @param {string} opts.openButtonId
   * @param {function(): object|Promise<object>} opts.buildLayer
   * @param {function(): string} [opts.defaultMapName]
   */
  function bind(opts) {
    var base = opts.base;
    var prefix = opts.prefix || 'add-to-map';
    var openBtn = el(opts.openButtonId);
    if (!openBtn || !base || typeof opts.buildLayer !== 'function') return;

    var modal = el(prefixId(prefix, 'modal'));
    var backdrop = el(prefixId(prefix, 'backdrop'));
    var closeBtn = el(prefixId(prefix, 'close'));
    var cancelBtn = el(prefixId(prefix, 'cancel'));
    var submitBtn = el(prefixId(prefix, 'submit'));
    var nameEl = el(prefixId(prefix, 'name'));
    var selEl = el(prefixId(prefix, 'select'));
    var msgEl = el(prefixId(prefix, 'message'));
    if (!modal || !submitBtn) return;

    function closeModal() {
      modal.setAttribute('aria-hidden', 'true');
    }

    openBtn.onclick = function () {
      showMsg(msgEl, '');
      var defName = opts.defaultMapName ? opts.defaultMapName() : 'Map layer';
      if (nameEl) {
        nameEl.value = defName;
        nameEl.placeholder = defName;
      }
      var newRadio = document.querySelector('input[name="' + prefix + '-choice"][value="new"]');
      if (newRadio) newRadio.checked = true;
      syncChoice(prefix);
      loadMaps(base, selEl);
      modal.setAttribute('aria-hidden', 'false');
    };

    if (backdrop) backdrop.onclick = closeModal;
    if (closeBtn) closeBtn.onclick = closeModal;
    if (cancelBtn) cancelBtn.onclick = closeModal;

    document.querySelectorAll('input[name="' + prefix + '-choice"]').forEach(function (radio) {
      radio.onchange = function () {
        syncChoice(prefix);
      };
    });

    submitBtn.onclick = function () {
      var choice = document.querySelector('input[name="' + prefix + '-choice"]:checked');
      if (!choice) return;
      showMsg(msgEl, '');
      submitBtn.disabled = true;

      Promise.resolve()
        .then(function () {
          return opts.buildLayer();
        })
        .then(function (layer) {
          if (!layer) throw new Error('Could not build layer');
          if (choice.value === 'new') {
            var name = (nameEl && nameEl.value.trim()) || (opts.defaultMapName ? opts.defaultMapName() : 'Map');
            showMsg(msgEl, 'Creating map…');
            return createMap(base, name, [layer]).then(function (data) {
              window.location.href = base + '/maps/' + encodeURIComponent(data.id) + '/edit?f=html';
            });
          }
          var mapId = selEl && selEl.value ? String(selEl.value) : '';
          if (!mapId) {
            showMsg(msgEl, 'Select a map.', true);
            submitBtn.disabled = false;
            return;
          }
          showMsg(msgEl, 'Adding layer…');
          return appendLayerToMap(base, mapId, layer).then(function () {
            window.location.href = base + '/maps/' + encodeURIComponent(mapId) + '/edit?f=html';
          });
        })
        .catch(function (e) {
          showMsg(msgEl, 'Error: ' + (e && (e.detail || e.message)) ? e.detail || e.message : String(e), true);
          submitBtn.disabled = false;
        });
    };
  }

  /**
   * @param {object} opts
   * @param {string} opts.base
   * @param {string} opts.collectionId
   * @param {string} [opts.featureId] - single raster item mode
   * @param {string} opts.openButtonId
   * @param {function(): string} [opts.defaultMapName]
   */
  function bindRasterCollection(opts) {
    var base = opts.base;
    var collectionId = opts.collectionId;
    var featureId = opts.featureId || null;
    bind({
      base: base,
      prefix: opts.prefix || 'add-to-map',
      openButtonId: opts.openButtonId,
      defaultMapName: opts.defaultMapName,
      buildLayer: function () {
        return fetchRasterInfo(base, collectionId).then(function (info) {
          if (!info || !info.item_count) throw new Error('No raster items in this collection');
          return buildRasterLayer(base, collectionId, info, featureId);
        });
      },
    });
  }

  global.GeofastmapAddToMap = {
    bind: bind,
    bindRasterCollection: bindRasterCollection,
    buildRasterLayer: buildRasterLayer,
    fetchRasterInfo: fetchRasterInfo,
  };
})(typeof window !== 'undefined' ? window : this);
