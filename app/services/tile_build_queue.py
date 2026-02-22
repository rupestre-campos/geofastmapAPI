"""Queue for PMTiles build jobs (Redis list) and job status tracking."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import get_settings

TILE_BUILD_QUEUE_KEY = "geofast:tile_build_queue"
TILE_BUILD_JOB_PREFIX = "geofast:tile_build_job:"
TILE_BUILD_LATEST_PREFIX = "geofast:tile_build_latest:"
TILE_BUILD_PENDING_PREFIX = "geofast:tile_build_pending:"
TILE_BUILD_JOB_TTL = 86400 * 7  # 7 days


@dataclass
class TileBuildJobInfo:
    job_id: str
    collection_id: str
    status: str  # queued, building, completed, failed
    message: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "collection_id": self.collection_id,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
        }


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


def _job_key(job_id: str) -> str:
    return f"{TILE_BUILD_JOB_PREFIX}{job_id}"


def _latest_key(collection_id: str) -> str:
    return f"{TILE_BUILD_LATEST_PREFIX}{collection_id}"


def _pending_key(collection_id: str) -> str:
    return f"{TILE_BUILD_PENDING_PREFIX}{collection_id}"


def create_tile_build_job(collection_id: str) -> TileBuildJobInfo:
    """Create a tile build job (Redis only). Returns job info; caller must enqueue."""
    r = _redis()
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    key = _job_key(job_id)
    r.hset(key, mapping={
        "job_id": job_id,
        "collection_id": collection_id,
        "status": "queued",
        "message": "",
        "created_at": now,
        "updated_at": now,
    })
    r.expire(key, TILE_BUILD_JOB_TTL)
    r.set(_latest_key(collection_id), job_id, ex=TILE_BUILD_JOB_TTL)
    return TileBuildJobInfo(
        job_id=job_id,
        collection_id=collection_id,
        status="queued",
        message=None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


def get_tile_build_job(job_id: str) -> TileBuildJobInfo | None:
    r = _redis()
    raw = r.hgetall(_job_key(job_id))
    if not raw:
        return None
    return _job_from_raw(raw)


def get_latest_tile_build_job(collection_id: str) -> TileBuildJobInfo | None:
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
) -> TileBuildJobInfo | None:
    r = _redis()
    key = _job_key(job_id)
    if not r.exists(key):
        return None
    updates = {}
    if status is not None:
        updates["status"] = status
    if message is not None:
        updates["message"] = message or ""
    if updates:
        updates["updated_at"] = datetime.utcnow().isoformat() + "Z"
        r.hset(key, mapping=updates)
    raw = r.hgetall(key)
    return _job_from_raw(raw) if raw else None


def _job_from_raw(raw: dict) -> TileBuildJobInfo:
    return TileBuildJobInfo(
        job_id=raw["job_id"],
        collection_id=raw["collection_id"],
        status=raw["status"],
        message=raw.get("message") or None,
        created_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00")),
    )


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
