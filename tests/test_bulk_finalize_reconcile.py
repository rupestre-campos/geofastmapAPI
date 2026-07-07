"""Tests for bulk finalize reconcile candidate selection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services.bulk_finalize_reconcile import _is_reconcile_candidate, _load_still_in_progress


def _job(**kwargs):
    defaults = {
        "job_id": "j1",
        "collection_id": "col-a",
        "status": "replacing",
        "message": "Loading replacement data into staging…",
        "items_created": 0,
        "items_failed": 0,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "last_progress_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_active_replace_load_not_reconcile_candidate():
    job = _job(status="replacing", items_created=0)
    with patch(
        "app.services.bulk_finalize_reconcile.holds_collection_bulk_mutex",
        return_value=False,
    ):
        assert _load_still_in_progress(job)
        assert not _is_reconcile_candidate(job)


def test_finalizing_stuck_is_candidate():
    job = _job(
        status="finalizing",
        message="Partition swap failed (attempt 3)",
        items_created=500_000,
        updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        last_progress_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    with patch(
        "app.services.bulk_finalize_reconcile.holds_collection_bulk_mutex",
        return_value=False,
    ):
        assert _is_reconcile_candidate(job)


def test_running_with_staged_rows_not_loading_if_not_active_status():
    job = _job(
        status="running",
        items_created=250_000,
        message="running",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        last_progress_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    with patch(
        "app.services.bulk_finalize_reconcile.holds_collection_bulk_mutex",
        return_value=False,
    ):
        assert _load_still_in_progress(job)
