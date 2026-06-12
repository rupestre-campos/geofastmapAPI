"""Partition naming and full-replace truncate path."""

from app.db.features_partitions import _safe_partition_name, _partition_bound_literal


def test_safe_partition_name_stable():
    n1 = _safe_partition_name("car-consolidated_area-to")
    n2 = _safe_partition_name("car-consolidated_area-to")
    assert n1 == n2
    assert n1.startswith("features_")


def test_partition_bound_literal_escapes_quotes():
    assert "''" in _partition_bound_literal("a'b")


def test_truncate_replace_calls_ensure(monkeypatch):
    from app.services import bulk_import as bi

    calls = {"ensure": 0, "truncate": 0}

    class _Conn:
        def execute(self, *a, **k):
            sql = str(a[0])
            if "TRUNCATE" in sql:
                calls["truncate"] += 1
            return type("R", (), {"rowcount": 0})()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

    monkeypatch.setattr(bi, "ensure_features_partition_sync", lambda *_a, **_k: (calls.__setitem__("ensure", calls["ensure"] + 1) or "features_test_abcd1234"))
    monkeypatch.setattr(bi, "_run_db_retry", lambda _label, fn: fn())
    monkeypatch.setattr(bi, "get_collection_bulk_mutex_holder", lambda *_a: None)

    bi._truncate_collection_features_sync(_Engine(), "coll-1")
    assert calls["ensure"] == 1
    assert calls["truncate"] == 1
