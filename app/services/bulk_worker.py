"""Process a single bulk import job (used by in-process consumer or standalone worker)."""

from __future__ import annotations

import os

from app.core.config import get_settings
from app.services.bulk_import import run_bulk_import_sync
from app.services.bulk_queue import QUEUE_KEY, BulkJobPayload
from app.services.bulk_storage import get_bulk_storage
from app.services.job_store import update_job
from app.services.tile_build_queue import (
    create_tile_build_job,
    enqueue_tile_build,
    update_tile_build_job,
)


def cleanup_orphan_bulk_uploads() -> None:
    """At startup, delete any file in bulk storage that does not have a job pending on the queue."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    try:
        import redis
        r = redis.from_url(settings.redis_url, decode_responses=True)
        payloads = r.lrange(QUEUE_KEY, 0, -1) or []
    except Exception:
        return
    pending_storage_keys: set[str] = set()
    for s in payloads:
        try:
            payload = BulkJobPayload.from_json(s)
            pending_storage_keys.add(payload.storage_key)
        except Exception:
            continue
    base = (settings.bulk_storage_path or "").rstrip("/")
    if not base or not os.path.isdir(base):
        return
    storage = get_bulk_storage()
    for name in os.listdir(base):
        if name.startswith(".") or ".." in name:
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        if name not in pending_storage_keys:
            try:
                storage.delete(name)
            except Exception:
                pass


def process_bulk_job(payload: BulkJobPayload) -> None:
    """Load file from storage, run import, update job status, delete file. Optionally queue tile build."""
    storage = get_bulk_storage()
    path = storage.get_path_or_uri(payload.storage_key)

    def on_progress(status: str, items_created: int, _total: int | None) -> None:
        update_job(payload.job_id, status=status, items_created=items_created)

    try:
        update_job(payload.job_id, status="running")
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            payload.mode,
            payload.batch_size,
            on_progress=on_progress,
            zip_inner_shp_paths=payload.zip_inner_shp_paths,
        )
        if err:
            update_job(
                payload.job_id,
                status="failed",
                message=err,
                items_created=created,
                items_failed=failed,
            )
        else:
            update_job(
                payload.job_id,
                status="completed",
                message=f"Imported {created} features." + (f" {failed} failed." if failed else ""),
                items_created=created,
                items_failed=failed,
            )
            if payload.queue_compute_tiles and get_settings().bulk_queue_type == "redis":
                try:
                    tile_job = create_tile_build_job(payload.collection_id, owner_id=payload.owner_id)
                    update_tile_build_job(tile_job.job_id, message="Tile build")
                    enqueue_tile_build(payload.collection_id, tile_job.job_id)
                except Exception:
                    pass
    except Exception as e:
        update_job(payload.job_id, status="failed", message=str(e))
    finally:
        # Always remove uploaded file after processing (success or failure)
        try:
            storage.delete(payload.storage_key)
        except Exception:
            pass
