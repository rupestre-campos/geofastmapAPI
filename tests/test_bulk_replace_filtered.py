"""Tests for replace_filtered bulk import mode."""

import pytest
from fastapi import HTTPException

from app.services.bulk_import_params import validate_bulk_import_mode_and_filters
from app.services.bulk_queue import BulkJobPayload
from app.utils.property_filters import PropertyOp


def test_validate_replace_filtered_requires_filters(copy_ingest_disabled):
    with pytest.raises(HTTPException) as exc:
        validate_bulk_import_mode_and_filters("replace_filtered", [])
    assert exc.value.status_code == 400


def test_validate_replace_filtered_parses_lines(copy_ingest_disabled):
    mode, lines = validate_bulk_import_mode_and_filters(
        "replace_filtered",
        ["state_id:eq:12", "date:lt:2026-07-12"],
    )
    assert mode == "replace_filtered"
    assert lines == ["state_id:eq:12", "date:lt:2026-07-12"]


def test_validate_replace_filters_forbidden_on_append():
    with pytest.raises(HTTPException) as exc:
        validate_bulk_import_mode_and_filters("append", ["state_id:eq:12"])
    assert exc.value.status_code == 400


def test_validate_replace_filters_newline_string(copy_ingest_disabled):
    mode, lines = validate_bulk_import_mode_and_filters(
        "replace_filtered",
        "state_id:eq:12\ndate:lt:2026-07-12",
    )
    assert mode == "replace_filtered"
    assert len(lines) == 2


def test_bulk_payload_roundtrip_replace_filters():
    p = BulkJobPayload(
        job_id="j1",
        collection_id="c1",
        storage_key="f.geojson",
        mode="replace_filtered",
        batch_size=500,
        replace_filters=["state_id:eq:12"],
    )
    out = BulkJobPayload.from_json(p.to_json())
    assert out.mode == "replace_filtered"
    assert out.replace_filters == ["state_id:eq:12"]
    parsed = out.parsed_replace_filters()
    assert len(parsed) == 1
    assert parsed[0].key == "state_id"
    assert parsed[0].op == PropertyOp.EQ
    assert parsed[0].value == "12"


def test_bulk_session_replace_filtered_stores_filters(monkeypatch):
    from app.services import bulk_upload_sessions as bus

    store = {}

    class _FakeRedis:
        def set(self, k, v, ex=None):
            store[k] = v

        def get(self, k):
            return store.get(k)

        def delete(self, k):
            store.pop(k, None)

    monkeypatch.setattr(bus, "_redis", lambda: _FakeRedis())
    monkeypatch.setattr(bus, "run_redis_retry", lambda _label, fn, **_k: fn())
    monkeypatch.setattr(bus, "get_settings", lambda: type("S", (), {"bulk_upload_session_ttl_seconds": 60})())

    s = bus.create_upload_session(
        collection_id="filt_coll2",
        owner_id=1,
        filename="a.geojson",
        mode="replace_filtered",
        batch_size=1000,
        queue_compute_tiles=True,
        extra={"replace_filters": ["state_id:eq:12"]},
    )
    upload_id = s["upload_id"]
    got = bus.get_upload_session(upload_id)
    assert got is not None
    assert got["mode"] == "replace_filtered"
    assert got["replace_filters"] == ["state_id:eq:12"]


@pytest.fixture
def copy_ingest_disabled(monkeypatch):
    from app.services import bulk_import_params as bip

    monkeypatch.setattr(
        bip,
        "get_settings",
        lambda: type("S", (), {"bulk_copy_ingest_enabled": False})(),
    )


def test_process_bulk_job_rejects_replace_filtered_copy_path(monkeypatch, tmp_path):
    from app.services import bulk_worker as bw

    monkeypatch.setattr(bw, "get_settings", lambda: type("S", (), {"bulk_copy_ingest_enabled": True})())
    monkeypatch.setattr(bw, "_defer_bulk_job_for_collection_mutex", lambda _p: False)
    monkeypatch.setattr(bw, "get_job", lambda _j: None)

    updates = []
    monkeypatch.setattr(bw, "update_job", lambda *_a, **kw: updates.append(kw))
    monkeypatch.setattr(bw, "_release_bulk_collection_mutex", lambda _p: None)
    monkeypatch.setattr(bw, "_queue_tile_build_if_requested", lambda *_a, **_k: None)

    storage = tmp_path / "upload.geojsonl"
    storage.write_text("{}\n")

    class _Storage:
        def get_path_or_uri(self, key):
            return str(storage)

        def delete(self, _key):
            pass

    monkeypatch.setattr(bw, "get_bulk_storage", lambda: _Storage())
    monkeypatch.setattr(bw, "unregister_bulk_import_job", lambda _j: None)

    payload = BulkJobPayload(
        job_id="parent-1",
        collection_id="c-filter",
        storage_key="upload.geojsonl",
        mode="replace_filtered",
        batch_size=100,
        replace_filters=["state_id:eq:12"],
        job_kind="parent",
    )
    bw.process_bulk_job(payload)
    assert any(u.get("status") == "failed" for u in updates)
    assert any("replace_filtered" in (u.get("message") or "") for u in updates)
