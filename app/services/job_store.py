"""Job status for bulk import. In-memory or Redis (when queue is Redis)."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import get_settings


@dataclass
class JobInfo:
    job_id: str
    collection_id: str
    status: str  # pending, running, replacing, completed, failed, cancelled
    message: str | None = None
    items_in: int = 0
    items_created: int = 0
    items_failed: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None  # set when status becomes completed, failed, or cancelled
    owner_id: int | None = None  # user id; None = legacy (only admin can see)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "collection_id": self.collection_id,
            "status": self.status,
            "message": self.message,
            "items_in": self.items_in,
            "items_created": self.items_created,
            "items_failed": self.items_failed,
            "created_at": self.created_at.isoformat() + "Z",
            "updated_at": self.updated_at.isoformat() + "Z",
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }
        if self.owner_id is not None:
            out["owner_id"] = self.owner_id
        return out


# ----- In-memory backend -----
_mem_lock = threading.Lock()
_mem_jobs: dict[str, JobInfo] = {}


def _create_job_memory(collection_id: str, owner_id: int | None = None) -> JobInfo:
    job_id = str(uuid.uuid4())
    job = JobInfo(job_id=job_id, collection_id=collection_id, status="pending", owner_id=owner_id)
    with _mem_lock:
        _mem_jobs[job_id] = job
    return job


def _get_job_memory(job_id: str) -> JobInfo | None:
    with _mem_lock:
        return _mem_jobs.get(job_id)


def _update_job_memory(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    items_in: int | None = None,
    items_created: int | None = None,
    items_failed: int | None = None,
    finished_at: datetime | None = None,
) -> JobInfo | None:
    with _mem_lock:
        job = _mem_jobs.get(job_id)
        if not job:
            return None
        now = datetime.utcnow()
        if status is not None:
            job.status = status
            if status in ("completed", "failed", "cancelled") and job.finished_at is None:
                job.finished_at = now
        if message is not None:
            job.message = message
        if items_in is not None:
            job.items_in = items_in
        if items_created is not None:
            job.items_created = items_created
        if items_failed is not None:
            job.items_failed = items_failed
        if finished_at is not None:
            job.finished_at = finished_at
        job.updated_at = now
        return job


# ----- Redis backend -----
def _redis_key(job_id: str) -> str:
    return f"geofast:job:{job_id}"


def _jobs_by_collection_key(collection_id: str) -> str:
    return f"geofast:jobs_by_collection:{collection_id}"


def _create_job_redis(collection_id: str, owner_id: int | None = None) -> JobInfo:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    job_id = str(uuid.uuid4())
    job = JobInfo(job_id=job_id, collection_id=collection_id, status="pending", owner_id=owner_id)
    key = _redis_key(job_id)
    mapping: dict[str, str] = {
        "job_id": job_id,
        "collection_id": collection_id,
        "status": job.status,
        "message": job.message or "",
        "items_in": str(job.items_in),
        "items_created": str(job.items_created),
        "items_failed": str(job.items_failed),
        "created_at": job.created_at.isoformat() + "Z",
        "updated_at": job.updated_at.isoformat() + "Z",
    }
    if job.finished_at is not None:
        mapping["finished_at"] = job.finished_at.isoformat() + "Z"
    if owner_id is not None:
        mapping["owner_id"] = str(owner_id)
    r.hset(key, mapping=mapping)
    r.expire(key, 86400 * 7)  # 7 days
    coll_key = _jobs_by_collection_key(collection_id)
    r.lpush(coll_key, job_id)
    r.ltrim(coll_key, 0, 49)
    r.expire(coll_key, 86400 * 7)
    return job


def _get_job_redis(job_id: str) -> JobInfo | None:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    key = _redis_key(job_id)
    raw = r.hgetall(key)
    if not raw:
        return None
    finished_at = None
    if raw.get("finished_at"):
        finished_at = datetime.fromisoformat(raw["finished_at"].replace("Z", "+00:00"))
    owner_id = None
    if raw.get("owner_id"):
        try:
            owner_id = int(raw["owner_id"])
        except ValueError:
            pass
    return JobInfo(
        job_id=raw["job_id"],
        collection_id=raw["collection_id"],
        status=raw["status"],
        message=raw.get("message") or None,
        items_in=int(raw.get("items_in", 0)),
        items_created=int(raw.get("items_created", 0)),
        items_failed=int(raw.get("items_failed", 0)),
        created_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00")),
        finished_at=finished_at,
        owner_id=owner_id,
    )


def _update_job_redis(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    items_in: int | None = None,
    items_created: int | None = None,
    items_failed: int | None = None,
    finished_at: datetime | None = None,
) -> JobInfo | None:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    key = _redis_key(job_id)
    if not r.exists(key):
        return None
    updates = {}
    now = datetime.utcnow().isoformat() + "Z"
    if status is not None:
        updates["status"] = status
        if status in ("completed", "failed", "cancelled") and not r.hget(key, "finished_at"):
            updates["finished_at"] = now
    if message is not None:
        updates["message"] = message
    if items_in is not None:
        updates["items_in"] = str(items_in)
    if items_created is not None:
        updates["items_created"] = str(items_created)
    if items_failed is not None:
        updates["items_failed"] = str(items_failed)
    if finished_at is not None:
        updates["finished_at"] = finished_at.isoformat() + "Z"
    if updates:
        updates["updated_at"] = now
        r.hset(key, mapping=updates)
    return _get_job_redis(job_id)


# ----- Public API (config-driven) -----
def create_job(collection_id: str, owner_id: int | None = None) -> JobInfo:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        return _create_job_redis(collection_id, owner_id=owner_id)
    return _create_job_memory(collection_id, owner_id=owner_id)


def get_job(job_id: str) -> JobInfo | None:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        return _get_job_redis(job_id)
    return _get_job_memory(job_id)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    items_in: int | None = None,
    items_created: int | None = None,
    items_failed: int | None = None,
    finished_at: datetime | None = None,
) -> JobInfo | None:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        return _update_job_redis(job_id, status=status, message=message, items_in=items_in, items_created=items_created, items_failed=items_failed, finished_at=finished_at)
    return _update_job_memory(job_id, status=status, message=message, items_in=items_in, items_created=items_created, items_failed=items_failed, finished_at=finished_at)


def list_jobs_for_collection(
    collection_id: str, limit: int = 20, owner_id: int | None = None
) -> list[JobInfo]:
    """Return recent jobs for a collection. If owner_id is set, only that user's jobs; if None (admin), all."""
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        raw = _list_jobs_for_collection_redis(collection_id, limit=limit * 2 if owner_id is not None else limit)
    else:
        raw = _list_jobs_for_collection_memory(collection_id, limit=limit * 2 if owner_id is not None else limit)
    if owner_id is not None:
        raw = [j for j in raw if j.owner_id == owner_id]
    return raw[:limit]


