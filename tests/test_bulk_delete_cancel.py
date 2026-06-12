"""Cooperative cancel during bulk replace delete."""

import pytest

from app.services.bulk_import import BulkImportCancelled, _raise_if_bulk_cancelled


def test_raise_if_bulk_cancelled_when_job_cancelled(monkeypatch):
    from app.services import bulk_import as bi

    monkeypatch.setattr(bi, "get_job", lambda _jid: type("J", (), {"status": "cancelled"})())
    with pytest.raises(BulkImportCancelled):
        _raise_if_bulk_cancelled("job-1")
