"""Redis-backed resumable upload sessions for chunked bulk uploads."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

_UPLOAD_SESSION_PREFIX = "geofastmap:bulk_upload_session:"


def _redis():
    import redis

    settings = get_settings()
    return redis.from_url(settings.redis_url, decode_responses=True)


def _key(upload_id: str) -> str:
    return f"{_UPLOAD_SESSION_PREFIX}{upload_id}"


def create_upload_session(
    *,
    collection_id: str,
    owner_id: int | None,
    filename: str,
    mode: str,
    batch_size: int,
    queue_compute_tiles: bool,
) -> dict[str, Any]:
    settings = get_settings()
    upload_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    payload = {
        "upload_id": upload_id,
        "collection_id": collection_id,
        "owner_id": owner_id,
        "filename": filename,
        "mode": mode,
        "batch_size": int(batch_size),
        "queue_compute_tiles": bool(queue_compute_tiles),
        "created_at": now,
        "updated_at": now,
        "status": "pending_parts",
        "parts": [],
    }

    def _write() -> None:
        r = _redis()
        r.set(_key(upload_id), json.dumps(payload, separators=(",", ":")), ex=settings.bulk_upload_session_ttl_seconds)

    run_redis_retry("bulk_upload_session_create", _write)
    return payload


def get_upload_session(upload_id: str) -> dict[str, Any] | None:
    def _read() -> str | None:
        return _redis().get(_key(upload_id))

    raw = run_redis_retry("bulk_upload_session_get", _read)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def update_upload_session(upload_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    settings = get_settings()
    s = get_upload_session(upload_id)
    if not s:
        return None
    s.update(patch)
    s["updated_at"] = datetime.utcnow().isoformat() + "Z"

    def _write() -> None:
        _redis().set(_key(upload_id), json.dumps(s, separators=(",", ":")), ex=settings.bulk_upload_session_ttl_seconds)

    run_redis_retry("bulk_upload_session_update", _write)
    return s


def add_uploaded_part(upload_id: str, part_no: int) -> dict[str, Any] | None:
    s = get_upload_session(upload_id)
    if not s:
        return None
    parts = {int(x) for x in (s.get("parts") or [])}
    parts.add(int(part_no))
    s["parts"] = sorted(parts)
    s["status"] = "pending_parts"
    return update_upload_session(upload_id, s)


def delete_upload_session(upload_id: str) -> None:
    run_redis_retry("bulk_upload_session_delete", lambda: _redis().delete(_key(upload_id)))
