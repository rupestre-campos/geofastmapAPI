"""Mosaic version id: deterministic and sensitive to item membership."""

from app.services.raster_mosaic_version import (
    compute_mosaic_version_id,
    mosaic_mv_matches_request,
)


def test_mv_changes_when_item_added():
    c = "coll-a"
    a = compute_mosaic_version_id(c, ["x"])
    b = compute_mosaic_version_id(c, ["x", "y"])
    assert a and b
    assert a != b


def test_mv_changes_when_item_removed():
    c = "coll-a"
    full = compute_mosaic_version_id(c, ["a", "b", "c"])
    minus_b = compute_mosaic_version_id(c, ["a", "c"])
    assert full and minus_b
    assert full != minus_b


def test_mv_stable_for_same_ordered_ids():
    c = "coll-a"
    v1 = compute_mosaic_version_id(c, ["id-a", "id-b"])
    v2 = compute_mosaic_version_id(c, ["id-a", "id-b"])
    assert v1 == v2
    assert len(v1) == 64


def test_mv_empty_collection_none():
    assert compute_mosaic_version_id("c", []) is None


def test_mv_matches_full_and_legacy_prefix():
    full = compute_mosaic_version_id("z", ["only"])
    assert full
    assert mosaic_mv_matches_request(full, full) is True
    assert mosaic_mv_matches_request(full[:16], full) is True
    assert mosaic_mv_matches_request("wrong", full) is False
    assert mosaic_mv_matches_request(None, full) is False
