"""Tests for fast collection features wipe (DETACH + DROP TABLE)."""

from __future__ import annotations

from app.db import features_partitions as fp


class _FakeConn:
    def __init__(self):
        self.statements: list[str] = []
        self._part = "features_car_area_imovel_abcdef12"
        self._bound = "FOR VALUES IN ('car_area_imovel')"

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        if "pg_inherits" in sql and "relpartbound" in sql:
            class _R:
                def fetchall(self_inner):
                    return [type("Row", (), {"relname": self._part, "bound": self._bound})()]

            return _R()
        if "pg_inherits" in sql and "c.relname = :name" in sql:
            return type("R", (), {"first": lambda self_inner: (1,)})()
        if "pg_class" in sql and "relkind" in sql:
            # table exists check for canonical leftover
            return type("R", (), {"first": lambda self_inner: None})()
        return type("R", (), {"first": lambda self_inner: None, "fetchall": lambda self_inner: []})()


class _FakeBegin:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return _FakeBegin(self.conn)


def test_drop_collection_features_detaches_and_drops(monkeypatch):
    conn = _FakeConn()
    engine = _FakeEngine(conn)
    monkeypatch.setattr(
        fp,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "bulk_swap_lock_timeout_seconds": 5.0,
                "bulk_swap_lock_max_wait_seconds": 30.0,
                "bulk_skip_features_touch_trigger": True,
            },
        )(),
    )
    monkeypatch.setattr(fp, "_safe_partition_name", lambda _cid: "features_car_area_imovel_abcdef12")

    dropped = fp.drop_collection_features_data_sync(engine, "car_area_imovel")
    assert dropped == "features_car_area_imovel_abcdef12"
    joined = "\n".join(conn.statements)
    assert "DETACH PARTITION" in joined
    assert 'DROP TABLE IF EXISTS "features_car_area_imovel_abcdef12"' in joined
    assert "DELETE FROM features_default" in joined
