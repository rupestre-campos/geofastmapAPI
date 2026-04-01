"""Unit tests for STAC federation merge (no HTTP)."""

from app.services.stac_federation import _merge_item_collections


def test_merge_item_collections_two_features():
    parts = [
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "b", "properties": {}},
            ],
        },
    ]
    out = _merge_item_collections(parts, catalog_labels=["c1", "c2"])
    assert out["type"] == "FeatureCollection"
    assert len(out["features"]) == 2
    assert out["features"][0]["properties"]["geofast:sourceCatalog"] == "c1"
    assert out["features"][1]["properties"]["geofast:sourceCatalog"] == "c2"


def test_merge_dedupes_same_catalog_and_id():
    parts = [
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
    ]
    out = _merge_item_collections(parts, catalog_labels=["c1", "c1"])
    assert len(out["features"]) == 1
