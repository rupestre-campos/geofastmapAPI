"""Regression: empty / mis-routed tile builds must not report success."""

from app.services.tile_build_queue import TileBuildPayload


def test_tile_build_payload_roundtrip_is_composite():
    p = TileBuildPayload(collection_id="c1", job_id="j1", is_composite=True)
    parsed = TileBuildPayload.from_json(p.to_json())
    assert parsed.is_composite is True
    assert parsed.collection_id == "c1"


def test_tile_build_payload_default_not_composite():
    parsed = TileBuildPayload.from_json('{"collection_id":"c1","job_id":"j1"}')
    assert parsed.is_composite is False
