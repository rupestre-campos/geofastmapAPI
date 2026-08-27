"""Composite collection tile revision and item id helpers."""

from app.services.composite_items import format_composite_item_id, parse_composite_item_id
from app.services.mvt_merge import compute_composite_tiles_revision


def test_composite_tiles_revision_order_sensitive():
    a = compute_composite_tiles_revision(["rev1", "rev2"])
    b = compute_composite_tiles_revision(["rev2", "rev1"])
    assert a != b
    assert len(a) == 16


def test_composite_item_id_roundtrip():
    mid, fid = "coll_a", "feat-1"
    comp = format_composite_item_id(mid, fid)
    parsed = parse_composite_item_id(comp)
    assert parsed == (mid, fid)
