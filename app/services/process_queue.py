"""Queue for OGC API - Processes jobs (intersection, erase). Uses Redis list; job status in job_store."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.config import get_settings

PROCESS_QUEUE_KEY = "geofast:process_queue"
PROCESS_JOB_IDS_KEY = "geofast:process_job_ids"
PROCESS_JOB_META_PREFIX = "geofast:process_job_meta:"


@dataclass
class ProcessJobPayload:
    job_id: str
    process_id: str  # "intersection" | "erase"
    collection_id_a: str
    collection_id_b: str

    def to_json(self) -> str:
        return json.dumps({
            "job_id": self.job_id,
            "process_id": self.process_id,
            "collection_id_a": self.collection_id_a,
            "collection_id_b": self.collection_id_b,
        })

    @classmethod
    def from_json(cls, s: str) -> "ProcessJobPayload":
        d = json.loads(s)
        return cls(
            job_id=d["job_id"],
            process_id=d["process_id"],
            collection_id_a=d["collection_id_a"],
            collection_id_b=d["collection_id_b"],
        )


def _redis():
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _meta_key(job_id: str) -> str:
    return f"{PROCESS_JOB_META_PREFIX}{job_id}"


def store_process_job_meta(
    job_id: str,
    process_id: str,
    collection_id_a: str,
    collection_id_b: str,
) -> None:
    """Store process job metadata for listing on the processing page."""
    if get_settings().process_queue_type != "redis":
        return
    try:
        r = _redis()
        key = _meta_key(job_id)
        r.hset(key, mapping={
            "job_id": job_id,
            "process_id": process_id,
            "collection_id_a": collection_id_a,
            "collection_id_b": collection_id_b,
        })
        r.expire(key, 86400 * 7)
        r.lpush(PROCESS_JOB_IDS_KEY, job_id)
        r.ltrim(PROCESS_JOB_IDS_KEY, 0, 99)
        r.expire(PROCESS_JOB_IDS_KEY, 86400 * 7)
    except Exception:
        pass


def get_process_job_meta(job_id: str) -> dict | None:
    """Return process job metadata dict or None."""
    if get_settings().process_queue_type != "redis":
        return None
    try:
        r = _redis()
        key = _meta_key(job_id)
        raw = r.hgetall(key)
        if not raw:
            return None
        return dict(raw)
    except Exception:
        return None


def set_process_job_result(job_id: str, result_collection_id: str) -> None:
    """Store result collection id when process job completes."""
    if get_settings().process_queue_type != "redis":
        return
    try:
        r = _redis()
        r.hset(_meta_key(job_id), "result_collection_id", result_collection_id)
    except Exception:
        pass


def list_process_job_ids(limit: int = 50) -> list[str]:
    """Return recent process job ids (newest first)."""
    if get_settings().process_queue_type != "redis":
        return []
    try:
        r = _redis()
        return r.lrange(PROCESS_JOB_IDS_KEY, 0, limit - 1)
    except Exception:
        return []


def enqueue_process_job(payload: ProcessJobPayload) -> bool:
    """Push job to process queue. Returns True if enqueued."""
    if get_settings().process_queue_type != "redis":
        return False
    r = _redis()
    r.lpush(PROCESS_QUEUE_KEY, payload.to_json())
    store_process_job_meta(
        payload.job_id,
        payload.process_id,
        payload.collection_id_a,
        payload.collection_id_b,
    )
    return True
