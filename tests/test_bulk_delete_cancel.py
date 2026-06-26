"""Cooperative cancel during bulk replace delete."""

import pytest

from app.services.bulk_import import BulkImportCancelled, _raise_if_bulk_cancelled


def test_raise_if_bulk_cancelled_when_job_cancelled(monkeypatch):
    from app.services import bulk_import as bi

    monkeypatch.setattr(bi, "get_job", lambda _jid: type("J", (), {"status": "cancelled"})())
    with pytest.raises(BulkImportCancelled):
        _raise_if_bulk_cancelled("job-1")


def _mock_engine(tagged: int, other: int, *, on_delete=None):
    class _Result:
        pass

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, stmt, params=None):
            sql = str(stmt)
            if "COUNT(*)" in sql:
                row = _Result()
                row.tagged = tagged
                row.other = other
                return type("R", (), {"first": lambda self: row})()
            if on_delete is not None:
                on_delete()
            return type("R", (), {"first": lambda self: None})()

        def commit(self):
            pass

    class _Engine:
        def connect(self):
            return _Conn()

    return _Engine()


def test_cancel_cleanup_truncates_when_only_job_rows(monkeypatch):
    from app.services import bulk_import as bi

    calls = {"truncate": 0, "delete": 0}

    monkeypatch.setattr(
        bi,
        "_truncate_collection_features_sync",
        lambda *a, **k: calls.__setitem__("truncate", calls["truncate"] + 1),
    )
    monkeypatch.setattr(bi, "_update_feature_count_sync", lambda *a, **k: None)
    monkeypatch.setattr(
        bi,
        "collections_crud",
        type("C", (), {"recompute_and_update_collection_extent_sync": staticmethod(lambda *a, **k: None)})(),
    )

    bi._sync_delete_bulk_import_rows_and_refresh(
        _mock_engine(1_000_000, 0), "car-apps-go", "job-1"
    )
    assert calls["truncate"] == 1
    assert calls["delete"] == 0


def test_cancel_cleanup_deletes_by_job_when_mixed_rows(monkeypatch):
    from app.services import bulk_import as bi

    calls = {"truncate": 0, "delete": 0}

    monkeypatch.setattr(
        bi,
        "_truncate_collection_features_sync",
        lambda *a, **k: calls.__setitem__("truncate", calls["truncate"] + 1),
    )
    monkeypatch.setattr(bi, "_run_db_retry", lambda label, fn: fn())
    monkeypatch.setattr(bi, "_update_feature_count_sync", lambda *a, **k: None)
    monkeypatch.setattr(
        bi,
        "collections_crud",
        type("C", (), {"recompute_and_update_collection_extent_sync": staticmethod(lambda *a, **k: None)})(),
    )

    bi._sync_delete_bulk_import_rows_and_refresh(
        _mock_engine(50_000, 2_000_000, on_delete=lambda: calls.__setitem__("delete", 1)),
        "car_area_imovel",
        "job-1",
    )
    assert calls["truncate"] == 0
    assert calls["delete"] == 1


def test_cancel_cleanup_noop_when_no_tagged_rows(monkeypatch):
    from app.services import bulk_import as bi

    calls = {"truncate": 0, "count": 0}

    monkeypatch.setattr(
        bi,
        "_truncate_collection_features_sync",
        lambda *a, **k: calls.__setitem__("truncate", calls["truncate"] + 1),
    )
    monkeypatch.setattr(
        bi,
        "_update_feature_count_sync",
        lambda *a, **k: calls.__setitem__("count", calls["count"] + 1),
    )
    monkeypatch.setattr(
        bi,
        "collections_crud",
        type("C", (), {"recompute_and_update_collection_extent_sync": staticmethod(lambda *a, **k: None)})(),
    )

    bi._sync_delete_bulk_import_rows_and_refresh(_mock_engine(0, 500), "car-apps-go", "job-1")
    assert calls["truncate"] == 0
    assert calls["count"] == 1
