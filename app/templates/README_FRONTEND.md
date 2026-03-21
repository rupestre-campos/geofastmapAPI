# Frontend shared components

Reusable pieces live in one place so updates and bug fixes apply everywhere.

## Shared JavaScript (`/static/js/geofast-map-utils.js`)

Loaded on map/style pages after MapLibre GL. Exposes `window.GeofastMapUtils`:

| Member | Use |
|--------|-----|
| `getBasemaps(googleKey)` | Basemap config (osm, streets, satellite, hybrid, google_*). Pass optional Google API key. |
| `specToPaint(spec)` | Converts a `style_spec` object to paint values (fillColor, lineWidth, pointRadius, etc.), including zoom expressions. Use for all fill/line/circle layers. |
| `pointFilter` / `notPointFilter` | MapLibre filter arrays for point vs non-point geometry. |
| `zoomStopsToExpression(stops)` | Converts `[[z, v], ...]` to MapLibre interpolate expression. |
| `LINE_DASH` | `{ solid, dashed, dotted }` for line-dasharray. |
| `popupHtmlForFeature(feat, base, collectionId)` | HTML string for feature popup (ID link + sorted properties). |
| `escapeHtml(s)` | Safe string for HTML. |
| `DEFAULT_STYLE_SPEC` | Default style values. |
| `setPaintPropertySafe(map, layerId, property, value)` | Optional helper to set paint without throwing. |

**Usage:** In any template that uses maps or vector styles, include after MapLibre:

```html
<script src="{{ base }}/static/js/geofast-map-utils.js"></script>
```

Then use e.g. `const BASEMAPS = GeofastMapUtils.getBasemaps(googleKey);` and `GeofastMapUtils.specToPaint(spec)` instead of redefining these per page.

## Shared HTML partials

### `_map_popup_css.html`

Styles for feature popups (`.map-popup`, `.map-popup-id`, `.map-popup-row`). Include in `{% block head %}` on any page that shows feature click popups:

```html
{% block head %}
  {% include '_map_popup_css.html' %}
  ...
{% endblock %}
```

### `_basemap_selector.html`

Basemap `<select>` with the same six options everywhere. Optional variable: `basemap_select_id` (default `map-basemap`).

```html
<div class="map-controls">
  {% include '_basemap_selector.html' %}
</div>
```

For a custom id:

```html
{% include '_basemap_selector.html' with context %}
{% set basemap_select_id = 'my-basemap' %}
```

(Or pass `basemap_select_id` from the view.)

## Pages using shared code

- **collection.html** – utils + popup CSS + basemap partial
- **collection_edit.html** – utils + popup CSS + basemap partial
- **items.html** – utils + popup CSS + basemap partial
- **item.html** – utils + popup CSS + basemap partial
- **item_edit.html** – utils + popup CSS + basemap partial
- **add_feature.html** – utils + basemap partial
- **map_view.html** – utils + popup CSS + basemap partial; `specFromLayer(layer)` uses `GeofastMapUtils.specToPaint(layer.style_spec)`
- **map_edit.html** – utils + popup CSS + basemap partial

**Style editor** (`style_editor.html`) and **collections list** (`collections.html`) keep their own basemap/popup logic where the UI or data shape differs; they can be switched to shared utils later if desired.

### Collection “read-only layer” (`editing_enabled`)

- Stored on the collection (`editing_enabled`, default `true`). When `false`, only **administrators** may change the collection or its features (API enforces via `can_edit_collection`).
- **Collection edit** (`collection_edit.html`): admins see an **Allow editing** checkbox; saves include `editing_enabled` in the PATCH body (`appendEditingEnabledIfAdmin`).
- **Collection view** (`collection.html`): shows a short notice when the layer is read-only for non-admins.

Run DB migration `0021_collection_editing_enabled` after deploy.

## Consistency

- For **page-specific behavior**, extend the shared API with **parameters** (e.g. `popupHtmlForFeature(feat, base, collectionId)`) or options instead of copying code.
- New map or style pages should include the shared script and partials and call `GeofastMapUtils` rather than redefining helpers.
