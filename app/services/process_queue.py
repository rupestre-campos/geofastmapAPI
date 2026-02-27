"""Queue for OGC API - Processes jobs (intersection, erase). Uses Redis list; job status in job_store."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.config import get_settings

PROCESS_QUEUE_KEY = "geofast:process_queue"


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


def enqueue_process_job(payload: ProcessJobPayload) -> bool:
    """Push job to process queue. Returns True if enqueued."""
    if get_settings().process_queue_type != "redis":
        return False
    r = _redis()
    r.lpush(PROCESS_QUEUE_KEY, payload.to_json())
    return True
