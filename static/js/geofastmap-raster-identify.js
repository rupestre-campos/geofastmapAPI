/**
 * Raster pixel identify: click map → Titiler point API → popup with band values.
 */
(function (global) {
  'use strict';

  var escapeHtml = (global.GeofastmapUtils && global.GeofastmapUtils.escapeHtml) || function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  };

  var activeAbort = null;

  function lngLatParts(lngLat) {
    if (!lngLat) return null;
    var lon = lngLat.lng != null ? lngLat.lng : (Array.isArray(lngLat) ? lngLat[0] : null);
    var lat = lngLat.lat != null ? lngLat.lat : (Array.isArray(lngLat) ? lngLat[1] : null);
    if (lon == null || lat == null || !isFinite(lon) || !isFinite(lat)) return null;
    return { lon: lon, lat: lat };
  }

  function buildPointUrl(tileUrlTemplate, lngLat) {
    if (!tileUrlTemplate || typeof tileUrlTemplate !== 'string') return null;
    var ll = lngLatParts(lngLat);
    if (!ll) return null;
    var u = tileUrlTemplate.trim();
    if (u.indexOf('/point') >= 0 && u.indexOf('lon=') < 0) {
      var sep0 = u.indexOf('?') >= 0 ? '&' : '?';
      return u + sep0 + 'lon=' + encodeURIComponent(ll.lon) + '&lat=' + encodeURIComponent(ll.lat);
    }
    u = u.replace(/\/tiles\/[^?#]+/, '/point');
    if (u.indexOf('/point') < 0) return null;
    var q = u.indexOf('?');
    var path = q >= 0 ? u.slice(0, q) : u;
    var qs = q >= 0 ? u.slice(q + 1) : '';
    var sep = qs ? '&' : '?';
    return path + '?' + qs + sep + 'lon=' + encodeURIComponent(ll.lon) + '&lat=' + encodeURIComponent(ll.lat);
  }

  function popupHtml(data, opts) {
    opts = opts || {};
    var title = opts.title ? '<div class="map-popup-step-title">' + escapeHtml(opts.title) + '</div>' : '';
    var parts = ['<div class="map-popup map-popup-raster-values">', title];
    if (!data || !data.rows || !data.rows.length) {
      parts.push('<p class="meta">No data</p></div>');
      return parts.join('');
    }
    parts.push('<ul class="raster-identify-rows">');
    data.rows.forEach(function (row) {
      if (row.role === 'class' && row.color) {
        parts.push(
          '<li class="raster-identify-row">' +
          '<span class="raster-viz-swatch" style="background:' + escapeHtml(row.color) + ';"></span> ' +
          '<span>' + escapeHtml(row.display || row.label || '') + '</span></li>'
        );
      } else {
        parts.push('<li class="raster-identify-row"><code>' + escapeHtml(row.display || '') + '</code></li>');
      }
    });
    parts.push('</ul></div>');
    return parts.join('');
  }

  function loadingHtml(title) {
    var t = title ? '<div class="map-popup-step-title">' + escapeHtml(title) + '</div>' : '';
    return '<div class="map-popup map-popup-raster-values">' + t + '<p class="meta">Loading…</p></div>';
  }

  function errorHtml(msg, title) {
    var t = title ? '<div class="map-popup-step-title">' + escapeHtml(title) + '</div>' : '';
    return '<div class="map-popup map-popup-raster-values">' + t + '<p class="meta">' + escapeHtml(msg || 'Could not read pixel') + '</p></div>';
  }

  function fetchPoint(url, signal) {
    return fetch(url, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      signal: signal
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          var msg = (body && body.detail) ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : r.statusText;
          throw new Error(msg || 'Point request failed');
        });
      }
      return r.json();
    });
  }

  function attach(map, registrations, options) {
    if (!map || !registrations || !registrations.length) return;
    options = options || {};
    var key = options.handlerKey || '__geofastmapRasterIdentify';
    if (map[key]) return;
    map[key] = true;

    var popup = options.popup || new maplibregl.Popup({ closeButton: true, closeOnClick: true });
    var sorted = registrations.slice().sort(function (a, b) {
      return (b.priority || 0) - (a.priority || 0);
    });

    map.on('click', function (e) {
      if (options.beforeClick && options.beforeClick(e, map) === false) return;
      if (global.GeofastmapInlineFeatureEdit && GeofastmapInlineFeatureEdit.handleMapClick(e, map)) return;

      var reg = null;
      var url = null;
      for (var i = 0; i < sorted.length; i++) {
        if (!sorted[i].getPointUrl) continue;
        var u = sorted[i].getPointUrl(e.lngLat);
        if (u) {
          reg = sorted[i];
          url = u;
          break;
        }
      }
      if (!url || !reg) return;

      if (activeAbort) {
        try { activeAbort.abort(); } catch (err) {}
      }
      activeAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var signal = activeAbort ? activeAbort.signal : undefined;

      var title = reg.title || 'Raster values';
      popup.setLngLat(e.lngLat).setHTML(loadingHtml(title)).addTo(map);
      if (options.onPopupOpen) options.onPopupOpen(popup);

      fetchPoint(url, signal).then(function (data) {
        if (signal && signal.aborted) return;
        popup.setLngLat(e.lngLat).setHTML(popupHtml(data, { title: title })).addTo(map);
      }).catch(function (err) {
        if (signal && signal.aborted) return;
        popup.setLngLat(e.lngLat).setHTML(errorHtml((err && err.message) || 'Could not read pixel', title)).addTo(map);
      });
    });

    if (options.cursor !== false) {
      map.getCanvas().style.cursor = 'crosshair';
    }
  }

  function appendRasterSection(popup, lngLat, getPointUrl, title) {
    if (!getPointUrl) return Promise.resolve(null);
    var url = getPointUrl(lngLat);
    if (!url) return Promise.resolve(null);
    if (activeAbort) {
      try { activeAbort.abort(); } catch (e) {}
    }
    activeAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
    return fetchPoint(url, activeAbort ? activeAbort.signal : undefined).then(function (data) {
      return popupHtml(data, { title: title || 'Raster values' });
    }).catch(function (err) {
      return errorHtml((err && err.message) || 'Raster unavailable', title || 'Raster values');
    });
  }

  global.GeofastmapRasterIdentify = {
    buildPointUrl: buildPointUrl,
    fetchPoint: fetchPoint,
    popupHtml: popupHtml,
    loadingHtml: loadingHtml,
    errorHtml: errorHtml,
    attach: attach,
    appendRasterSection: appendRasterSection
  };
})(typeof window !== 'undefined' ? window : this);
