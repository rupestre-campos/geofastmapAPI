"""Queue for PMTiles build jobs (Redis list). Job status is stored in job_store (GET /jobs/{job_id})."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import get_settings

if TYPE_CHECKING:
    from app.services.job_store import JobInfo

TILE_BUILD_QUEUE_KEY = "geofast:tile_build_queue"
TILE_BUILD_LATEST_PREFIX = "geofast:tile_build_latest:"
TILE_BUILD_PENDING_PREFIX = "geofast:tile_build_pending:"
TILE_BUILD_JOB_TTL = 86400 * 7  # 7 days


@dataclass
class TileBuildPayload:
    collection_id: str
    job_id: str

    def to_json(self) -> str:
        return json.dumps({"collection_id": self.collection_id, "job_id": self.job_id})

    @classmethod
    def from_json(cls, s: str) -> "TileBuildPayload":
        d = json.loads(s)
        return cls(collection_id=d["collection_id"], job_id=d["job_id"])


def _redis():
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _latest_key(collection_id: str) -> str:
    return f"{TILE_BUILD_LATEST_PREFIX}{collection_id}"


def _pending_key(collection_id: str) -> str:
    return f"{TILE_BUILD_PENDING_PREFIX}{collection_id}"


def create_tile_build_job(collection_id: str) -> "JobInfo":
    """Create a tile build job in job_store and set as latest for this collection. Caller must enqueue."""
    from app.services.job_store import create_job
    job = create_job(collection_id)
    r = _redis()
    r.set(_latest_key(collection_id), job.job_id, ex=TILE_BUILD_JOB_TTL)
    return job


def get_tile_build_job(job_id: str) -> "JobInfo | None":
    """Return job from job_store (tile build and bulk import share the same store)."""
    from app.services.job_store import get_job
    return get_job(job_id)


def get_latest_tile_build_job(collection_id: str) -> "JobInfo | None":
    r = _redis()
    job_id = r.get(_latest_key(collection_id))
    if not job_id:
        return None
    return get_tile_build_job(job_id)


def update_tile_build_job(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
) -> "JobInfo | None":
    """Update tile build job in job_store. status: building -> running, queued -> pending, completed/failed as-is."""
    from app.services.job_store import update_job
    if status == "building":
        status = "running"
    elif status == "queued":
        status = "pending"
    return update_job(job_id, status=status, message=message)


def get_pending_job_id(collection_id: str) -> str | None:
    """Return job_id if this collection has a queued or building job (dedup)."""
    r = _redis()
    return r.get(_pending_key(collection_id))


def set_pending(collection_id: str, job_id: str) -> None:
    r = _redis()
    r.set(_pending_key(collection_id), job_id, ex=3600)  # 1h fallback if worker dies


def clear_pending(collection_id: str) -> None:
    r = _redis()
    r.delete(_pending_key(collection_id))


def enqueue_tile_build(collection_id: str, job_id: str) -> bool:
    """
    Add build job to queue. Set pending so we don't enqueue duplicate for same collection.
    Returns True if enqueued, False if already pending (use existing job_id).
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    r = _redis()
    pending = _pending_key(collection_id)
    if r.get(pending):
        return False  # already queued or building
    r.set(pending, job_id, ex=3600)
    r.lpush(TILE_BUILD_QUEUE_KEY, TileBuildPayload(collection_id=collection_id, job_id=job_id).to_json())
    return True
