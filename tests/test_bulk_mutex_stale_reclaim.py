"""Stale bulk mutex reclaim when holder job already failed."""

from dataclasses import dataclass
from datetime import datetime

from app.services import bulk_collection_activity as bca
from app.services import bulk_worker as bw
from app.services.bulk_queue import BulkJobPayload


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)
        return 1

    def expire(self, key, ttl):
        return key in self.kv

    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.kv.keys()):
            if key.startswith(prefix):
                yield key


@dataclass
class _Job:
    job_id: str
    collection_id: str
    status: str
    finished_at: datetime | None = None


def _patch_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(
        bca,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "bulk_queue_type": "redis",
                "redis_url": "redis://x",
                "redis_retry_read_max_attempts": 3,
                "bulk_collection_mutex_ttl_seconds": 600,
            },
        )(),
    )
    monkeypatch.setattr(bca, "run_redis_retry", lambda _label, fn, **_k: fn())
    import redis as redis_mod

    monkeypatch.setattr(redis_mod, "from_url", lambda *_a, **_k: r)
    return r


def test_reclaim_stale_mutex_when_holder_failed(monkeypatch):
    r = _patch_redis(monkeypatch)
    r.kv[f"{bca.BULK_COLLECTION_MUTEX_PREFIX}car-area_fall-am"] = "job-old"

    failed = _Job("job-old", "car-area_fall-am", "failed", finished_at=datetime.utcnow())
    monkeypatch.setattr("app.services.job_store.get_job", lambda jid: failed if jid == "job-old" else None)

    reclaimed = bca.reclaim_stale_collection_bulk_mutex("car-area_fall-am")
    assert reclaimed == "job-old"
    assert bca.get_collection_bulk_mutex_holder("car-area_fall-am") is None


def test_defer_acquires_after_reclaiming_failed_holder(monkeypatch):
    r = _patch_redis(monkeypatch)
    r.kv[f"{bca.BULK_COLLECTION_MUTEX_PREFIX}car-area_fall-am"] = "job-old"

    failed = _Job("job-old", "car-area_fall-am", "failed", finished_at=datetime.utcnow())
    monkeypatch.setattr("app.services.job_store.get_job", lambda jid: failed if jid == "job-old" else None)

    acquire_calls = {"n": 0}

    def fake_acquire(collection_id, owner):
        acquire_calls["n"] += 1
        return acquire_calls["n"] >= 2

    enqueued = []
    monkeypatch.setattr(bw, "try_acquire_collection_bulk_mutex", fake_acquire)
    monkeypatch.setattr(bw, "enqueue", lambda p: enqueued.append(p))
    monkeypatch.setattr(bw, "update_job", lambda *_a, **_k: None)

    payload = BulkJobPayload(
        job_id="job-new",
        collection_id="car-area_fall-am",
        storage_key="f.geojsonl",
        mode="replace",
        batch_size=1000,
        job_kind="parent",
    )
    assert bw._defer_bulk_job_for_collection_mutex(payload) is False
    assert not enqueued


def test_update_job_releases_mutex_on_failed(monkeypatch):
    r = _patch_redis(monkeypatch)
    r.kv[f"{bca.BULK_COLLECTION_MUTEX_PREFIX}coll-1"] = "job-a"

    from app.services import job_store as js

    monkeypatch.setattr(
        js,
        "get_settings",
        lambda: type("S", (), {"bulk_queue_type": "memory"})(),
    )
    job = js.create_job("coll-1")
    r.kv[f"{bca.BULK_COLLECTION_MUTEX_PREFIX}coll-1"] = job.job_id

    js.update_job(job.job_id, status="failed", message="boom")
    assert bca.get_collection_bulk_mutex_holder("coll-1") is None
