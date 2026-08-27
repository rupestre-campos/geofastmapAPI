"""Job status for bulk import. In-memory or Redis (when queue is Redis)."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

_TERMINAL_STATUSES_FOR_FINISHED = frozenset(
    {"completed", "failed", "cancelled", "error", "success", "done", "succeeded"}
)


def _release_bulk_mutex_on_terminal(job_id: str, collection_id: str | None, status: str | None) -> None:
    if not collection_id or not status:
        return
    if status.lower() not in _TERMINAL_STATUSES_FOR_FINISHED:
        return
    try:
        from app.services.bulk_collection_activity import release_bulk_mutex_for_job

        release_bulk_mutex_for_job(collection_id, job_id)
    except Exception:
        pass


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
    last_progress_at: datetime | None = None  # updated on each progress/status write during import
    owner_id: int | None = None  # user id; None = legacy (only admin can see)
    job_label: str | None = None  # e.g. raster_batch — for UI classification (optional)

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
            "last_progress_at": self.last_progress_at.isoformat() + "Z" if self.last_progress_at else None,
        }
        if self.owner_id is not None:
            out["owner_id"] = self.owner_id
        if self.job_label:
            out["job_label"] = self.job_label
        return out


# ----- In-memory backend -----
_mem_lock = threading.Lock()
_mem_jobs: dict[str, JobInfo] = {}


def _create_job_memory(
    collection_id: str, owner_id: int | None = None, job_label: str | None = None
) -> JobInfo:
    job_id = str(uuid.uuid4())
    job = JobInfo(
        job_id=job_id,
        collection_id=collection_id,
        status="pending",
        owner_id=owner_id,
        job_label=job_label,
    )
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
            if status.lower() in _TERMINAL_STATUSES_FOR_FINISHED and job.finished_at is None:
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
        if status is not None or items_created is not None or message is not None:
            job.last_progress_at = now
        if status is not None:
            _release_bulk_mutex_on_terminal(job_id, job.collection_id, status)
        return job


# ----- Redis backend -----
def _redis_key(job_id: str) -> str:
    return f"geofastmap:job:{job_id}"


def _jobs_by_collection_key(collection_id: str) -> str:
    return f"geofastmap:jobs_by_collection:{collection_id}"


def _create_job_redis(
    collection_id: str, owner_id: int | None = None, job_label: str | None = None
) -> JobInfo:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    job_id = str(uuid.uuid4())
    job = JobInfo(
        job_id=job_id,
        collection_id=collection_id,
        status="pending",
        owner_id=owner_id,
        job_label=job_label,
    )
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
    if job.last_progress_at is not None:
        mapping["last_progress_at"] = job.last_progress_at.isoformat() + "Z"
    if owner_id is not None:
        mapping["owner_id"] = str(owner_id)
    if job_label:
        mapping["job_label"] = job_label
    def _write() -> None:
        r.hset(key, mapping=mapping)
        r.expire(key, 86400 * 7)  # 7 days
        coll_key = _jobs_by_collection_key(collection_id)
        r.lpush(coll_key, job_id)
        r.ltrim(coll_key, 0, 49)
        r.expire(coll_key, 86400 * 7)

    run_redis_retry("create_job", _write)
    return job


def _get_job_redis(job_id: str) -> JobInfo | None:
    import redis
    settings = get_settings()
    key = _redis_key(job_id)
    read_attempts = max(1, int(getattr(settings, "redis_retry_read_max_attempts", 15) or 15))

    def _read() -> dict:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return r.hgetall(key)

    raw = run_redis_retry("get_job", _read, max_attempts=read_attempts)
    if not raw:
        return None
    finished_at = None
    if raw.get("finished_at"):
        finished_at = datetime.fromisoformat(raw["finished_at"].replace("Z", "+00:00"))
    last_progress_at = None
    if raw.get("last_progress_at"):
        last_progress_at = datetime.fromisoformat(raw["last_progress_at"].replace("Z", "+00:00"))
    owner_id = None
    if raw.get("owner_id"):
        try:
            owner_id = int(raw["owner_id"])
        except ValueError:
            pass
    jl = raw.get("job_label") or None
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
        last_progress_at=last_progress_at,
        owner_id=owner_id,
        job_label=str(jl) if jl else None,
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
    if not run_redis_retry("update_job_exists", lambda: r.exists(key)):
        return None
    collection_id = run_redis_retry("update_job_collection", lambda: r.hget(key, "collection_id"))
    updates = {}
    now = datetime.utcnow().isoformat() + "Z"
    if status is not None:
        updates["status"] = status
        if status.lower() in _TERMINAL_STATUSES_FOR_FINISHED and not r.hget(key, "finished_at"):
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
        updates["last_progress_at"] = now
        run_redis_retry("update_job", lambda: r.hset(key, mapping=updates))
    if status is not None:
        _release_bulk_mutex_on_terminal(job_id, collection_id, status)
    return _get_job_redis(job_id)


# ----- Public API (config-driven) -----
def create_job(
    collection_id: str, owner_id: int | None = None, *, job_label: str | None = None
) -> JobInfo:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        return _create_job_redis(collection_id, owner_id=owner_id, job_label=job_label)
    return _create_job_memory(collection_id, owner_id=owner_id, job_label=job_label)


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


def list_all_jobs(
    limit: int = 100,
    owner_id: int | None = None,
    *,
    offset: int = 0,
) -> list[JobInfo]:
    """
    Return recent jobs (newest updated_at first).
    If owner_id is set, only that user's jobs; if None (admin), all.
    Legacy jobs (owner_id None) only when admin (owner_id is None).
    """
    settings = get_settings()
    # Fetch enough rows to cover offset+limit (and owner filter oversampling).
    fetch = max(limit + offset, 1)
    if owner_id is not None:
        fetch = min(max(fetch * 2, fetch + 50), 10_000)
    else:
        fetch = min(fetch, 10_000)
    if settings.bulk_queue_type == "redis":
        raw = _list_all_jobs_redis(limit=fetch)
    else:
        raw = _list_all_jobs_memory(limit=fetch)
    if owner_id is not None:
        raw = [j for j in raw if j.owner_id == owner_id]
    return raw[offset : offset + limit]


def list_all_jobs_unpaginated(owner_id: int | None = None, *, max_jobs: int = 5000) -> list[JobInfo]:
    """
    Load up to max_jobs (newest first) for filter-then-paginate UIs.
    Prefer this when applying status/collection/time filters client- or route-side.
    """
    settings = get_settings()
    cap = max(1, min(int(max_jobs), 10_000))
    if settings.bulk_queue_type == "redis":
        raw = _list_all_jobs_redis(limit=cap)
    else:
        raw = _list_all_jobs_memory(limit=cap)
    if owner_id is not None:
        raw = [j for j in raw if j.owner_id == owner_id]
    return raw[:cap]


def _list_all_jobs_memory(limit: int) -> list[JobInfo]:
    with _mem_lock:
        jobs = list(_mem_jobs.values())
    jobs.sort(key=lambda j: j.updated_at, reverse=True)
    return jobs[:limit]


def _list_all_jobs_redis(limit: int) -> list[JobInfo]:
    import redis
    settings = get_settings()
    r = redis.from_url(settings.redis_url, decode_responses=True)
    prefix = "geofastmap:job:"
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
