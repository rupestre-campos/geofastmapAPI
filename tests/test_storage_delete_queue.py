"""Tests for storage delete queue."""

import json

from app.services.storage_delete_queue import StorageDeletePayload


def test_storage_delete_payload_roundtrip():
    p = StorageDeletePayload(
        job_id="j1",
        action="delete_collection",
        target_id="my-layer",
        owner_id=1,
    )
    raw = json.loads(p.to_json())
    back = StorageDeletePayload.from_json(p.to_json())
    assert back.job_id == "j1"
    assert back.action == "delete_collection"
    assert back.target_id == "my-layer"
    assert back.owner_id == 1
    assert raw["action"] == "delete_collection"
