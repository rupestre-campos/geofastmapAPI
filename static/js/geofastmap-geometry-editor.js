/**
 * Shared loader for Geoman + GeoEditor.
 *
 * Keeps every drawing surface on the same dependency versions, waits for
 * Geoman's actual ready promise (the gm:loaded event can be missed), and
 * chooses a touch-friendly toolbar layout on small/coarse-pointer devices.
 */
(function (global) {
  'use strict';

  var modulesPromise = null;
  var GEOMAN_URL =
    'https://esm.sh/@geoman-io/maplibre-geoman-free@0.6.2?deps=maplibre-gl@5.18.0';
  var EDITOR_URL =
    'https://esm.sh/maplibre-gl-geo-editor@0.7.3?deps=maplibre-gl@5.18.0,@geoman-io/maplibre-geoman-free@0.6.2';

  function isTouchLayout() {
    return (
      (global.matchMedia && global.matchMedia('(pointer: coarse)').matches) ||
      (global.matchMedia && global.matchMedia('(max-width: 768px)').matches)
    );
  }

  function loadModules() {
    if (!modulesPromise) {
      modulesPromise = Promise.all([import(GEOMAN_URL), import(EDITOR_URL)]).then(function (modules) {
        var Geoman = modules[0].Geoman || modules[0].default;
        var GeoEditor = modules[1].GeoEditor || modules[1].default;
        if (typeof Geoman !== 'function' || typeof GeoEditor !== 'function') {
          throw new Error('The geometry editor modules did not expose their expected constructors.');
        }
        return { Geoman: Geoman, GeoEditor: GeoEditor };
      });
    }
    return modulesPromise;
  }

  function withTimeout(promise, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var timer = global.setTimeout(function () {
        reject(new Error('Timed out while initializing the geometry editor.'));
      }, timeoutMs);
      Promise.resolve(promise).then(
        function (value) {
          global.clearTimeout(timer);
          resolve(value);
        },
        function (err) {
          global.clearTimeout(timer);
          reject(err);
        }
      );
    });
  }

  function setStatus(statusEl, message, isError) {
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.hidden = !message;
    statusEl.classList.toggle('geometry-editor-status-error', !!isError);
  }

  function create(map, options) {
    options = options || {};
    var statusEl =
      typeof options.statusEl === 'string'
        ? document.getElementById(options.statusEl)
        : options.statusEl || null;
    var touch = isTouchLayout();

    setStatus(statusEl, 'Loading drawing tools…', false);

    return loadModules()
      .then(function (modules) {
        /*
         * Register the fallback event listener before construction. Newer
         * Geoman versions expose waitForGeomanLoaded(), but this ordering also
         * keeps initialization safe if an older compatible build is served.
         */
        var eventReady = new Promise(function (resolve) {
          map.once('gm:loaded', resolve);
        });
        var geoman = new modules.Geoman(map, options.geomanOptions || {});
        var ready =
          typeof geoman.waitForGeomanLoaded === 'function'
            ? geoman.waitForGeomanLoaded()
            : eventReady;

        return withTimeout(ready, options.timeoutMs || 15000).then(function () {
          var editorOptions = Object.assign({}, options.editorOptions || {});
          var controlPosition =
            options.controlPosition || (touch ? 'bottom-left' : editorOptions.position || 'top-left');
          editorOptions.position = controlPosition;
          if (!editorOptions.toolbarOrientation) editorOptions.toolbarOrientation = 'vertical';
          editorOptions.columns = touch ? options.touchColumns || 3 : editorOptions.columns || 2;

          var editor = new modules.GeoEditor(editorOptions);
          editor.setGeoman(geoman);
          map.addControl(editor, controlPosition);
          setStatus(statusEl, '', false);
          return {
            geoman: geoman,
            geoEditor: editor,
            isTouch: touch,
            controlPosition: controlPosition,
          };
        });
      })
      .catch(function (err) {
        modulesPromise = null;
        var detail = err && err.message ? err.message : String(err);
        setStatus(statusEl, 'Drawing tools could not load. Reload the page or check CDN access. ' + detail, true);
        throw err;
      });
  }

  global.GeofastmapGeometryEditor = {
    create: create,
    isTouchLayout: isTouchLayout,
    loadModules: loadModules,
  };
})(typeof window !== 'undefined' ? window : this);
