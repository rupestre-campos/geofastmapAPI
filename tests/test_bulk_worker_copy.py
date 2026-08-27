"""Tests for single-file bulk worker (COPY ingest path)."""

from app.services import bulk_worker as bw
from app.services.bulk_queue import BulkJobPayload


def test_run_vector_import_rejects_replace_filtered_when_copy_enabled(monkeypatch):
    monkeypatch.setattr(
        bw,
        "get_settings",
        lambda: type("S", (), {"bulk_copy_ingest_enabled": True})(),
    )
    payload = BulkJobPayload(
        job_id="j1",
        collection_id="c1",
        storage_key="f.geojsonl",
        mode="replace_filtered",
        batch_size=1000,
        replace_filters=["state_id:eq:12"],
    )
    created, failed, err = bw._run_vector_import("/tmp/x.geojsonl", payload, lambda *a, **k: None)
    assert created == 0
    assert "replace_filtered" in (err or "")


def test_legacy_parent_payload_treated_as_single(monkeypatch, tmp_path):
    """Legacy queued parent jobs should not crash; they run as a single import."""
    called = {"n": 0}

    def fake_copy(*_a, **_k):
        called["n"] += 1
        return (1, 0, None)

    monkeypatch.setattr(bw, "run_bulk_copy_import_sync", fake_copy)
    monkeypatch.setattr(bw, "get_settings", lambda: type("S", (), {"bulk_copy_ingest_enabled": True})())
    monkeypatch.setattr(bw, "_defer_bulk_job_for_collection_mutex", lambda _p: False)
    monkeypatch.setattr(bw, "get_job", lambda _j: None)
    monkeypatch.setattr(bw, "update_job", lambda *_a, **_k: None)
    monkeypatch.setattr(bw, "_release_bulk_collection_mutex", lambda _p: None)
    monkeypatch.setattr(bw, "_queue_tile_build_if_requested", lambda *_a, **_k: None)

    storage = tmp_path / "upload.geojsonl"
    storage.write_text('{"type":"Feature","geometry":{"type":"Point","coordinates":[0,0]},"properties":{}}\n')

    class _Storage:
        def get_path_or_uri(self, key):
            return str(storage)

        def delete(self, _key):
            pass

    monkeypatch.setattr(bw, "get_bulk_storage", lambda: _Storage())
    monkeypatch.setattr(bw, "unregister_bulk_import_job", lambda _j: None)

    payload = BulkJobPayload(
        job_id="parent-legacy",
        collection_id="c1",
        storage_key="upload.geojsonl",
        mode="append",
        batch_size=1000,
        job_kind="parent",
    )
    bw.process_bulk_job(payload)
    assert called["n"] == 1
