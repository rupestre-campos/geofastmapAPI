/**
 * Inline map feature editor: load GeoJSON, Geoman + GeoEditor, PATCH geometry/properties.
 * Pages call GeofastmapInlineFeatureEdit.setPageOptions({ map, base, getHideLayerIds, defaultStyleSpec }) once the map exists.
 */
(function (global) {
  'use strict';

  var state = null;

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function propValueToJson(val) {
    if (val === null || val === undefined || val === '') return null;
    var t = String(val).trim();
    if (t === '') return null;
    try {
      return JSON.parse(t);
    } catch (e) {
      return t;
    }
  }

  function getEditorFeatures(geoEditor) {
    if (!geoEditor) return [];
    try {
      var fc = geoEditor.getAllFeatureCollection && geoEditor.getAllFeatureCollection();
      if (fc && fc.features) return fc.features;
      var f = geoEditor.getFeatures && geoEditor.getFeatures();
      return Array.isArray(f) ? f : [];
    } catch (e) {
      return [];
    }
  }

  function editorFeaturesToGeometry(features) {
    var f = features && features[0];
    return f && f.geometry ? f.geometry : null;
  }

  /** Ensure JSON-serializable GeoJSON (typed arrays / exotic objects break PATCH). */
  function cloneGeometryForApi(geom) {
    if (!geom || typeof geom !== 'object') return null;
    try {
      return JSON.parse(JSON.stringify(geom));
    } catch (e) {
      return null;
    }
  }

  /** Read current geometry from the inline GeoJSON source (editor keeps it in sync). */
  function getGeometryFromInlineSource(map) {
    try {
      var src = map.getSource('geofastmap-inline-edit');
      if (!src || src.type !== 'geojson') return null;
      var d = null;
      if (typeof src.serialize === 'function') d = src.serialize();
      else if (src._data) d = src._data;
      if (!d) return null;
      if (d.type === 'FeatureCollection' && d.features && d.features.length) {
        var g = d.features[0].geometry;
        return g || null;
      }
      if (d.type === 'Feature') return d.geometry || null;
    } catch (e) {}
    return null;
  }

  function getGeometryForSave(map, geoEditor) {
    var g = editorFeaturesToGeometry(getEditorFeatures(geoEditor));
    if (!g) g = getGeometryFromInlineSource(map);
    return cloneGeometryForApi(g);
  }

  function removeInlineLayers(map) {
    if (!map) return;
    ['geofastmap-inline-symbol', 'geofastmap-inline-circle', 'geofastmap-inline-line', 'geofastmap-inline-fill'].forEach(function (id) {
      try {
        if (map.getLayer(id)) map.removeLayer(id);
      } catch (e) {}
    });
    try {
      if (map.getSource('geofastmap-inline-edit')) map.removeSource('geofastmap-inline-edit');
    } catch (e) {}
  }

  function stop() {
    if (!state || !state.active) return;
    var map = state.map;
    try {
      if (state.geoEditor && map && map.removeControl) map.removeControl(state.geoEditor);
    } catch (e) {}
    state.geoEditor = null;
    state.geoman = null;
    if (state.mapClickHandler && map) {
      map.off('click', state.mapClickHandler);
    }
    state.mapClickHandler = null;
    removeInlineLayers(map);
    if (map && state.hidden) {
      Object.keys(state.hidden).forEach(function (id) {
        try {
          if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', state.hidden[id]);
        } catch (e) {}
      });
    }
    if (state.toolbarEl && state.toolbarEl.parentNode) state.toolbarEl.parentNode.removeChild(state.toolbarEl);
    if (state.propsPopup) {
      try {
        state.propsPopup.remove();
      } catch (e) {}
    }
    state.toolbarEl = null;
    state.propsPopup = null;
    state.active = false;
    state = null;
  }

  /**
   * Force MapLibre to refetch dynamic vector tiles for this collection (cache-bust query param).
   * Call after successful PATCH so underlying tile layers refresh without waiting for HTTP max-age.
   */
  function bustDynamicVectorTilesForCollection(map, collectionId) {
    if (!map || !collectionId) return;
    var needle = '/collections/' + encodeURIComponent(collectionId) + '/tiles/dynamic/';
    try {
      var style = map.getStyle();
      if (!style || !style.sources) return;
      Object.keys(style.sources).forEach(function (sid) {
        var spec = style.sources[sid];
        if (!spec || spec.type !== 'vector' || !spec.tiles || !spec.tiles.length) return;
        if (String(spec.tiles[0]).indexOf(needle) === -1) return;
        var src = map.getSource(sid);
        if (!src || typeof src.setTiles !== 'function') return;
        var t0 = Date.now();
        var next = spec.tiles.map(function (tileUrl, i) {
          try {
            var u = new URL(tileUrl, window.location.href);
            u.searchParams.set('_gt', String(t0 + i));
            return u.toString();
          } catch (e) {
            var sep = String(tileUrl).indexOf('?') >= 0 ? '&' : '?';
            return String(tileUrl) + sep + '_gt=' + (t0 + i);
          }
        });
        src.setTiles(next);
      });
      try {
        window.dispatchEvent(
          new CustomEvent('geofastmap:dynamic-tiles-invalidate', { detail: { collectionId: collectionId } })
        );
      } catch (e) {}
    } catch (e) {}
  }

  function patchFeature(base, collectionId, featureId, body) {
    return fetch(
      base + '/collections/' + encodeURIComponent(collectionId) + '/items/' + encodeURIComponent(featureId),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/merge-patch+json', Accept: 'application/geo+json' },
        credentials: 'same-origin',
        body: JSON.stringify(body),
      }
    ).then(function (r) {
      if (!r.ok) return r.json().then(function (j) {
        throw j;
      });
      return r.json();
    });
  }

  function wireInlineSaveButtons(map, base, collectionId, featureId, bar) {
    var btnG = bar.querySelector('.geofastmap-inline-save-geom');
    var btnA = bar.querySelector('.geofastmap-inline-save-all');
    if (btnG) btnG.disabled = false;
    if (btnA) btnA.disabled = false;
    if (btnG) {
      btnG.onclick = function () {
        if (!state || !state.geoEditor) {
          alert('Editor not ready.');
          return;
        }
        var geom = getGeometryForSave(map, state.geoEditor);
        if (!geom) {
          alert('No geometry to save. Select the feature and try again.');
          return;
        }
        patchFeature(base, collectionId, featureId, { geometry: geom })
          .then(function (updated) {
            state.featureGeojson = updated;
            if (state.geoEditor.loadGeoJson) state.geoEditor.loadGeoJson(updated);
            bustDynamicVectorTilesForCollection(map, collectionId);
            alert('Geometry saved.');
          })
          .catch(function (err) {
            alert('Error: ' + (err.detail || err.message || JSON.stringify(err)));
          });
      };
    }
    if (btnA) {
      btnA.onclick = function () {
        if (!state || !state.geoEditor) {
          alert('Editor not ready.');
          return;
        }
        var feats = getEditorFeatures(state.geoEditor);
        var geom = getGeometryForSave(map, state.geoEditor);
        var rawProps = feats && feats[0] && feats[0].properties ? feats[0].properties : (state.featureGeojson && state.featureGeojson.properties) || {};
        var clean = {};
        Object.keys(rawProps).forEach(function (k) {
          if (k.indexOf('__gm_') !== 0) clean[k] = rawProps[k];
        });
        var body = {};
        if (geom) body.geometry = geom;
        body.properties = clean;
        patchFeature(base, collectionId, featureId, body)
          .then(function (updated) {
            state.featureGeojson = updated;
            if (state.geoEditor.loadGeoJson) state.geoEditor.loadGeoJson(updated);
            bustDynamicVectorTilesForCollection(map, collectionId);
            alert('Saved.');
          })
          .catch(function (err) {
            alert('Error: ' + (err.detail || err.message || JSON.stringify(err)));
          });
      };
    }
  }

  function openPropsPopup(map, lngLat, GMU) {
    if (!state || !state.featureGeojson) return;
    if (state.propsPopup) {
      try {
        state.propsPopup.remove();
      } catch (e) {}
    }
    var props = state.featureGeojson.properties || {};
    var keys = Object.keys(props).filter(function (k) {
      return k.indexOf('__gm_') !== 0;
    });
    var rows = keys
      .map(function (k) {
        var v = props[k];
        var valStr = v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
        return (
          '<tr><td><label>' +
          esc(k) +
          '</label></td><td><input type="text" class="geofastmap-inline-prop-inp" data-k="' +
          esc(k) +
          '" value="' +
          esc(valStr) +
          '" style="width:100%;max-width:220px"></td></tr>'
        );
      })
      .join('');
    var html =
      '<div class="map-popup geofastmap-inline-props-popup">' +
      '<div class="map-popup-step-title">Edit properties</div>' +
      '<table class="geofastmap-inline-props-table">' +
      rows +
      '</table>' +
      '<div style="margin-top:8px;"><button type="button" class="btn btn-sm btn-primary geofastmap-inline-save-props">Save properties</button></div>' +
      '</div>';
    var popup = new maplibregl.Popup({ closeButton: true, maxWidth: '360px' }).setLngLat(lngLat).setHTML(html).addTo(map);
    state.propsPopup = popup;
    var saveBtn = popup.getElement().querySelector('.geofastmap-inline-save-props');
    if (saveBtn) {
      saveBtn.onclick = function () {
        var tbody = popup.getElement().querySelector('.geofastmap-inline-props-table');
        var next = {};
        (tbody ? tbody.querySelectorAll('.geofastmap-inline-prop-inp') : []).forEach(function (inp) {
          var k = inp.getAttribute('data-k');
          if (!k) return;
          next[k] = propValueToJson(inp.value);
        });
        patchFeature(state.base, state.collectionId, state.featureId, { properties: next })
          .then(function (updated) {
            state.featureGeojson = updated;
            if (state.geoEditor && state.geoEditor.loadGeoJson) state.geoEditor.loadGeoJson(updated);
            bustDynamicVectorTilesForCollection(map, state.collectionId);
            try {
              popup.remove();
            } catch (e) {}
            if (state.propsPopup === popup) state.propsPopup = null;
            alert('Properties saved.');
          })
          .catch(function (err) {
            alert('Error: ' + (err.detail || err.message || JSON.stringify(err)));
          });
      };
    }
  }

  function handleMapClickForProps(e, map) {
    if (!state || !state.active || map !== state.map) return false;
    var ids = ['geofastmap-inline-fill', 'geofastmap-inline-line', 'geofastmap-inline-circle', 'geofastmap-inline-symbol'].filter(function (id) {
      try {
        return map.getLayer(id);
      } catch (err) {
        return false;
      }
    });
    if (!ids.length) return false;
    var feats;
    try {
      feats = map.queryRenderedFeatures(e.point, { layers: ids });
    } catch (err) {
      return false;
    }
    if (!feats || !feats.length) return false;
    openPropsPopup(map, e.lngLat, null);
    return true;
  }

  function start(pageOpts, collectionId, featureId) {
    if (state && state.active) {
      alert('Finish or cancel the current edit first (Cancel on the map toolbar).');
      return;
    }
    var map = pageOpts.map;
    var base = pageOpts.base;
    var getHideLayerIds = pageOpts.getHideLayerIds || function () {
      return [];
    };
    var defaultStyleSpec = pageOpts.defaultStyleSpec || {};
    if (typeof pageOpts.getDefaultStyleSpecForCollection === 'function') {
      try {
        defaultStyleSpec = pageOpts.getDefaultStyleSpecForCollection(collectionId) || defaultStyleSpec;
      } catch (e) {}
    }

    if (window._geofastmapPopup) {
      try {
        window._geofastmapPopup.remove();
      } catch (e) {}
    }

    fetch(base + '/collections/' + encodeURIComponent(collectionId) + '/items/' + encodeURIComponent(featureId), {
      headers: { Accept: 'application/geo+json' },
      credentials: 'same-origin',
    })
      .then(function (r) {
        if (!r.ok) throw new Error(r.statusText);
        return r.json();
      })
      .then(function (feat) {
        state = {
          active: true,
          map: map,
          base: base,
          collectionId: collectionId,
          featureId: featureId,
          featureGeojson: feat,
          geoEditor: null,
          geoman: null,
          hidden: {},
          toolbarEl: null,
          propsPopup: null,
          mapClickHandler: null,
        };

        var hidden = {};
        getHideLayerIds(collectionId).forEach(function (id) {
          try {
            if (map.getLayer(id)) {
              hidden[id] = map.getLayoutProperty(id, 'visibility');
              map.setLayoutProperty(id, 'visibility', 'none');
            }
          } catch (e) {}
        });
        state.hidden = hidden;

        var gj = {
          type: 'Feature',
          geometry: feat.geometry,
          properties: feat.properties || {},
        };
        if (feat.id != null) gj.id = feat.id;
        var p = gj.properties || {};
        gj.properties = {};
        Object.keys(p).forEach(function (k) {
          if (k.indexOf('__gm_') !== 0) gj.properties[k] = p[k];
        });
        if (featureId != null) gj.properties.id = featureId;

        removeInlineLayers(map);
        map.addSource('geofastmap-inline-edit', { type: 'geojson', data: gj });
        var GMU = global.GeofastmapUtils;
        var s = GMU && GMU.specToPaint ? GMU.specToPaint(defaultStyleSpec) : {};
        var beforeId = null;
        var sl = map.getStyle() && map.getStyle().layers;
        if (sl && sl.length) {
          for (var i = sl.length - 1; i >= 0; i--) {
            if (sl[i].id.indexOf('geofastmap-inline') !== 0) {
              beforeId = sl[i].id;
              break;
            }
          }
        }
        var pf = GMU && GMU.pointFilter ? GMU.pointFilter : ['in', ['geometry-type'], ['literal', ['Point', 'MultiPoint']]];
        var npf = GMU && GMU.notPointFilter ? GMU.notPointFilter : ['!', ['in', ['geometry-type'], ['literal', ['Point', 'MultiPoint']]]];

        map.addLayer(
          {
            id: 'geofastmap-inline-fill',
            type: 'fill',
            source: 'geofastmap-inline-edit',
            filter: npf,
            paint: { 'fill-color': s.fillColor || '#58a6ff', 'fill-opacity': s.fillOpacity != null ? s.fillOpacity : 0.6 },
          },
          beforeId
        );
        map.addLayer(
          {
            id: 'geofastmap-inline-line',
            type: 'line',
            source: 'geofastmap-inline-edit',
            filter: npf,
            paint: {
              'line-color': s.lineColor || '#58a6ff',
              'line-width': s.lineWidth != null ? s.lineWidth : 2,
              'line-opacity': s.lineOpacity != null ? s.lineOpacity : 1,
              'line-dasharray': s.lineDash || [1, 0],
            },
          },
          beforeId
        );
        map.addLayer(
          {
            id: 'geofastmap-inline-circle',
            type: 'circle',
            source: 'geofastmap-inline-edit',
            filter: pf,
            paint: {
              'circle-color': s.pointColor || '#58a6ff',
              'circle-radius': s.pointRadius != null ? s.pointRadius : 8,
              'circle-opacity': s.pointOpacity != null ? s.pointOpacity : 0.9,
            },
          },
          beforeId
        );
        if (map.hasImage && map.hasImage('geofastmap-pin')) {
          var psz = s.pointRadius != null ? s.pointRadius : 8;
          var iconSize = typeof psz === 'number' ? psz / 12 : 0.7;
          map.addLayer(
            {
              id: 'geofastmap-inline-symbol',
              type: 'symbol',
              source: 'geofastmap-inline-edit',
              filter: pf,
              layout: { 'icon-image': 'geofastmap-pin', 'icon-size': iconSize, 'icon-allow-overlap': true },
              paint: { 'icon-color': s.pointColor || '#58a6ff', 'icon-opacity': s.pointOpacity != null ? s.pointOpacity : 0.9 },
            },
            beforeId
          );
          map.setLayoutProperty('geofastmap-inline-circle', 'visibility', 'none');
        }

        var editModes = ['select', 'drag', 'change', 'rotate', 'cut', 'delete', 'scale', 'copy', 'split', 'union', 'difference', 'simplify', 'lasso'];
        var drawModes = ['polygon', 'line', 'rectangle', 'circle', 'marker', 'freehand'];
        var keys = Object.keys(gj.properties || {}).filter(function (k) {
          return k.indexOf('__gm_') !== 0;
        });
        var attributeSchema = {
          polygon: keys.map(function (k) {
            return { name: k, label: k, type: 'string' };
          }),
          line: keys.map(function (k) {
            return { name: k, label: k, type: 'string' };
          }),
          point: keys.map(function (k) {
            return { name: k, label: k, type: 'string' };
          }),
          common: keys.map(function (k) {
            return { name: k, label: k, type: 'string' };
          }),
        };

        Promise.all([import('https://esm.sh/@geoman-io/maplibre-geoman-free@0.6.2'), import('https://esm.sh/maplibre-gl-geo-editor@0.7.3')])
          .then(function (mods) {
            var Geoman = mods[0].Geoman || mods[0].default;
            var GeoEditor = mods[1].GeoEditor;
            var geoman = new Geoman(map, {});
            map.once('gm:loaded', function () {
              if (!state || !state.active) return;
              var geoEditor = new GeoEditor({
                position: 'top-left',
                toolbarOrientation: 'vertical',
                columns: 2,
                drawModes: drawModes,
                editModes: editModes,
                showFeatureProperties: true,
                fitBoundsOnLoad: true,
                attributeSchema: attributeSchema,
              });
              geoEditor.setGeoman(geoman);
              map.addControl(geoEditor, 'top-left');
              geoEditor.loadGeoJson(gj);
              state.geoEditor = geoEditor;
              state.geoman = geoman;
              wireInlineSaveButtons(map, base, collectionId, featureId, bar);
            });
          })
          .catch(function (err) {
            console.warn('GeoEditor failed', err);
            alert('Could not load map editor. Check network/CDN.');
            stop();
          });

        var wrap = map.getContainer().parentNode || map.getContainer();
        var bar = document.createElement('div');
        bar.className = 'geofastmap-inline-edit-toolbar';
        bar.setAttribute('role', 'toolbar');
        bar.innerHTML =
          '<span style="font-weight:600;margin-right:8px;">Editing feature</span>' +
          '<button type="button" class="btn btn-sm btn-primary geofastmap-inline-save-geom" disabled title="Loads after editor is ready">Save geometry</button> ' +
          '<button type="button" class="btn btn-sm btn-primary geofastmap-inline-save-all" disabled title="Loads after editor is ready">Save all</button> ' +
          '<button type="button" class="btn btn-sm geofastmap-inline-cancel">Cancel</button>' +
          '<span class="meta" style="margin-left:8px;">Click the feature to edit properties. Ctrl+Z / Ctrl+Y undo/redo.</span>';
        bar.style.cssText =
          'position:absolute;bottom:12px;left:50%;transform:translateX(-50%);z-index:20;background:var(--card,#fff);border:1px solid var(--border,#ccc);padding:8px 12px;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:flex;flex-wrap:wrap;align-items:center;gap:6px;max-width:96%;';
        wrap.style.position = wrap.style.position || 'relative';
        wrap.appendChild(bar);
        state.toolbarEl = bar;

        bar.querySelector('.geofastmap-inline-cancel').onclick = function () {
          stop();
        };

        state.mapClickHandler = function (e) {
          handleMapClickForProps(e, map);
        };
        map.on('click', state.mapClickHandler);
      })
      .catch(function (err) {
        alert('Could not load feature: ' + (err.message || String(err)));
      });
  }

  global.GeofastmapInlineFeatureEdit = {
    setPageOptions: function (opts) {
      global.__geofastmapInlinePageOpts = opts || {};
    },
    /** Bust browser/MapLibre tile cache for all vector sources pointing at this collection's dynamic tiles. */
    bustDynamicTilesForCollection: function (map, collectionId) {
      bustDynamicVectorTilesForCollection(map, collectionId);
    },
    startFromPopup: function (collectionId, featureId) {
      var o = global.__geofastmapInlinePageOpts;
      if (!o || !o.map) return;
      start(o, collectionId, featureId);
    },
    handleMapClick: function (e, map) {
      return handleMapClickForProps(e, map);
    },
    isActive: function () {
      return !!(state && state.active);
    },
    stop: stop,
  };

  document.addEventListener(
    'click',
    function (e) {
      var a = e.target.closest && e.target.closest('a[data-action="geofastmap-edit-feature"]');
      if (!a) return;
      e.preventDefault();
      e.stopPropagation();
      var cid = a.getAttribute('data-collection');
      var fid = a.getAttribute('data-feature-id');
      if (!cid || !fid) return;
      global.GeofastmapInlineFeatureEdit.startFromPopup(cid, fid);
    },
    true
  );
})(typeof window !== 'undefined' ? window : this);
