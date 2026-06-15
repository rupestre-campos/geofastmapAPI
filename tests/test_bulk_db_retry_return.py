"""_run_db_retry must return fn() result (e.g. delete batch rowcount)."""

from app.services import bulk_import as bi


def test_run_db_retry_returns_fn_value():
    assert bi._run_db_retry("test", lambda: 42) == 42


def test_delete_features_batched_compares_retry_return(monkeypatch):
    """Regression: n <= 0 must not compare None when _run_db_retry returns rowcount."""
    state = {"calls": 0}

    class _Result:
        rowcount = 5

    class _Conn:
        def execute(self, *_a, **_k):
            return _Result()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

    monkeypatch.setattr(bi, "resolve_features_partition_relname_sync", lambda *_a: None)
    monkeypatch.setattr(bi, "_delete_where_clause", lambda *_a: True)
    monkeypatch.setattr(bi, "_raise_if_bulk_cancelled", lambda *_a: None)
    monkeypatch.setattr(bi, "get_collection_bulk_mutex_holder", lambda *_a: None)
    monkeypatch.setattr(bi, "get_settings", lambda: type("S", (), {"bulk_replace_delete_batch_rows": 25_000})())

    def fake_retry(_label, fn):
        state["calls"] += 1
        return fn() if state["calls"] == 1 else 0

    monkeypatch.setattr(bi, "_run_db_retry", fake_retry)

    total = bi._delete_features_batched_sync(_Engine(), "coll-1")
    assert total == 5
