"""Tests for read-only raster style display context."""

from app.services.raster_style_display import raster_style_viz_context


def test_classification_with_classes():
    ctx = raster_style_viz_context(
        {
            "style_type": "classification",
            "bidx": [1],
            "nodata": 27,
            "classes": [
                {"value": 1, "name": "Forest", "color": "#228822"},
                {"value": 2, "name": "Water", "color": "0000ff"},
            ],
        }
    )
    assert ctx is not None
    assert ctx["kind"] == "classification"
    assert ctx["bidx"] == "b1"
    assert ctx["nodata"] == "27"
    assert len(ctx["classes"]) == 2
    assert ctx["classes"][0]["color"] == "#228822"
    assert ctx["classes"][1]["color"] == "#0000ff"


def test_classification_from_colormap_only():
    ctx = raster_style_viz_context(
        {
            "style_type": "classification",
            "colormap": {"0": "#000000", "1": "#ffffff"},
        }
    )
    assert ctx["kind"] == "classification"
    assert len(ctx["classes"]) == 2


def test_continuous_rgb_bands():
    ctx = raster_style_viz_context(
        {
            "style_type": "continuous",
            "bidx": [3, 2, 1],
            "rescale": ["0", "3000"],
        }
    )
    assert ctx["kind"] == "continuous"
    assert ctx["mode_label"] == "RGB (bands)"
    labels = [p["label"] for p in ctx["params"]]
    assert "Red band" in labels
    assert "Green band" in labels
    assert "Blue band" in labels
    assert "Rescale" in labels


def test_continuous_single_band():
    ctx = raster_style_viz_context({"bidx": [2], "colormap_name": "viridis"})
    assert ctx["mode_label"] == "Single band"
    assert any(p["label"] == "Band" and p["value"] == "b2" for p in ctx["params"])
    assert any(p["label"] == "Colormap" for p in ctx["params"])


def test_continuous_expression():
    ctx = raster_style_viz_context({"expression": "b1 * 2", "color_formula": "Gamma RGB 2.2"})
    assert ctx["mode_label"] == "Expression"
    assert any(p["label"] == "Expression" for p in ctx["params"])
    assert any(p["label"] == "Color formula" for p in ctx["params"])


def test_empty_spec_returns_none():
    assert raster_style_viz_context(None) is None
    assert raster_style_viz_context({}) is None
