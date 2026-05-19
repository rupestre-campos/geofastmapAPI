"""Tests for Titiler point response enrichment."""

from app.services.titiler_point import (
    _has_sample_values,
    enrich_point_response,
    style_spec_from_request_query,
)


class _FakeQuery:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def multi_items(self):
        out = []
        for k, v in self._data.items():
            if isinstance(v, list):
                for x in v:
                    out.append((k, x))
            else:
                out.append((k, v))
        return out


class _FakeRequest:
    def __init__(self, params: dict):
        self.query_params = _FakeQuery(params)


def test_rgb_bands_enrichment():
    spec = {"bidx": [8, 2, 3]}
    raw = {"coordinates": [-47.9, -15.8], "values": [126, 223, 221], "band_names": ["b8", "b2", "b3"]}
    out = enrich_point_response(raw, spec)
    assert out["render_mode"] == "rgb_bands"
    assert len(out["rows"]) == 3
    assert out["rows"][0]["display"] == "R b8 126"
    assert out["rows"][1]["display"] == "G b2 223"
    assert out["rows"][2]["display"] == "B b3 221"


def test_rgb_assets_enrichment():
    spec = {"assets": ["B04", "B03", "B02"]}
    raw = {"values": [100, 200, 50]}
    out = enrich_point_response(raw, spec)
    assert out["render_mode"] == "rgb_assets"
    assert "R B04 100" in out["rows"][0]["display"]


def test_single_band_with_colormap_name():
    spec = {"bidx": [1], "colormap_name": "viridis"}
    raw = {"values": [0.42]}
    out = enrich_point_response(raw, spec)
    assert out["render_mode"] == "single"
    assert "viridis" in out["rows"][0]["display"]


def test_expression_enrichment():
    spec = {"expression": "b1 - b2"}
    raw = {"values": [0.15]}
    out = enrich_point_response(raw, spec)
    assert out["render_mode"] == "expression"
    assert "b1 - b2" in out["rows"][0]["display"]


def test_classification_enrichment():
    spec = {
        "style_type": "classification",
        "classes": [
            {"value": 1, "name": "Forest", "color": "#228822"},
            {"value": 2, "name": "Water", "color": "#0000ff"},
        ],
    }
    raw = {"values": [1]}
    out = enrich_point_response(raw, spec)
    assert out["render_mode"] == "classification"
    assert out["rows"][0]["name"] if "name" in out["rows"][0] else True
    assert "Forest" in out["rows"][0]["display"]
    assert out["rows"][0]["color"] == "#228822"


def test_nodata_flag():
    spec = {"bidx": [1], "nodata": 0}
    raw = {"values": [0]}
    out = enrich_point_response(raw, spec)
    assert out["rows"][0]["is_nodata"] is True


def test_style_spec_from_request_query_rgb():
    req = _FakeRequest({"bidx": ["3", "2", "1"]})
    spec = style_spec_from_request_query(req)  # type: ignore[arg-type]
    assert spec["bidx"] == [3, 2, 1]


def test_empty_values():
    out = enrich_point_response({"values": []}, {})
    assert out["rows"][0]["display"] == "No raster coverage at this location"


def test_empty_values_with_bidx_spec_no_placeholder_row():
    out = enrich_point_response({"values": []}, {"bidx": [1]})
    assert len(out["rows"]) == 1
    assert out["rows"][0]["role"] == "info"


def test_has_sample_values():
    assert _has_sample_values({"values": [1.0]}) is True
    assert _has_sample_values({"values": [None]}) is False
    assert _has_sample_values({"values": []}) is False


def test_classification_point_params_exclude_colormap():
    from app.services.raster_style_spec import (
        titiler_params_from_classification_style,
        titiler_params_from_classification_style_for_point,
    )

    spec = {
        "style_type": "classification",
        "colormap": {"1": "#ff0000"},
        "bidx": ["1"],
    }
    tile_keys = {k for k, _ in titiler_params_from_classification_style(spec)}
    point_keys = {k for k, _ in titiler_params_from_classification_style_for_point(spec)}
    assert "colormap" in tile_keys
    assert "colormap" not in point_keys
    assert "bidx" in point_keys
