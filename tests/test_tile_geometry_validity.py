"""Tests for geometry validity helpers used by tile paths."""

from shapely.geometry import Polygon

from app.utils.geo import ensure_valid_shapely


def test_ensure_valid_shapely_fixes_bowtie():
    # Classic self-intersecting bow-tie
    bad = Polygon([(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)])
    assert not bad.is_valid
    fixed = ensure_valid_shapely(bad)
    assert fixed is not None
    assert not fixed.is_empty
    assert fixed.is_valid


def test_ensure_valid_shapely_leaves_valid():
    good = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    assert good.is_valid
    out = ensure_valid_shapely(good)
    assert out.equals(good)
