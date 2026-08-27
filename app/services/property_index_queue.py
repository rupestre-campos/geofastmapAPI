"""Queue for per-collection property index CREATE/DROP jobs (Redis list)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.services.job_store import create_job, update_job

PROPERTY_INDEX_QUEUE_KEY = "geofastmap:property_index_queue"
PROPERTY_INDEX_JOB_LABEL = "property_index"


@dataclass
class PropertyIndexPayload:
    job_id: str
    collection_id: str
    old_fields: list[str] = field(default_factory=list)
    new_fields: list[str] = field(default_factory=list)
    is_composite: bool = False
    composite_members: list[dict[str, str]] | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "collection_id": self.collection_id,
                "old_fields": self.old_fields,
                "new_fields": self.new_fields,
                "is_composite": bool(self.is_composite),
                "composite_members": self.composite_members or [],
            }
        )

    @classmethod
    def from_json(cls, s: str) -> "PropertyIndexPayload":
        d = json.loads(s)
        members = d.get("composite_members") or []
        if not isinstance(members, list):
            members = []
        return cls(
            job_id=str(d["job_id"]),
            collection_id=str(d["collection_id"]),
            old_fields=[str(x) for x in (d.get("old_fields") or [])],
            new_fields=[str(x) for x in (d.get("new_fields") or [])],
            is_composite=bool(d.get("is_composite")),
            composite_members=members if d.get("is_composite") else None,
        )


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def property_index_queue_enabled() -> bool:
    """Only enqueue when process_worker will consume (PROCESS_QUEUE_TYPE=redis)."""
    return (get_settings().process_queue_type or "").strip().lower() == "redis"


def property_index_queue_depth() -> int | None:
    """LLEN of the property-index queue, or None if Redis unavailable / queue disabled."""
    if not property_index_queue_enabled():
        return None
    try:
        return int(_redis().llen(PROPERTY_INDEX_QUEUE_KEY) or 0)
    except Exception:
        return None


def enqueue_property_index_job(payload: PropertyIndexPayload) -> bool:
    if not property_index_queue_enabled():
        return False
    _redis().lpush(PROPERTY_INDEX_QUEUE_KEY, payload.to_json())
    return True


def schedule_property_index_job(
    collection_id: str,
    old_fields: list[str],
    new_fields: list[str],
    *,
    is_composite: bool = False,
    composite_members: Any = None,
    owner_id: int | None = None,
) -> "Any":
    """
    Create a visible job and enqueue index sync (or run inline when Redis queue is off).
    Returns JobInfo.
    """
    from app.services.composite_collections import member_collection_ids, parse_composite_members
    from app.services.property_index_worker import run_property_index_job_sync

    members_list = None
    if is_composite:
        members_list = [
            {"collection_id": m}
            for m in member_collection_ids(parse_composite_members(composite_members))
        ]

    job = create_job(collection_id, owner_id=owner_id, job_label=PROPERTY_INDEX_JOB_LABEL)
    update_job(
        job.job_id,
        message="Queued property index sync",
        items_in=max(len(new_fields or []), len(old_fields or [])),
    )
    payload = PropertyIndexPayload(
        job_id=job.job_id,
        collection_id=collection_id,
        old_fields=list(old_fields or []),
        new_fields=list(new_fields or []),
        is_composite=is_composite,
        composite_members=members_list,
    )
    if enqueue_property_index_job(payload):
        depth = property_index_queue_depth()
        depth_s = str(depth) if depth is not None else "?"
        print(
            f"[property-index] queued collection={collection_id} job_id={job.job_id} "
            f"queue={PROPERTY_INDEX_QUEUE_KEY} depth={depth_s}",
            flush=True,
        )
        return job
    print(
        f"[property-index] PROCESS_QUEUE_TYPE is not redis — running inline for "
        f"collection={collection_id} job_id={job.job_id}",
        file=sys.stderr,
        flush=True,
    )
    # Dev / memory mode: run synchronously so indexes still apply.
    run_property_index_job_sync(payload)
    return job
