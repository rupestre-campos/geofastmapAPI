"""Unit tests for mosaic planner helpers."""

from shapely.geometry import MultiPolygon, box, shape

from app.services.mosaic_plan import (
    _aoi_longitude_strips,
    build_mosaicjson_from_footprints,
    greedy_cover_aoi,
    mgrs_tile_from_stac_item_id,
    pinpoint_bboxes_from_remainder,
    plan_mosaic_from_features,
    season_datetime_slices,
)
from app.core.permissions import can_access_raster_view_tiles_anonymous


def test_season_datetime_slices_full_range():
    slices = season_datetime_slices("2024-01-01", "2024-12-31", None)
    # Wide span with no seasons: split into calendar months for STAC resilience
    assert len(slices) >= 2
    assert "2024" in slices[0]


def test_season_datetime_slices_short_range_single_slice():
    slices = season_datetime_slices("2024-06-01", "2024-06-30", None)
    assert len(slices) == 1
    assert "2024-06" in slices[0]


def test_season_datetime_slices_summer():
    slices = season_datetime_slices("2024-01-01", "2024-12-31", ["summer"])
    assert slices
    assert any("2024-06" in s or "2024-07" in s or "2024-08" in s for s in slices)


def test_mgrs_tile_from_item_id():
    assert mgrs_tile_from_stac_item_id("S2A_MSIL2A_20250810T140849_N0510_R080_T23KNS_20250810T141000") == "23KNS"
    assert mgrs_tile_from_stac_item_id("S2A_23KNS_20250810_1_L2A") == "23KNS"


def test_swap_options_filters_same_mgrs_tile():
    """Swap alternatives share the same Sentinel-2 MGRS tile id (e.g. 23KNS)."""
    aoi = box(0, 0, 1, 1)
    common = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    features = [
        {
            "type": "Feature",
            "id": "S2A_MSIL2A_20250101_N0000_R000_T23KNS_20250101",
            "geometry": {"type": "Polygon", "coordinates": common},
            "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 10},
            "collection": "sentinel-2-l2a",
            "assets": {"visual": {"href": "https://example.com/a.tif", "type": "image/tiff"}},
        },
        {
            "type": "Feature",
            "id": "S2A_MSIL2A_20250201_N0000_R000_T23KNS_20250201",
            "geometry": {"type": "Polygon", "coordinates": common},
            "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 20},
            "collection": "sentinel-2-l2a",
            "assets": {"visual": {"href": "https://example.com/b.tif", "type": "image/tiff"}},
        },
    ]
    out = plan_mosaic_from_features(aoi, features, "lowest_cloud")
    assert len(out["selected"]) == 1
    sel = out["selected"][0]
    assert sel.get("mgrs_tile") == "23KNS"
    alts = out["swap_options"].get(sel["key"], [])
    assert len(alts) == 1
    assert alts[0]["mgrs_tile"] == "23KNS"
    assert alts[0]["id"] == "S2A_MSIL2A_20250201_N0000_R000_T23KNS_20250201"


def test_aoi_longitude_strips_splits_square():
    g = box(0, 0, 1, 1)
    strips = _aoi_longitude_strips(g, 4)
    assert len(strips) >= 2


def test_pinpoint_bboxes_one_small_search_per_gap():
    """Void-fill uses small bboxes per disconnected gap (same idea as click-to-fill)."""
    clip = [-10.0, -10.0, 10.0, 10.0]
    g = MultiPolygon([box(0, 0, 0.08, 0.08), box(5, 5, 5.15, 5.15)])
    bbs = pinpoint_bboxes_from_remainder(g, clip)
    assert len(bbs) >= 2
    for bb in bbs:
        assert len(bb) == 4
        assert bb[2] > bb[0] and bb[3] > bb[1]


def test_footprint_prefers_geometry_over_bbox():
    """Item `geometry` (true data extent) wins over axis-aligned `bbox` when both exist."""
    aoi = box(0, 0, 2, 2)
    inner = [[[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]]
    features = [
        {
            "type": "Feature",
            "id": "geom-smaller-than-bbox",
            "bbox": [0, 0, 2, 2],
            "geometry": {"type": "Polygon", "coordinates": inner},
            "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 0},
            "collection": "col",
            "assets": {"visual": {"href": "https://example.com/a.tif", "type": "image/tiff"}},
        }
    ]
    out = plan_mosaic_from_features(aoi, features, "lowest_cloud")
    assert len(out["selected"]) >= 1
    fp = out["selected"][0]["footprint"]
    g = shape(fp)
    assert g.bounds[0] >= 0.4 and g.bounds[2] <= 1.6
    assert "remaining_uncovered" in out


def test_greedy_cover_simple():
    aoi = box(0, 0, 1, 1)
    features = [
        {
            "type": "Feature",
            "id": "a",
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            "properties": {"geofast:sourceCatalog": "c1"},
            "collection": "col",
            "assets": {"visual": {"href": "https://example.com/a.tif", "type": "image/tiff"}},
        }
    ]
    out = plan_mosaic_from_features(aoi, features, "lowest_cloud")
    assert len(out["selected"]) >= 1


def test_build_mosaicjson():
    g = box(-1, -1, 1, 1)
    mj = build_mosaicjson_from_footprints([("https://example.com/x.tif", g)], minzoom=6, maxzoom=12)
    assert mj.get("mosaicjson") == "0.0.3"
    assert "tiles" in mj
    assert mj["bounds"]


def test_can_access_raster_tiles_anonymous():
    assert can_access_raster_view_tiles_anonymous(visibility="public", allow_public_maps=False)
    assert can_access_raster_view_tiles_anonymous(visibility="private", allow_public_maps=True)
    assert not can_access_raster_view_tiles_anonymous(visibility="private", allow_public_maps=False)
