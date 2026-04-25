"""Unit tests for mosaic planner helpers."""

from types import SimpleNamespace

import pytest
from shapely.geometry import MultiPolygon, box, shape

from app.services.mosaic_plan import (
    _aoi_longitude_strips,
    build_mosaicjson_from_footprints,
    Candidate,
    greedy_cover_aoi,
    mgrs_tile_from_stac_item_id,
    pinpoint_bboxes_from_remainder,
    plan_mosaic_from_features,
    season_datetime_slices,
    split_initial_search_bboxes,
    swap_options_for_selected,
    collect_stac_features,
    plan_mosaic_with_void_fill,
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


def test_swap_options_paging_reports_total():
    """swap_options_limit slices alternatives; swap_options_total is full count."""
    aoi = box(0, 0, 1, 1)
    common = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    features = []
    for i, cloud in enumerate([10, 20, 30]):
        sid = f"S2A_MSIL2A_20250{i+1}01_N0000_R000_T23KNS_20250{i+1}01"
        features.append(
            {
                "type": "Feature",
                "id": sid,
                "geometry": {"type": "Polygon", "coordinates": common},
                "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": cloud},
                "collection": "sentinel-2-l2a",
                "assets": {"visual": {"href": f"https://example.com/{i}.tif", "type": "image/tiff"}},
            }
        )
    sel_key = features[0]["id"]
    selected = [{"key": sel_key, "stac_item_id": sel_key, "footprint": {"type": "Polygon", "coordinates": common}}]
    out = swap_options_for_selected(
        aoi, features, "lowest_cloud", selected, swap_options_limit=1, swap_options_offset=None
    )
    assert out["swap_options_total"][sel_key] == 2
    assert len(out["swap_options"][sel_key]) == 1
    out2 = swap_options_for_selected(
        aoi, features, "lowest_cloud", selected, swap_options_limit=1, swap_options_offset={sel_key: 1}
    )
    assert len(out2["swap_options"][sel_key]) == 1
    assert out2["swap_options"][sel_key][0]["id"] != out["swap_options"][sel_key][0]["id"]


def test_swap_options_legacy_bare_id_excludes_current_scene():
    """Saved mosaic rows may use bare STAC id as key; pool uses cat:coll:id dedupe keys."""
    aoi = box(0, 0, 1, 1)
    common = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    sid_a = "S2A_MSIL2A_20250101_N0000_R000_T23KNS_20250101"
    sid_b = "S2A_MSIL2A_20250201_N0000_R000_T23KNS_20250201"
    features = [
        {
            "type": "Feature",
            "id": sid_a,
            "geometry": {"type": "Polygon", "coordinates": common},
            "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 10},
            "collection": "sentinel-2-l2a",
            "assets": {"visual": {"href": "https://example.com/a.tif", "type": "image/tiff"}},
        },
        {
            "type": "Feature",
            "id": sid_b,
            "geometry": {"type": "Polygon", "coordinates": common},
            "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 20},
            "collection": "sentinel-2-l2a",
            "assets": {"visual": {"href": "https://example.com/b.tif", "type": "image/tiff"}},
        },
    ]
    selected = [
        {
            "key": sid_a,
            "stac_item_id": sid_a,
            "footprint": {"type": "Polygon", "coordinates": common},
        }
    ]
    out = swap_options_for_selected(aoi, features, "lowest_cloud", selected)
    alts = out["swap_options"].get(sid_a, [])
    ids = {a["id"] for a in alts}
    assert sid_a not in ids
    assert sid_b in ids


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


def test_split_initial_search_bboxes_large_area():
    bbs = split_initial_search_bboxes([-70.0, -30.0, -10.0, 20.0])
    assert len(bbs) > 1
    for bb in bbs:
        assert len(bb) == 4
        assert bb[2] > bb[0] and bb[3] > bb[1]


def test_split_initial_search_bboxes_small_area_single():
    bbs = split_initial_search_bboxes([10.0, 10.0, 12.0, 12.0])
    assert len(bbs) == 1


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


def _candidate(
    key: str,
    geom,
    cloud: float = 0.0,
) -> Candidate:
    from datetime import datetime

    return Candidate(
        feature={"id": key, "geofast:sourceCatalog": "c", "collection": "x"},
        geom=geom,
        href="https://ex/x.tif",
        key=f"c:x:{key}",
        cloud=cloud,
        dt=datetime(2020, 1, 1),
    )


def test_greedy_cover_prefers_tighter_footprint_at_equal_marginal_gain() -> None:
    """When area gained ties, prefer higher gain/footprint (less wasted scene area)."""
    aoi = box(0, 0, 0.1, 0.1)  # area 0.01; both add same new coverage, one footprint much larger
    c_tight = _candidate("t", box(0, 0, 0.1, 0.1), cloud=0.0)  # r = 1
    c_huge = _candidate("h", box(0, 0, 1, 1), cloud=0.0)  # g = 0.01, a = 1, r = 0.01
    sel, rem, _frac = greedy_cover_aoi(
        aoi, [c_tight, c_huge], "lowest_cloud", min_marginal_coverage_fraction=0.0
    )
    assert len(sel) == 1
    assert "t" in sel[0].key
    assert rem is None or _frac == 0.0


