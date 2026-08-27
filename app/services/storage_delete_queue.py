"""Redis queue for admin storage deletes (tiles/disk first, then DB)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

STORAGE_DELETE_QUEUE_KEY = "geofastmap:storage_delete_queue"


@dataclass
class StorageDeletePayload:
    job_id: str
    action: str  # delete_collection | delete_tiles | delete_mosaic | delete_orphan
    target_id: str
    owner_id: int | None = None
    orphan_kind: str | None = None
    mosaic_json_path: str | None = None

    def to_json(self) -> str:
        out: dict[str, object] = {
            "job_id": self.job_id,
            "action": self.action,
            "target_id": self.target_id,
        }
        if self.owner_id is not None:
            out["owner_id"] = self.owner_id
        if self.orphan_kind:
            out["orphan_kind"] = self.orphan_kind
        if self.mosaic_json_path:
            out["mosaic_json_path"] = self.mosaic_json_path
        return json.dumps(out)

    @classmethod
    def from_json(cls, s: str) -> StorageDeletePayload:
        d = json.loads(s)
        return cls(
            job_id=str(d["job_id"]),
            action=str(d["action"]),
            target_id=str(d["target_id"]),
            owner_id=d.get("owner_id"),
            orphan_kind=d.get("orphan_kind"),
            mosaic_json_path=d.get("mosaic_json_path"),
        )


def enqueue_storage_delete(payload: StorageDeletePayload) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        raise RuntimeError("Storage delete queue requires BULK_QUEUE_TYPE=redis")
    import redis

    def _push() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.lpush(STORAGE_DELETE_QUEUE_KEY, payload.to_json())

    run_redis_retry("storage_delete_enqueue", _push)
