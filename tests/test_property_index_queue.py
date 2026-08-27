"""Tests for property index job payload / scheduling helpers."""

from app.services.property_index_queue import PROPERTY_INDEX_JOB_LABEL, PropertyIndexPayload


def test_property_index_payload_roundtrip():
    p = PropertyIndexPayload(
        job_id="j1",
        collection_id="c1",
        old_fields=["a"],
        new_fields=["a", "b"],
        is_composite=True,
        composite_members=[{"collection_id": "m1"}],
    )
    parsed = PropertyIndexPayload.from_json(p.to_json())
    assert parsed.job_id == "j1"
    assert parsed.is_composite is True
    assert parsed.new_fields == ["a", "b"]
    assert parsed.composite_members == [{"collection_id": "m1"}]
    assert PROPERTY_INDEX_JOB_LABEL == "property_index"
