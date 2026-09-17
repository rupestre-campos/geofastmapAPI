"""Regression checks for the shared map geometry editor integration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def test_drawing_pages_use_shared_loader() -> None:
    drawing_pages = (
        "add_feature.html",
        "item_edit.html",
        "collection_edit.html",
        "style_editor.html",
    )
    for name in drawing_pages:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "geofastmap-geometry-editor.js" in source
        assert "GeofastmapGeometryEditor.create(map" in source
        assert "import('https://esm.sh/@geoman" not in source
    add_feature = (TEMPLATES / "add_feature.html").read_text(encoding="utf-8")
    assert "controlPosition: 'top-right'" in add_feature
    assert "feats.length > 1" in add_feature


def test_inline_edit_pages_load_helper_before_inline_editor() -> None:
    inline_pages = ("collection.html", "item.html", "items.html", "map_edit.html", "map_view.html")
    for name in inline_pages:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        helper_at = source.index("geofastmap-geometry-editor.js")
        inline_at = source.index("geofastmap-inline-feature-edit.js")
        assert helper_at < inline_at


def test_shared_loader_has_touch_and_failure_handling() -> None:
    source = (ROOT / "static" / "js" / "geofastmap-geometry-editor.js").read_text(encoding="utf-8")
    assert "(pointer: coarse)" in source
    assert "'bottom-left'" in source
    assert "waitForGeomanLoaded" in source
    assert "geometry-editor-status-error" in source


def test_mosaic_planner_drawn_aoi_is_wired() -> None:
    source = (TEMPLATES / "mosaic_planner.html").read_text(encoding="utf-8")
    assert "new MapboxDraw(" in source
    assert "touchEnabled: true" in source
    assert "getDrawnAoi()" in source
    assert "map.on('draw.create'" in source


def test_geoman_events_use_current_namespace() -> None:
    for name in ("collection_edit.html", "style_editor.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "map.on('pm:" not in source
        assert "map.on('gm:create'" in source
