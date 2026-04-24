"""Queue + status store for async mosaic planner jobs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.core.config import get_settings

MOSAIC_PLAN_QUEUE_KEY = "geofastmap:mosaic_plan_queue"
MOSAIC_PLAN_JOB_KEY_PREFIX = "geofastmap:mosaic_plan_job:"


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _job_key(job_id: str) -> str:
    return f"{MOSAIC_PLAN_JOB_KEY_PREFIX}{job_id}"


def enqueue_mosaic_plan_job(body: dict[str, Any], owner_id: int) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
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
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    if message is not None:
        mapping["message"] = message
    if status in ("completed", "failed", "cancelled"):
        mapping["finished_at"] = datetime.utcnow().isoformat() + "Z"
    r.hset(_job_key(job_id), mapping=mapping)


def set_mosaic_plan_job_result(job_id: str, result: dict[str, Any]) -> None:
    r = _redis()
    r.hset(
        _job_key(job_id),
        mapping={
            "status": "completed",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "result": json.dumps(result, separators=(",", ":")),
            "message": "Completed",
        },
    )


def set_mosaic_plan_job_error(job_id: str, message: str) -> None:
    r = _redis()
    r.hset(
        _job_key(job_id),
        mapping={
            "status": "failed",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "message": message[:2000],
        },
    )


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
    return out

