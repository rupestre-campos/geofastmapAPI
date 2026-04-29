from app.services import bulk_upload_sessions as bus


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, k, v, ex=None):
        self.store[k] = v
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)
        return 1


def test_upload_session_create_update_delete(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(bus, "_redis", lambda: r)
    monkeypatch.setattr(bus, "run_redis_retry", lambda _label, fn, **_k: fn())
    monkeypatch.setattr(bus, "get_settings", lambda: type("S", (), {"bulk_upload_session_ttl_seconds": 60})())

    s = bus.create_upload_session(
        collection_id="c1",
        owner_id=7,
        filename="a.geojsonl",
        mode="append",
        batch_size=1000,
        queue_compute_tiles=True,
    )
    sid = s["upload_id"]
    got = bus.get_upload_session(sid)
    assert got is not None
    assert got["collection_id"] == "c1"

    bus.add_uploaded_part(sid, 1)
    bus.add_uploaded_part(sid, 2)
    got2 = bus.get_upload_session(sid)
    assert got2["parts"] == [1, 2]

    bus.delete_upload_session(sid)
    assert bus.get_upload_session(sid) is None
