"""Redis get_job retries transient connection errors (bulk import cancel polls)."""

import pytest
import redis as redis_lib

from app.core.config import get_settings
from app.services.job_store import get_job


@pytest.fixture(autouse=True)
def redis_queue_fast_retry(monkeypatch):
    monkeypatch.setenv("BULK_QUEUE_TYPE", "redis")
    monkeypatch.setenv("REDIS_RETRY_BASE_SECONDS", "0.01")
    monkeypatch.setenv("REDIS_RETRY_MAX_SECONDS", "0.05")
    monkeypatch.setenv("REDIS_RETRY_READ_MAX_ATTEMPTS", "6")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_job_redis_retries_hgetall(monkeypatch):
    calls = {"n": 0}
    jid = "11111111-1111-1111-1111-111111111111"
    mapping = {
        "job_id": jid,
        "collection_id": "c1",
        "status": "running",
        "message": "",
        "items_in": "0",
        "items_created": "0",
        "items_failed": "0",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }

    class FlakyClient:
        def hgetall(self, key):
            calls["n"] += 1
            if calls["n"] < 3:
                raise redis_lib.ConnectionError("temporary")
            assert key == f"geofastmap:job:{jid}"
            return dict(mapping)

    monkeypatch.setattr(redis_lib, "from_url", lambda *a, **k: FlakyClient())

    job = get_job(jid)
    assert job is not None
    assert job.job_id == jid
    assert job.collection_id == "c1"
    assert job.status == "running"
    assert calls["n"] == 3


def test_get_job_redis_unknown_no_retry_needed(monkeypatch):
    class EmptyClient:
        def hgetall(self, key):
            return {}

    monkeypatch.setattr(redis_lib, "from_url", lambda *a, **k: EmptyClient())
    assert get_job("22222222-2222-2222-2222-222222222222") is None
