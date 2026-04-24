"""Queue + status store for async mosaic planner jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from app.core.config import get_settings

MOSAIC_PLAN_QUEUE_KEY = "geofastmap:mosaic_plan_queue"
MOSAIC_PLAN_JOB_KEY_PREFIX = "geofastmap:mosaic_plan_job:"
MOSAIC_PLAN_SUBTASK_QUEUE_KEY = "geofastmap:mosaic_plan_subtask_queue"
MOSAIC_PLAN_SUBTASK_KEY_PREFIX = "geofastmap:mosaic_plan_subtask:"
MOSAIC_PLAN_SUBTASK_RESULT_KEY = "geofastmap:mosaic_plan_subtask_result:"
MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY = "geofastmap:mosaic_footprint_subtask_queue"
MOSAIC_PLAN_FOOTPRINT_SUBTASK_KEY_PREFIX = "geofastmap:mosaic_footprint_subtask:"
MOSAIC_PLAN_FOOTPRINT_SUBTASK_RESULT_KEY = "geofastmap:mosaic_footprint_subtask_result:"


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _job_key(job_id: str) -> str:
    return f"{MOSAIC_PLAN_JOB_KEY_PREFIX}{job_id}"


def _subtask_key(task_id: str) -> str:
    return f"{MOSAIC_PLAN_SUBTASK_KEY_PREFIX}{task_id}"


def _subtask_result_key(job_id: str, round_idx: int) -> str:
    return f"{MOSAIC_PLAN_SUBTASK_RESULT_KEY}{job_id}:{round_idx}"


def _footprint_subtask_key(task_id: str) -> str:
    return f"{MOSAIC_PLAN_FOOTPRINT_SUBTASK_KEY_PREFIX}{task_id}"


def _footprint_subtask_result_key(job_id: str, batch_idx: int) -> str:
    return f"{MOSAIC_PLAN_FOOTPRINT_SUBTASK_RESULT_KEY}{job_id}:{batch_idx}"


def _to_iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def enqueue_mosaic_plan_job(body: dict[str, Any], owner_id: int) -> str:
    job_id = str(uuid.uuid4())
    now = _to_iso_now()
    payload = {"job_id": job_id, "owner_id": owner_id, "body": body}
    r = _redis()
    r.hset(
        _job_key(job_id),
        mapping={
            "job_id": job_id,
            "owner_id": str(owner_id),
            "status": "pending",
            "message": "Queued",
            "created_at": now,
            "updated_at": now,
            "payload": json.dumps(payload, separators=(",", ":")),
        },
    )
    r.expire(_job_key(job_id), 86400)
    r.lpush(MOSAIC_PLAN_QUEUE_KEY, json.dumps(payload, separators=(",", ":")))
    return job_id


def set_mosaic_plan_job_status(job_id: str, status: str, *, message: str | None = None) -> None:
    r = _redis()
    mapping: dict[str, str] = {
        "status": status,
        "updated_at": _to_iso_now(),
    }
    if message is not None:
        mapping["message"] = message
    if status in ("completed", "failed", "cancelled"):
        mapping["finished_at"] = _to_iso_now()
    r.hset(_job_key(job_id), mapping=mapping)


def set_mosaic_plan_job_progress(job_id: str, **fields: Any) -> None:
    """Update heartbeat/progress fields without forcing terminal status."""
    mapping: dict[str, str] = {
        "updated_at": _to_iso_now(),
    }
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            mapping[str(k)] = json.dumps(v, separators=(",", ":"))
        else:
            mapping[str(k)] = str(v)
    _redis().hset(_job_key(job_id), mapping=mapping)


def set_mosaic_plan_job_result(
    job_id: str,
    result: dict[str, Any],
    *,
    status: str = "completed",
    message: str | None = None,
) -> None:
    r = _redis()
    r.hset(
        _job_key(job_id),
        mapping={
            "status": status,
            "updated_at": _to_iso_now(),
            "finished_at": _to_iso_now(),
            "result": json.dumps(result, separators=(",", ":")),
            "message": message or ("Completed with warnings" if status == "completed_with_errors" else "Completed"),
        },
    )


def set_mosaic_plan_job_error(job_id: str, message: str) -> None:
    r = _redis()
    r.hset(
        _job_key(job_id),
        mapping={
            "status": "failed",
            "updated_at": _to_iso_now(),
            "finished_at": _to_iso_now(),
            "message": message[:2000],
        },
    )


def _task_id(job_id: str, round_idx: int, shard_key: str) -> str:
    raw = f"{job_id}:{round_idx}:{shard_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def enqueue_mosaic_plan_subtask(
    *,
    job_id: str,
    owner_id: int,
    round_idx: int,
    shard_key: str,
    payload: dict[str, Any],
    ttl_seconds: int = 3600,
) -> str:
    task_id = _task_id(job_id, round_idx, shard_key)
    r = _redis()
    tkey = _subtask_key(task_id)
    if not r.exists(tkey):
        body = {
            "task_id": task_id,
            "job_id": job_id,
            "owner_id": owner_id,
            "round_idx": int(round_idx),
            "shard_key": shard_key,
            "payload": payload,
        }
        now = _to_iso_now()
        r.hset(
            tkey,
            mapping={
                "task_id": task_id,
                "job_id": job_id,
                "owner_id": str(owner_id),
                "round_idx": str(round_idx),
                "shard_key": shard_key,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "payload": json.dumps(body, separators=(",", ":")),
            },
        )
        r.expire(tkey, ttl_seconds)
        r.lpush(MOSAIC_PLAN_SUBTASK_QUEUE_KEY, json.dumps(body, separators=(",", ":")))
    return task_id


def set_mosaic_plan_subtask_status(task_id: str, status: str, *, message: str | None = None) -> None:
    mapping = {"status": status, "updated_at": _to_iso_now()}
    if message is not None:
        mapping["message"] = message[:500]
    _redis().hset(_subtask_key(task_id), mapping=mapping)


def set_mosaic_plan_subtask_result(
    task_id: str,
    *,
    job_id: str,
    round_idx: int,
    result: dict[str, Any],
    status: str = "completed",
) -> None:
    r = _redis()
    r.hset(
        _subtask_key(task_id),
        mapping={
            "status": status,
            "updated_at": _to_iso_now(),
            "finished_at": _to_iso_now(),
            "result": json.dumps(result, separators=(",", ":")),
        },
    )
    r.rpush(
        _subtask_result_key(job_id, round_idx),
        json.dumps({"task_id": task_id, "status": status, "result": result}, separators=(",", ":")),
    )


async def await_mosaic_plan_subtask_results(
    *,
    job_id: str,
    round_idx: int,
    expected_count: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    r = _redis()
    key = _subtask_result_key(job_id, round_idx)
    out: list[dict[str, Any]] = []
    remaining = max(0, int(expected_count))
    while remaining > 0:
        item = await asyncio.to_thread(r.brpop, key, max(1, int(timeout_seconds)))
        if not item:
            break
        _k, raw = item
        try:
            out.append(json.loads(raw))
        except Exception:
            out.append({"status": "failed", "result": {"errors": [{"detail": "invalid-subtask-result"}]}})
        remaining -= 1
    return out


def _footprint_task_id(job_id: str, batch_idx: int, path: list[Any]) -> str:
    raw = json.dumps({"job_id": job_id, "batch_idx": batch_idx, "path": path}, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()[:32]


def clear_mosaic_footprint_subtask_results(job_id: str, batch_idx: int) -> None:
    _redis().delete(_footprint_subtask_result_key(job_id, batch_idx))


def enqueue_mosaic_footprint_display_subtask(
    *,
    job_id: str,
    owner_id: int,
    batch_idx: int,
    path: list[Any],
    url: str,
    bbox4: list[float],
    ttl_seconds: int = 3600,
) -> str:
    task_id = _footprint_task_id(job_id, batch_idx, path)
    r = _redis()
    tkey = _footprint_subtask_key(task_id)
    if not r.exists(tkey):
        body = {
            "task_id": task_id,
            "job_id": job_id,
            "owner_id": owner_id,
            "batch_idx": int(batch_idx),
            "path": path,
            "url": str(url),
            "bbox4": [float(x) for x in bbox4[:4]],
        }
        now = _to_iso_now()
        r.hset(
            tkey,
            mapping={
                "task_id": task_id,
                "job_id": job_id,
                "owner_id": str(owner_id),
                "batch_idx": str(batch_idx),
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "payload": json.dumps(body, separators=(",", ":")),
            },
        )
        r.expire(tkey, ttl_seconds)
        r.lpush(MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY, json.dumps(body, separators=(",", ":")))
    return task_id


def set_mosaic_footprint_subtask_status(task_id: str, status: str, *, message: str | None = None) -> None:
    mapping = {"status": status, "updated_at": _to_iso_now()}
    if message is not None:
        mapping["message"] = message[:500]
    _redis().hset(_footprint_subtask_key(task_id), mapping=mapping)


def set_mosaic_footprint_subtask_result(
    task_id: str,
    *,
    job_id: str,
    batch_idx: int,
    path: list[Any],
    footprint_display: dict[str, Any] | None,
    status: str = "completed",
) -> None:
    r = _redis()
    r.hset(
        _footprint_subtask_key(task_id),
        mapping={
            "status": status,
            "updated_at": _to_iso_now(),
            "finished_at": _to_iso_now(),
            "result": json.dumps(
                {"path": path, "footprint_display": footprint_display},
                separators=(",", ":"),
            ),
        },
    )
    r.rpush(
        _footprint_subtask_result_key(job_id, batch_idx),
        json.dumps(
            {
                "task_id": task_id,
                "status": status,
                "path": path,
                "footprint_display": footprint_display,
            },
            separators=(",", ":"),
        ),
    )


async def await_mosaic_footprint_subtask_results(
    *,
    job_id: str,
    batch_idx: int,
    expected_count: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    r = _redis()
    key = _footprint_subtask_result_key(job_id, batch_idx)
    out: list[dict[str, Any]] = []
    remaining = max(0, int(expected_count))
    while remaining > 0:
        item = await asyncio.to_thread(r.brpop, key, max(1, int(timeout_seconds)))
        if not item:
            break
        _k, raw = item
        try:
            out.append(json.loads(raw))
        except Exception:
            out.append({"status": "failed", "path": [], "footprint_display": None})
        remaining -= 1
    return out


def get_mosaic_plan_job(job_id: str) -> dict[str, Any] | None:
    raw = _redis().hgetall(_job_key(job_id))
    if not raw:
        return None
    out: dict[str, Any] = {
        "job_id": raw.get("job_id") or job_id,
        "owner_id": int(raw.get("owner_id") or 0),
        "status": raw.get("status") or "unknown",
        "message": raw.get("message") or "",
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "finished_at": raw.get("finished_at"),
    }
    if raw.get("result"):
        try:
            out["result"] = json.loads(raw["result"])
        except Exception:
            pass
    if raw.get("payload"):
        try:
            out["payload"] = json.loads(raw["payload"])
        except Exception:
            pass
    for k in (
        "phase",
        "round",
        "rounds_max",
        "features_seen",
        "retry_after_seconds",
        "children_total",
        "children_done",
        "children_failed",
    ):
        v = raw.get(k)
        if v is None:
            continue
        if k in (
            "round",
            "rounds_max",
            "features_seen",
            "retry_after_seconds",
            "children_total",
            "children_done",
            "children_failed",
        ):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out

