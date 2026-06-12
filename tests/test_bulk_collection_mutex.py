"""Tests for per-collection bulk import mutex."""

from app.services import bulk_collection_activity as bca
from app.services.bulk_queue import BulkJobPayload
from app.services import bulk_worker as bw


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)
        self.ttl.pop(key, None)
        return 1

    def expire(self, key, ttl):
        if key in self.kv:
            self.ttl[key] = ttl
            return True
        return False


def test_mutex_acquire_and_release(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(bca, "get_settings", lambda: type("S", (), {"bulk_queue_type": "redis", "redis_url": "redis://x", "redis_retry_read_max_attempts": 3, "bulk_collection_mutex_ttl_seconds": 600})())
    monkeypatch.setattr(bca, "run_redis_retry", lambda _label, fn, **_k: fn())
    import redis as redis_mod
    monkeypatch.setattr(redis_mod, "from_url", lambda *_a, **_k: r)

    assert bca.try_acquire_collection_bulk_mutex("coll-1", "job-a") is True
    assert bca.try_acquire_collection_bulk_mutex("coll-1", "job-b") is False
    assert bca.holds_collection_bulk_mutex("coll-1", "job-a") is True
    bca.release_collection_bulk_mutex("coll-1", "job-a")
    assert bca.try_acquire_collection_bulk_mutex("coll-1", "job-b") is True


def test_defer_requeues_when_mutex_held(monkeypatch):
    enqueued = []
    updates = []

    monkeypatch.setattr(bw, "get_collection_bulk_mutex_holder", lambda _c: "other-job")
    monkeypatch.setattr(bw, "holds_collection_bulk_mutex", lambda *_a, **_k: False)
    monkeypatch.setattr(bw, "try_acquire_collection_bulk_mutex", lambda *_a, **_k: False)
    monkeypatch.setattr(bw, "enqueue", lambda p: enqueued.append(p))
    monkeypatch.setattr(bw, "update_job", lambda jid, **kw: updates.append((jid, kw)))

    payload = BulkJobPayload(
        job_id="job-new",
        collection_id="coll-x",
        storage_key="f.geojsonl",
        mode="append",
        batch_size=1000,
        job_kind="parent",
    )
    assert bw._defer_bulk_job_for_collection_mutex(payload) is True
    assert len(enqueued) == 1
    assert updates[0][1]["message"].startswith("Waiting for another bulk import")
