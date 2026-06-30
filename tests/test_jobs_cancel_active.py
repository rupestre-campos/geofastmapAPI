"""Tests for bulk cancel active jobs endpoint."""

from app.api.routes import jobs as jobs_routes
from app.services.job_store import JobInfo


def test_cancel_job_record_skips_completed():
    job = JobInfo(job_id="j1", collection_id="c1", status="completed")
    result = jobs_routes.cancel_job_record(job)
    assert result["cancelled"] is False


def test_cancel_job_record_pending_generic(monkeypatch):
    job = JobInfo(job_id="j2", collection_id="c1", status="pending")
    monkeypatch.setattr(jobs_routes, "is_registered_bulk_import_job", lambda _j: False)
    monkeypatch.setattr(jobs_routes, "get_process_job_meta", lambda _j: None)
    monkeypatch.setattr(jobs_routes, "_is_latest_tile_build_job", lambda *_a: False)
    updates = []
    monkeypatch.setattr(jobs_routes, "update_job", lambda jid, **kw: updates.append((jid, kw)))

    result = jobs_routes.cancel_job_record(job)
    assert result["cancelled"] is True
    assert updates and updates[0][1]["status"] == "cancelled"
