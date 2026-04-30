from app.core.config import get_settings
from app.services import bulk_collection_activity as bca


def test_wait_until_idle_immediate_when_bulk_queue_not_redis(monkeypatch):
    monkeypatch.setenv("BULK_QUEUE_TYPE", "memory")
    get_settings.cache_clear()
    try:
        msgs = []

        ok = bca.wait_until_collection_bulk_idle(
            "coll-a",
            stop_check=lambda: False,
            on_waiting_message=lambda: msgs.append("msg"),
        )

        assert ok is True
        assert msgs == []
    finally:
        get_settings.cache_clear()


def test_wait_until_idle_polls_until_clear(monkeypatch):
    monkeypatch.setenv("BULK_QUEUE_TYPE", "redis")
    get_settings.cache_clear()
    try:
        checks = {"n": 0}

        def fake_has(_cid: str) -> bool:
            checks["n"] += 1
            return checks["n"] < 3

        sleeps: list[float] = []

        monkeypatch.setattr(bca, "collection_has_bulk_activity", fake_has)
        monkeypatch.setattr(bca.time, "sleep", lambda s: sleeps.append(s))

        ok = bca.wait_until_collection_bulk_idle("coll-b", stop_check=lambda: False, poll_seconds=0.5)

        assert ok is True
        assert checks["n"] == 3
        assert sleeps == [0.5, 0.5]
    finally:
        get_settings.cache_clear()


def test_wait_until_idle_stops_when_stop_check_true(monkeypatch):
    monkeypatch.setenv("BULK_QUEUE_TYPE", "redis")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(bca, "collection_has_bulk_activity", lambda _cid: True)
        stop = {"v": False}

        def stop_check() -> bool:
            return stop["v"]

        def set_stop() -> None:
            stop["v"] = True

        monkeypatch.setattr(bca.time, "sleep", lambda _s: set_stop())

        ok = bca.wait_until_collection_bulk_idle("coll-c", stop_check=stop_check, poll_seconds=0.5)

        assert ok is False
    finally:
        get_settings.cache_clear()