def test_greedy_cover_min_marginal_skips_sliver_additions() -> None:
    """After first cover, do not add scenes whose marginal share of the remaining hole is tiny."""
    aoi = box(0, 0, 1, 1)
    c1 = _candidate("1", box(0, 0, 0.5, 1), cloud=0.0)
    c2 = _candidate("2", box(0.5, 0, 0.55, 0.05), cloud=0.0)
    full, _r0, _ = greedy_cover_aoi(aoi, [c1, c2], "lowest_cloud", min_marginal_coverage_fraction=0.0)
    assert len(full) == 2
    trimmed, _r1, _ = greedy_cover_aoi(
        aoi, [c1, c2], "lowest_cloud", min_marginal_coverage_fraction=0.1
    )
    assert len(trimmed) == 1


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


@pytest.mark.asyncio
async def test_collect_stac_features_parallel_datetime_deterministic(monkeypatch):
    import app.core.config as core_config
    settings = SimpleNamespace(
        mosaic_stac_datetime_parallelism=4,
        mosaic_stac_fetch_limit=500,
        mosaic_void_fill_max_rounds=6,
        mosaic_void_pinpoint_max_parts=16,
        mosaic_void_fill_min_uncovered=0.001,
        mosaic_same_pass_num_strips=8,
    )
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)

    async def fake_execute(_catalogs, body):
        dt = body.get("datetime")
        if "2024-02" in dt:
            feats = [
                {
                    "type": "Feature",
                    "id": "scene-b",
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                    "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 30},
                    "collection": "col",
                    "assets": {"visual": {"href": "https://example.com/b.tif", "type": "image/tiff"}},
                }
            ]
            return {"features": feats}, []
        feats = [
            {
                "type": "Feature",
                "id": "scene-a",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 20},
                "collection": "col",
                "assets": {"visual": {"href": "https://example.com/a.tif", "type": "image/tiff"}},
            },
            {
                "type": "Feature",
                "id": "scene-b",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                "properties": {"geofast:sourceCatalog": "c1", "eo:cloud_cover": 30},
                "collection": "col",
                "assets": {"visual": {"href": "https://example.com/b.tif", "type": "image/tiff"}},
            },
        ]
        return {"features": feats}, []

    import app.api.routes.stac as stac_route

    monkeypatch.setattr(stac_route, "execute_stac_search_for_catalogs", fake_execute)
    catalogs = [SimpleNamespace(id="c1")]
    out, errs = await collect_stac_features(
        catalogs,
        stac_collection="col",
        bbox=[0, 0, 1, 1],
        datetime_slices=["2024-01-01T00:00:00Z/2024-01-31T23:59:59Z", "2024-02-01T00:00:00Z/2024-02-29T23:59:59Z"],
        cloud_cover_max=None,
        sort_mode="newest_first",
        fetch_limit=500,
    )
    assert not errs
    assert [f["id"] for f in out] == ["scene-a", "scene-b"]


@pytest.mark.asyncio
async def test_plan_void_fill_respects_settings_rounds_and_parts(monkeypatch):
    import app.core.config as core_config
    from app.services import mosaic_plan as mp
    settings = SimpleNamespace(
        mosaic_stac_bbox_parallelism=2,
        mosaic_stac_datetime_parallelism=2,
        mosaic_stac_fetch_limit=500,
        mosaic_void_fill_max_rounds=1,
        mosaic_void_pinpoint_max_parts=3,
        mosaic_void_fill_min_uncovered=0.001,
        mosaic_same_pass_num_strips=8,
    )
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)

    called = {"parts": None}

    def fake_pinpoint(_remaining, _clip_bbox, *, max_parts=16):
        called["parts"] = max_parts
        return []

    async def fake_collect(*_args, **_kwargs):
        return [], []

    monkeypatch.setattr(mp, "pinpoint_bboxes_from_remainder", fake_pinpoint)
    monkeypatch.setattr(mp, "collect_stac_features", fake_collect)

    aoi = box(0, 0, 1, 1)
    res, errs, merged = await plan_mosaic_with_void_fill(
        [SimpleNamespace(id="c1")],
        stac_collection="col",
        aoi=aoi,
        search_bbox=[0, 0, 1, 1],
        datetime_slices=["2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"],
        cloud_cover_max=None,
        sort_mode="lowest_cloud",
        fetch_limit=50,
    )
    assert called["parts"] is None or called["parts"] == 3
    assert res["void_fill_rounds"] <= 1
    assert errs == []
    assert merged == []
