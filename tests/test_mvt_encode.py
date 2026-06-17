"""Tests for in-process MVT encoding (dynamic tiles search-cache path)."""
import json

import mapbox_vector_tile

from app.services.mvt_encode import encode_geojson_to_mvt
from app.utils.geo import mvt_layer_name


def test_encode_geojson_to_mvt_sanitizes_layer_name():
    """Layer name must match mvt_layer_name so MapLibre source-layer matches the PBF."""
    collection_id = "car-area_fall-df"
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "1",
                "properties": {"name": "test"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-47.9, -15.8],
                            [-47.8, -15.8],
                            [-47.8, -15.7],
                            [-47.9, -15.7],
                            [-47.9, -15.8],
                        ]
                    ],
                },
            }
        ],
    }
    tile_bytes = encode_geojson_to_mvt(
        json.dumps(geojson).encode("utf-8"),
        collection_id,
        z=10,
        x=500,
        y=500,
    )
    assert tile_bytes
    decoded = mapbox_vector_tile.decode(tile_bytes)
    assert list(decoded.keys()) == [mvt_layer_name(collection_id)]
    assert decoded[mvt_layer_name(collection_id)]
