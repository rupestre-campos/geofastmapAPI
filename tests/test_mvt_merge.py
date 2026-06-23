"""Tests for MVT merge (composite static tiles)."""

from app.services.mvt_merge import compute_composite_tiles_revision, merge_mvt_tiles


def test_merge_mvt_tiles_empty():
    assert merge_mvt_tiles([], "layer", 0, 0, 0) == b""


def test_compute_composite_tiles_revision_order_matters():
    r1 = compute_composite_tiles_revision(["aaa", "bbb"])
    r2 = compute_composite_tiles_revision(["bbb", "aaa"])
    assert r1 != r2
    assert len(r1) == 16
