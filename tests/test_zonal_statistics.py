"""Unit tests for zonal statistics helpers (Titiler response normalization)."""

from app.services.zonal_statistics import (
    normalize_band_stats,
    normalize_titiler_statistics_payload,
    unique_values_from_band_stats,
)


def test_unique_values_from_categories():
    band = {"categories": [[1, 10], [2, 5], {"value": 3, "count": 2}]}
    assert unique_values_from_band_stats(band) == [
        {"value": 1, "count": 10},
        {"value": 2, "count": 5},
        {"value": 3, "count": 2},
    ]


def test_unique_values_from_histogram():
    band = {"histogram": [[4, 7], [10, 20]]}
    assert unique_values_from_band_stats(band) == [
        {"value": 10, "count": 4},
        {"value": 20, "count": 7},
    ]


def test_normalize_band_stats_continuous():
    band = {
        "min": 1.5,
        "max": 9.0,
        "mean": 4.2,
        "count": 100,
        "std": 1.1,
        "percentile_2": 1.6,
        "percentile_98": 8.8,
    }
    out = normalize_band_stats(band, categorical=False)
    assert out["min"] == 1.5
    assert out["max"] == 9.0
    assert out["mean"] == 4.2
    assert out["count"] == 100
    assert out["std"] == 1.1
    assert out["percentile_2"] == 1.6
    assert "unique_values" not in out


def test_normalize_band_stats_categorical():
    band = {
        "min": 1,
        "max": 2,
        "mean": 1.4,
        "count": 15,
        "std": 0.5,
        "categories": [[1, 10], [2, 5]],
        "majority": 1,
    }
    out = normalize_band_stats(band, categorical=True)
    assert out["unique_values"] == [{"value": 1, "count": 10}, {"value": 2, "count": 5}]
    assert out["majority"] == 1


def test_normalize_titiler_feature_payload():
    raw = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "properties": {
            "statistics": {
                "b1": {"min": 0, "max": 10, "mean": 5, "count": 50, "std": 2},
            }
        },
    }
    out = normalize_titiler_statistics_payload(
        raw,
        categorical=False,
        raster_meta={"collection_id": "dem", "feature_id": "r1"},
        zone_meta={"collection_id": "parcels", "feature_id": "p1"},
    )
    assert out["type"] == "Feature"
    assert out["properties"]["raster"]["collection_id"] == "dem"
    assert out["properties"]["zone"]["feature_id"] == "p1"
    assert out["properties"]["statistics"]["b1"]["mean"] == 5.0


def test_normalize_titiler_feature_collection():
    raw = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "statistics": {
                        "1": {"min": 2, "max": 4, "mean": 3, "valid_pixels": 8, "stddev": 0.5},
                    }
                },
            }
        ],
    }
    out = normalize_titiler_statistics_payload(raw, categorical=False)
    assert out["properties"]["statistics"]["1"]["count"] == 8
    assert out["properties"]["statistics"]["1"]["std"] == 0.5
