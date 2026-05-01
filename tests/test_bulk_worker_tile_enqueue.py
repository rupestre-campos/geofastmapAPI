from app.services.bulk_queue import BulkJobPayload
from app.services import bulk_worker as bw


def test_parent_fallback_enqueues_tiles_when_requested(monkeypatch):
    payload = BulkJobPayload(
        job_id="job-parent",
        collection_id="c-1",
        storage_key="job-parent.geojson",
        mode="append",
        batch_size=1000,
        owner_id=17,
        queue_compute_tiles=True,
        job_kind="parent",
    )
    called = {"queue": []}

    monkeypatch.setattr(bw, "run_bulk_import_sync", lambda *a, **k: (11, 0, None))
    monkeypatch.setattr(bw, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(
        bw,
        "_queue_tile_build_if_requested",
        lambda collection_id, owner_id, queue_requested: called["queue"].append(
            (collection_id, owner_id, queue_requested)
        ),
    )

    bw._process_parent_bulk_job(payload, "/tmp/input.geojson")

    assert called["queue"] == [("c-1", 17, True)]


def test_shard_finalizer_enqueues_tiles_when_parent_requests(monkeypatch):
    payload = BulkJobPayload(
        job_id="job-parent:shard:1",
        collection_id="c-2",
        storage_key="job-parent.shard.0001.geojsonl",
        mode="append",
        batch_size=1000,
        owner_id=33,
        queue_compute_tiles=False,
        job_kind="shard",
        parent_job_id="job-parent",
        shard_index=1,
        shard_total=1,
        finalize_collection=False,
    )
    called = {"queue": []}

    class _S:
        bulk_queue_type = "redis"

    class _Storage:
        def delete(self, _k):
            return None

    monkeypatch.setattr(bw, "get_settings", lambda: _S())
    monkeypatch.setattr(bw, "run_bulk_import_sync", lambda *a, **k: (7, 0, None))
    monkeypatch.setattr(bw, "get_bulk_storage", lambda: _Storage())
    monkeypatch.setattr(
        bw,
        "record_parent_shard_result",
        lambda **_k: {
            "expected_shards": 1,
            "completed_shards": 1,
            "failed_shards": 0,
            "items_created": 7,
            "items_failed": 0,
            "error_samples_json": "[]",
            "queue_compute_tiles": True,
        },
    )
    monkeypatch.setattr(bw, "finalize_collection_import_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(bw, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(bw, "unregister_bulk_import_job", lambda *_a, **_k: None)
    monkeypatch.setattr(bw, "clear_parent_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bw,
        "_queue_tile_build_if_requested",
        lambda collection_id, owner_id, queue_requested: called["queue"].append(
            (collection_id, owner_id, queue_requested)
        ),
    )

    bw._process_shard_bulk_job(payload, "/tmp/shard.geojsonl")

    assert called["queue"] == [("c-2", 33, True)]
