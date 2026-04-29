"""Bulk import job queue. Redis for workers; in-memory + thread for single-process dev."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

QUEUE_KEY = "geofastmap:bulk_import_queue"
BULK_IMPORT_REG_PREFIX = "geofastmap:bulk_import_meta:"
BULK_PARENT_STATE_PREFIX = "geofastmap:bulk_parent_state:"

_bulk_reg_lock = threading.Lock()
_mem_bulk_storage: dict[str, str] = {}


@dataclass
class BulkJobPayload:
    job_id: str
    collection_id: str
    storage_key: str
    mode: str
    batch_size: int
    owner_id: int | None = None
    queue_compute_tiles: bool = True
    zip_inner_shp_paths: list[str] | None = None
    job_kind: str = "single"  # single | parent | shard
    parent_job_id: str | None = None
    shard_index: int | None = None
    shard_total: int | None = None
    finalize_collection: bool = True

    def to_json(self) -> str:
        out = {
            "job_id": self.job_id,
            "collection_id": self.collection_id,
            "owner_id": self.owner_id,
            "storage_key": self.storage_key,
            "mode": self.mode,
            "batch_size": self.batch_size,
            "queue_compute_tiles": self.queue_compute_tiles,
        }
        if self.zip_inner_shp_paths:
            out["zip_inner_shp_paths"] = self.zip_inner_shp_paths
        if self.job_kind != "single":
            out["job_kind"] = self.job_kind
        if self.parent_job_id:
            out["parent_job_id"] = self.parent_job_id
        if self.shard_index is not None:
            out["shard_index"] = int(self.shard_index)
        if self.shard_total is not None:
            out["shard_total"] = int(self.shard_total)
        if not self.finalize_collection:
            out["finalize_collection"] = False
        return json.dumps(out)

    @classmethod
    def from_json(cls, s: str) -> "BulkJobPayload":
        d = json.loads(s)
        qt = d.get("queue_compute_tiles", True)
        if isinstance(qt, bool):
            queue_compute_tiles = qt
        else:
            queue_compute_tiles = str(qt).lower() not in ("false", "0", "no", "")
        zip_inner = d.get("zip_inner_shp_paths")
        if zip_inner is not None and not isinstance(zip_inner, list):
            zip_inner = None
        return cls(
            job_id=d["job_id"],
            collection_id=d["collection_id"],
            owner_id=d.get("owner_id"),
            storage_key=d["storage_key"],
            mode=d["mode"],
            batch_size=int(d["batch_size"]),
            queue_compute_tiles=queue_compute_tiles,
            zip_inner_shp_paths=zip_inner,
            job_kind=str(d.get("job_kind") or "single"),
            parent_job_id=d.get("parent_job_id"),
            shard_index=int(d["shard_index"]) if d.get("shard_index") is not None else None,
            shard_total=int(d["shard_total"]) if d.get("shard_total") is not None else None,
            finalize_collection=bool(d.get("finalize_collection", True)),
        )


def _parent_state_key(job_id: str) -> str:
    return f"{BULK_PARENT_STATE_PREFIX}{job_id}"


def init_parent_state(
    *,
    parent_job_id: str,
    collection_id: str,
    expected_shards: int,
    mode: str,
) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    mapping = {
        "parent_job_id": parent_job_id,
        "collection_id": collection_id,
        "mode": mode,
        "expected_shards": str(max(0, int(expected_shards))),
        "completed_shards": "0",
        "failed_shards": "0",
        "items_created": "0",
        "items_failed": "0",
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "error_samples": "[]",
    }
    run_redis_retry(
        "bulk_parent_state_init",
        lambda: redis.from_url(settings.redis_url, decode_responses=True).hset(
            _parent_state_key(parent_job_id), mapping=mapping
        ),
    )


def record_parent_shard_result(
    *,
    parent_job_id: str,
    created: int,
    failed: int,
    shard_failed: bool,
    shard_index: int | None = None,
    error_message: str | None = None,
) -> dict[str, int] | None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return None
    import redis
    from datetime import datetime

    r = redis.from_url(settings.redis_url, decode_responses=True)
    key = _parent_state_key(parent_job_id)
    if not r.exists(key):
        return None
    pipe = r.pipeline()
    pipe.hincrby(key, "completed_shards", 1)
    pipe.hincrby(key, "items_created", int(created))
    pipe.hincrby(key, "items_failed", int(failed))
    if shard_failed:
        pipe.hincrby(key, "failed_shards", 1)
    updates: dict[str, str] = {"updated_at": datetime.utcnow().isoformat() + "Z"}
    if shard_failed and error_message:
        try:
            samples = json.loads(r.hget(key, "error_samples") or "[]")
        except Exception:
            samples = []
        if not isinstance(samples, list):
            samples = []
        if len(samples) < 12:
            samples.append(
                {
                    "shard_index": int(shard_index) if shard_index is not None else None,
                    "error": str(error_message)[:500],
                }
            )
        updates["error_samples"] = json.dumps(samples, separators=(",", ":"))
    pipe.hset(key, mapping=updates)
    pipe.execute()
    raw = r.hgetall(key) or {}
    out = {
        "expected_shards": int(raw.get("expected_shards", 0) or 0),
        "completed_shards": int(raw.get("completed_shards", 0) or 0),
        "failed_shards": int(raw.get("failed_shards", 0) or 0),
        "items_created": int(raw.get("items_created", 0) or 0),
        "items_failed": int(raw.get("items_failed", 0) or 0),
        "error_samples_json": raw.get("error_samples", "[]"),
    }
    return out


def clear_parent_state(parent_job_id: str) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    run_redis_retry(
        "bulk_parent_state_clear",
        lambda: redis.from_url(settings.redis_url, decode_responses=True).delete(
            _parent_state_key(parent_job_id)
        ),
    )


def register_bulk_import_job(job_id: str, storage_key: str) -> None:
    """Track bulk jobs so cancel can remove queue entry, delete upload file, and identify job type."""
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        import redis

        run_redis_retry(
            "register_bulk_import_job",
            lambda: redis.from_url(settings.redis_url, decode_responses=True).set(
                f"{BULK_IMPORT_REG_PREFIX}{job_id}", storage_key, ex=86400 * 8
            ),
        )
        return
    with _bulk_reg_lock:
        _mem_bulk_storage[job_id] = storage_key


def unregister_bulk_import_job(job_id: str) -> None:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        import redis

        r = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            r.delete(f"{BULK_IMPORT_REG_PREFIX}{job_id}")
        except Exception:
            pass
        return
    with _bulk_reg_lock:
        _mem_bulk_storage.pop(job_id, None)


def get_bulk_import_storage_key(job_id: str) -> str | None:
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        import redis

        r = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            return r.get(f"{BULK_IMPORT_REG_PREFIX}{job_id}")
        except Exception:
            return None
    with _bulk_reg_lock:
        return _mem_bulk_storage.get(job_id)


def is_registered_bulk_import_job(job_id: str) -> bool:
    return get_bulk_import_storage_key(job_id) is not None


def remove_bulk_job_from_redis_queue(job_id: str) -> int:
    """Remove at most one queue entry matching job_id. Returns 0 or 1."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return 0
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    payloads = r.lrange(QUEUE_KEY, 0, -1) or []
    for p in payloads:
        try:
            if BulkJobPayload.from_json(p).job_id == job_id:
                return int(r.lrem(QUEUE_KEY, 1, p))
        except Exception:
            continue
    return 0


def enqueue(payload: BulkJobPayload) -> None:
    """Enqueue a bulk import job. Uses Redis or in-memory queue from config."""
    settings = get_settings()
    if settings.bulk_queue_type == "redis":
        import redis
        try:
            run_redis_retry(
                "bulk_enqueue",
                lambda: redis.from_url(settings.redis_url, decode_responses=True).lpush(
                    QUEUE_KEY, payload.to_json()
                ),
            )
        except Exception as e:
            raise RuntimeError(f"Failed to enqueue bulk job after retries: {e}") from e
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
