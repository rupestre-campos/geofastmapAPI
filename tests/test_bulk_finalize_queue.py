"""Tests for bulk finalize queue."""

from app.services import bulk_finalize_queue as bfq
from app.services.bulk_copy_ingest import FINALIZE_QUEUED


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def exists(self, key):
        return key in self.kv

    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)
            self.hashes.pop(key, None)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        if end == -1:
            end = len(items) - 1
        return items[start : end + 1]

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        removed = 0
        while value in items and (count == 0 or removed < count):
            items.remove(value)
            removed += 1
        return removed

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        return key in self.kv or key in self.hashes


def _patch_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(
        bfq,
        "get_settings",
        lambda: type("S", (), {"bulk_queue_type": "redis", "redis_url": "redis://x"})(),
    )
    monkeypatch.setattr(bfq, "run_redis_retry", lambda _label, fn, **_k: fn())
    import redis as redis_mod

    monkeypatch.setattr(redis_mod, "from_url", lambda *_a, **_k: r)
    return r


def test_enqueue_finalize_dedupes(monkeypatch):
    r = _patch_redis(monkeypatch)
    payload = bfq.BulkFinalizePayload(
        job_id="job-1",
        collection_id="coll-a",
        mode="replace",
        items_created=10,
    )
    assert bfq.enqueue_finalize(payload) is True
    assert len(r.lists.get(bfq.FINALIZE_QUEUE_KEY, [])) == 1
    assert bfq.enqueue_finalize(payload) is True
    assert len(r.lists.get(bfq.FINALIZE_QUEUE_KEY, [])) == 1


def test_finalize_queued_sentinel():
    assert FINALIZE_QUEUED == "finalize_queued"
