"""Map definition: dedupe duplicate saved-mosaic layers."""

from app.api.routes.maps import _dedupe_map_definition_mosaic_layers

_VIEW = "019d4644-bd1c-7a3c-b7ca-4e001eb6ce1a"
_TILES = f"https://example.com/raster-views/{_VIEW}/titiler/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"


def test_dedupe_mosaic_layers_by_view_id_and_tiles_url():
    definition = {
        "layers": [
            {
                "collection_id": "_mosaic",
                "layer_id": f"mosaic-{_VIEW}",
                "raster_tiles": True,
                "mosaic_view_id": _VIEW,
                "tiles_url": _TILES,
            },
            {
                "collection_id": "_mosaic",
                "layer_id": "other-id",
                "raster_tiles": True,
                "tiles_url": _TILES,
            },
            {
                "collection_id": "vectors",
                "layer_id": "vectors-1",
                "style_spec": {},
            },
        ]
    }
    out = _dedupe_map_definition_mosaic_layers(definition)
    assert len(out["layers"]) == 2
    mosaic_layers = [ly for ly in out["layers"] if ly.get("mosaic_view_id") == _VIEW]
    assert len(mosaic_layers) == 1
    assert mosaic_layers[0]["layer_id"] == f"mosaic-{_VIEW}"