def _list_jobs_for_collection_memory(collection_id: str, limit: int) -> list[JobInfo]:
    with _mem_lock:
        jobs = [j for j in _mem_jobs.values() if j.collection_id == collection_id]
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]


def _list_jobs_for_collection_redis(collection_id: str, limit: int) -> list[JobInfo]:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    coll_key = _jobs_by_collection_key(collection_id)
    job_ids = r.lrange(coll_key, 0, limit - 1)
    jobs: list[JobInfo] = []
    for jid in job_ids:
        job = _get_job_redis(jid)
        if job:
            jobs.append(job)
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]


def list_all_jobs(limit: int = 100, owner_id: int | None = None) -> list[JobInfo]:
    """Return recent jobs. If owner_id is set, only that user's jobs; if None (admin), all. Legacy (owner_id None) only when admin."""
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        raw = _list_all_jobs_redis(limit=limit * 2 if owner_id is not None else limit)
    else:
        raw = _list_all_jobs_memory(limit=limit * 2 if owner_id is not None else limit)
    if owner_id is not None:
        raw = [j for j in raw if j.owner_id == owner_id]
    return raw[:limit]


def _list_all_jobs_memory(limit: int) -> list[JobInfo]:
    with _mem_lock:
        jobs = list(_mem_jobs.values())
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]


def _list_all_jobs_redis(limit: int) -> list[JobInfo]:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    prefix = "geofast:job:"
    job_ids: list[str] = []
    for key in r.scan_iter(match=prefix + "*", count=500):
        if key.startswith(prefix) and key != prefix:
            job_ids.append(key[len(prefix) :])
    jobs: list[JobInfo] = []
    for jid in job_ids:
        job = _get_job_redis(jid)
        if job:
            jobs.append(job)
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]
