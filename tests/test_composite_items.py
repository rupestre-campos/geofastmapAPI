"""Tests for composite items fan-out helpers."""

from __future__ import annotations

from app.services.composite_items import (
    format_composite_item_id,
    parse_composite_item_id,
)


def test_composite_item_id_roundtrip():
    cid = format_composite_item_id("layer_a", "feat-1")
    assert cid == "layer_a:feat-1"
    assert parse_composite_item_id(cid) == ("layer_a", "feat-1")


def test_parse_composite_item_id_invalid():
    assert parse_composite_item_id("no-separator") is None
    assert parse_composite_item_id(":bad") is None
