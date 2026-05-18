import pytest
from fastapi import HTTPException

from app.services.raster_style_spec import (
    RasterStyleSpecError,
    build_classification_style_spec,
    is_classification_style,
    normalize_raster_style_spec,
    normalize_raster_style_spec_http,
    parse_classification_inputs,
    parse_nodata_value,
    titiler_nodata_param,
    titiler_params_from_classification_style,
)


def test_parse_classes_array_and_colormap():
    colormap, classes = parse_classification_inputs(
        {"0": "#000000", "1": "#FF0000"},
        [{"value": 0, "name": "nodata", "color": "#000000"}, {"value": 1, "name": "building", "color": "#FF0000"}],
    )
    assert colormap == {"0": "#000000", "1": "#FF0000"}
    assert len(classes) == 2
    assert classes[0]["name"] == "nodata"


def test_parse_classes_object_shape():
    colormap, classes = parse_classification_inputs(
        None,
        {"2": {"name": "water", "color": "#0000FF"}},
    )
    assert colormap == {"2": "#0000FF"}
    assert classes[0]["value"] == "2"
    assert classes[0]["name"] == "water"


def test_colormap_only_generates_classes():
    colormap, classes = parse_classification_inputs({"10": "#ABCDEF"}, None)
    assert colormap == {"10": "#ABCDEF"}
    assert classes == [{"value": "10", "name": "10", "color": "#ABCDEF"}]


def test_invalid_hex_raises():
    with pytest.raises(RasterStyleSpecError, match="Invalid hex"):
        parse_classification_inputs({"1": "red"}, None)


def test_color_mismatch_raises():
    with pytest.raises(RasterStyleSpecError, match="mismatch"):
        parse_classification_inputs(
            {"1": "#FF0000"},
            [{"value": 1, "name": "a", "color": "#00FF00"}],
        )


def test_normalize_classification_style_spec():
    spec = normalize_raster_style_spec(
        {
            "style_type": "classification",
            "bidx": [2],
            "colormap": {"0": "#000000"},
            "classes": [{"value": 0, "name": "bg", "color": "#000000"}],
        }
    )
    assert spec["style_type"] == "classification"
    assert spec["colormap_type"] == "explicit"
    assert spec["bidx"] == ["2"]
    assert is_classification_style(spec)


def test_normalize_continuous_unchanged():
    spec = {"style_type": "continuous", "rescale": ["0", "1"], "colormap_name": "viridis"}
    assert normalize_raster_style_spec(spec) == spec


def test_normalize_http_raises_400():
    with pytest.raises(HTTPException) as exc:
        normalize_raster_style_spec_http({"style_type": "classification"})
    assert exc.value.status_code == 400


def test_titiler_params_from_classification():
    params = titiler_params_from_classification_style(
        {
            "colormap": {"1": "#FF0000"},
            "colormap_type": "explicit",
            "bidx": ["1"],
        }
    )
    keys = [p[0] for p in params]
    assert "colormap" in keys
    assert "colormap_type" in keys
    assert ("bidx", "1") in params
    colormap_val = next(v for k, v in params if k == "colormap")
    assert '"1"' in colormap_val


def test_parse_nodata_value():
    assert parse_nodata_value("0") == 0
    assert parse_nodata_value("-9999") == -9999
    assert parse_nodata_value("3.14") == 3.14
    assert parse_nodata_value("") is None
    assert parse_nodata_value(None) is None


def test_classification_style_with_nodata():
    spec = normalize_raster_style_spec(
        {
            "style_type": "classification",
            "nodata": 0,
            "colormap": {"1": "#FF0000"},
            "classes": [{"value": 1, "name": "a", "color": "#FF0000"}],
        }
    )
    assert spec["nodata"] == 0
    params = titiler_params_from_classification_style(spec)
    assert ("nodata", "0") in params


def test_titiler_nodata_param_continuous():
    assert titiler_nodata_param({"nodata": -32768}) == ("nodata", "-32768")
    assert titiler_nodata_param({}) is None


def test_build_classification_style_spec_with_asset():
    out = build_classification_style_spec(
        {"asset": "feat-1", "bidx": ["3"], "nodata": 255},
        colormap_raw={"5": "#111111"},
        classes_raw=None,
    )
    assert out["asset"] == "feat-1"
    assert out["bidx"] == ["3"]
    assert out["nodata"] == 255
    assert out["colormap"]["5"] == "#111111"
