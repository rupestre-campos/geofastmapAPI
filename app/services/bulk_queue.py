"""Bulk import job queue. Redis for workers; in-memory + thread for single-process dev."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from app.core.config import get_settings

QUEUE_KEY = "geofast:bulk_import_queue"


@dataclass
class BulkJobPayload:
    job_id: str
    collection_id: str
    storage_key: str
    mode: str
    batch_size: int

    def to_json(self) -> str:
        return json.dumps({
            "job_id": self.job_id,
            "collection_id": self.collection_id,
            "storage_key": self.storage_key,
            "mode": self.mode,
            "batch_size": self.batch_size,
        })

    @classmethod
    def from_json(cls, s: str) -> "BulkJobPayload":
        d = json.loads(s)
        return cls(
            job_id=d["job_id"],
            collection_id=d["collection_id"],
            storage_key=d["storage_key"],
            mode=d["mode"],
            batch_size=int(d["batch_size"]),
        )


def enqueue(payload: BulkJobPayload) -> None:
    """Enqueue a bulk import job. Uses Redis or in-memory queue from config."""
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.lpush(QUEUE_KEY, payload.to_json())
        return
    _memory_queue_put(payload)


def _memory_queue_put(payload: BulkJobPayload) -> None:
    _memory_queue.put(payload)


def _memory_queue_get(timeout: float = 1.0) -> BulkJobPayload | None:
    try:
        return _memory_queue.get(timeout=timeout)
    except queue.Empty:
        return None


# In-memory queue and consumer thread (used when bulk_queue_type=memory)
_memory_queue: queue.Queue[BulkJobPayload] = queue.Queue()
_consumer_started = False
_consumer_lock = threading.Lock()


def start_memory_consumer(processor: Callable[[BulkJobPayload], None]) -> None:
    """Start the in-process consumer when queue type is memory. Call once at app startup."""
    global _consumer_started
    with _consumer_lock:
        if _consumer_started:
            return
        _consumer_started = True

    def run() -> None:
        while True:
            payload = _memory_queue_get()
            if payload is None:
                continue
            try:
                processor(payload)
            except Exception:
                pass  # processor should update job status

    t = threading.Thread(target=run, daemon=True)
    t.start()
