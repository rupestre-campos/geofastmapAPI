"""Dedicated queue for bulk staging → live partition promote (single consumer, self-healing)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

FINALIZE_QUEUE_KEY = "geofastmap:bulk_finalize_queue"
FINALIZE_DEDUPE_PREFIX = "geofastmap:bulk_finalize_pending:"
FINALIZE_STATE_PREFIX = "geofastmap:bulk_finalize_state:"


@dataclass
class BulkFinalizePayload:
    job_id: str
    collection_id: str
    mode: str
    items_created: int = 0
    items_failed: int = 0
    owner_id: int | None = None
    queue_compute_tiles: bool = False
    attempt: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "collection_id": self.collection_id,
                "mode": self.mode,
                "items_created": int(self.items_created),
                "items_failed": int(self.items_failed),
                "owner_id": self.owner_id,
                "queue_compute_tiles": bool(self.queue_compute_tiles),
                "attempt": int(self.attempt),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> BulkFinalizePayload:
        d = json.loads(raw)
        return cls(
            job_id=d["job_id"],
            collection_id=d["collection_id"],
            mode=str(d.get("mode") or "append"),
            items_created=int(d.get("items_created") or 0),
            items_failed=int(d.get("items_failed") or 0),
            owner_id=d.get("owner_id"),
            queue_compute_tiles=bool(d.get("queue_compute_tiles")),
            attempt=int(d.get("attempt") or 0),
        )


def _dedupe_key(job_id: str) -> str:
    return f"{FINALIZE_DEDUPE_PREFIX}{job_id}"


def _state_key(job_id: str) -> str:
    return f"{FINALIZE_STATE_PREFIX}{job_id}"


def is_finalize_pending(job_id: str) -> bool:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    import redis

    def _read() -> bool:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return bool(r.exists(_dedupe_key(job_id)))

    return bool(run_redis_retry("finalize_pending_read", _read, max_attempts=5))


def mark_finalize_pending(payload: BulkFinalizePayload) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis
    from datetime import datetime

    now = datetime.utcnow().isoformat() + "Z"
    mapping = {
        "job_id": payload.job_id,
        "collection_id": payload.collection_id,
        "mode": payload.mode,
        "items_created": str(payload.items_created),
        "items_failed": str(payload.items_failed),
        "owner_id": str(payload.owner_id) if payload.owner_id is not None else "",
        "queue_compute_tiles": "1" if payload.queue_compute_tiles else "0",
        "updated_at": now,
    }

    def _write() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.set(_dedupe_key(payload.job_id), "1", ex=86400 * 7)
        r.hset(_state_key(payload.job_id), mapping=mapping)
        r.expire(_state_key(payload.job_id), 86400 * 7)

    run_redis_retry("finalize_pending_mark", _write)


def get_finalize_state(job_id: str) -> dict[str, str]:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return {}
    import redis

    def _read() -> dict[str, str]:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        raw = r.hgetall(_state_key(job_id)) or {}
        return {k: str(v) for k, v in raw.items()}

    return run_redis_retry("finalize_state_read", _read, max_attempts=5) or {}


def mark_finalize_pending_job(job_id: str) -> None:
    """Backward-compatible dedupe mark when only job_id is known."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    run_redis_retry(
        "finalize_pending_mark",
        lambda: redis.from_url(settings.redis_url, decode_responses=True).set(
            _dedupe_key(job_id), "1", ex=86400 * 7
        ),
    )


def clear_finalize_pending(job_id: str) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        r.delete(_dedupe_key(job_id), _state_key(job_id))
    except Exception:
        pass


def record_finalize_error(job_id: str, message: str, attempt: int) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis
    from datetime import datetime

    mapping = {
        "job_id": job_id,
        "attempt": str(attempt),
        "error": str(message)[:2000],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    run_redis_retry(
        "finalize_state_write",
        lambda: redis.from_url(settings.redis_url, decode_responses=True).hset(
            _state_key(job_id), mapping=mapping
        ),
    )


def enqueue_finalize(payload: BulkFinalizePayload, *, force: bool = False) -> bool:
    """
    Queue partition promote work. Returns True if enqueued (or already pending).
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    import redis

    dedupe = _dedupe_key(payload.job_id)

    def _enqueue() -> bool:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        if not force and r.exists(dedupe):
            return True
        mark_finalize_pending(payload)
        r.lpush(FINALIZE_QUEUE_KEY, payload.to_json())
        return True

    return bool(run_redis_retry("finalize_enqueue", _enqueue))


def remove_finalize_from_queue(job_id: str) -> int:
    """Remove matching finalize payloads from the queue. Returns count removed."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return 0
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    removed = 0
    for raw in r.lrange(FINALIZE_QUEUE_KEY, 0, -1) or []:
        try:
            if BulkFinalizePayload.from_json(raw).job_id == job_id:
                removed += int(r.lrem(FINALIZE_QUEUE_KEY, 1, raw))
        except Exception:
            continue
    return removed


def finalize_queue_length() -> int:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return 0
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        return int(r.llen(FINALIZE_QUEUE_KEY) or 0)
    except Exception:
        return 0
