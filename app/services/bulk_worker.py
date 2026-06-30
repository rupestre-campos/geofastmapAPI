"""Process a single bulk import job (used by in-process consumer or standalone worker)."""

from __future__ import annotations

import os
import sys
import time
from typing import Callable

from app.core.config import get_settings
from app.services.bulk_copy_ingest import run_bulk_copy_import_sync
from app.services.bulk_import import BulkImportCancelled, run_bulk_import_sync
from app.services.bulk_collection_activity import (
    get_collection_bulk_mutex_holder,
    is_terminal_job_status,
    reclaim_all_stale_bulk_mutexes,
    reclaim_stale_collection_bulk_mutex,
    refresh_collection_bulk_mutex,
    release_collection_bulk_mutex,
    try_acquire_collection_bulk_mutex,
)
from app.services.bulk_queue import (
    QUEUE_KEY,
    BulkJobPayload,
    enqueue,
    unregister_bulk_import_job,
)
from app.services.bulk_storage import get_bulk_storage
from app.services.bulk_watchdog import run_bulk_watchdog_pass
from app.services.job_store import get_job, update_job
from app.services.redis_resilience import run_redis_retry
from app.services.raster_batch import run_raster_batch_job
from app.services.tile_build_queue import (
    create_tile_build_job,
    enqueue_tile_build,
    update_tile_build_job,
)


def _queue_tile_build_if_requested(collection_id: str, owner_id: int | None, queue_requested: bool) -> None:
    if not queue_requested or get_settings().bulk_queue_type != "redis":
        return
    allowed_raw = (get_settings().bulk_auto_tile_build_collections or "").strip()
    allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}
    if not allowed or collection_id not in allowed:
        print(
            f"[bulk-worker] skip auto tile build for {collection_id} "
            f"(queue_compute_tiles={queue_requested}, allowlist={allowed_raw or 'disabled'})",
            flush=True,
        )
        return
    try:
        tile_job = create_tile_build_job(collection_id, owner_id=owner_id)
        update_tile_build_job(tile_job.job_id, message="Tile build")
        enqueue_tile_build(collection_id, tile_job.job_id)
    except Exception:
        pass


def cleanup_orphan_bulk_uploads() -> None:
    """At startup, delete orphan upload files, reclaim stale mutexes, drop orphan staging."""
    reclaimed = reclaim_all_stale_bulk_mutexes()
    for collection_id, holder in reclaimed:
        print(
            f"[bulk-worker] startup reclaimed mutex collection={collection_id} holder={holder}",
            flush=True,
        )
    run_bulk_watchdog_pass()
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


def _defer_bulk_job_for_collection_mutex(payload: BulkJobPayload) -> bool:
    """Re-queue when another bulk job holds the collection mutex. Returns True if deferred."""
    owner = payload.job_id
    if try_acquire_collection_bulk_mutex(payload.collection_id, owner):
        return False

    holder = get_collection_bulk_mutex_holder(payload.collection_id)
    reclaim_stale_collection_bulk_mutex(payload.collection_id)
    if try_acquire_collection_bulk_mutex(payload.collection_id, owner):
        return False

    holder = get_collection_bulk_mutex_holder(payload.collection_id)
    holder_job = get_job(holder) if holder else None
    if holder and holder_job and is_terminal_job_status(holder_job.status):
        reclaim_stale_collection_bulk_mutex(payload.collection_id)
        if try_acquire_collection_bulk_mutex(payload.collection_id, owner):
            return False
        holder = get_collection_bulk_mutex_holder(payload.collection_id)
        holder_job = get_job(holder) if holder else None

    holder_status = holder_job.status if holder_job else ("missing" if holder else "none")
    try:
        update_job(
            payload.job_id,
            status="pending",
            message=(
                "Waiting for another bulk import on this collection"
                + (f" (job {holder}, status={holder_status})" if holder else "")
                + "…"
            ),
        )
    except Exception:
        pass
    try:
        enqueue(payload)
    except Exception as e:
        print(
            f"[bulk-worker] defer re-enqueue failed job_id={payload.job_id}: {e}",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"[bulk-worker] deferred job_id={payload.job_id} collection={payload.collection_id} holder={holder}",
        flush=True,
    )
    return True


def _release_bulk_collection_mutex(payload: BulkJobPayload) -> None:
    release_collection_bulk_mutex(payload.collection_id, payload.job_id)


def _run_vector_import(
    path: str,
    payload: BulkJobPayload,
    on_progress: Callable[[str, int, int | None], None],
) -> tuple[int, int, str | None]:
    settings = get_settings()
    mode = payload.mode
    if mode == "replace_filtered":
        return 0, 0, "replace_filtered is not supported; use mode=replace or append."
    if mode not in ("append", "replace"):
        return 0, 0, f"Unsupported import mode: {mode}"

    if bool(getattr(settings, "bulk_copy_ingest_enabled", True)):
        return run_bulk_copy_import_sync(
            path,
            payload.collection_id,
            mode,
            payload.job_id,
            on_progress=on_progress,
            zip_inner_shp_paths=payload.zip_inner_shp_paths,
        )

    return run_bulk_import_sync(
        path,
        payload.collection_id,
        mode,
        payload.batch_size,
        on_progress=on_progress,
        zip_inner_shp_paths=payload.zip_inner_shp_paths,
        bulk_import_job_id=payload.job_id,
    )


def process_bulk_job(payload: BulkJobPayload) -> None:
    """Load file from storage, run import, update job status, delete file."""
    storage = get_bulk_storage()
    path = storage.get_path_or_uri(payload.storage_key)
    print(
        f"[bulk-worker] start job_id={payload.job_id} collection={payload.collection_id} storage_key={payload.storage_key}",
        flush=True,
    )

    if payload.job_kind == "raster_batch":
        try:
            run_raster_batch_job(
                job_id=payload.job_id,
                collection_id=payload.collection_id,
                archive_path=path,
            )
        except Exception as e:
            print(
                f"[bulk-worker] job_id={payload.job_id} raster_batch failed: {type(e).__name__}: {e}",
                flush=True,
            )
            update_job(payload.job_id, status="failed", message=str(e))
        finally:
            try:
                storage.delete(payload.storage_key)
            except Exception:
                pass
            unregister_bulk_import_job(payload.job_id)
        return

    # Legacy parent/shard payloads: run as single-file import (sharding disabled).
    if payload.job_kind in ("parent", "shard"):
        print(
            f"[bulk-worker] legacy job_kind={payload.job_kind} treated as single job_id={payload.job_id}",
            flush=True,
        )

    if _defer_bulk_job_for_collection_mutex(payload):
        return

    job = get_job(payload.job_id)
    if job and job.status == "cancelled":
        _release_bulk_collection_mutex(payload)
        try:
            storage.delete(payload.storage_key)
        except Exception:
            pass
        unregister_bulk_import_job(payload.job_id)
        return

    def on_progress(status: str, items_created: int, _total: int | None) -> None:
        job = get_job(payload.job_id)
        if job and job.status in ("failed", "cancelled"):
            raise BulkImportCancelled()
        try:
            def _write() -> None:
                refresh_collection_bulk_mutex(payload.collection_id, payload.job_id)
                update_job(payload.job_id, status=status, items_created=items_created)

            run_redis_retry(
                "bulk_import_progress",
                _write,
                max_attempts=max(
                    3,
                    int(getattr(get_settings(), "redis_retry_read_max_attempts", 15) or 15),
                ),
            )
        except Exception as e:
            if not hasattr(on_progress, "_last_err_log"):
                on_progress._last_err_log = 0.0  # type: ignore[attr-defined]
            now = time.monotonic()
            if now - on_progress._last_err_log >= 60.0:  # type: ignore[attr-defined]
                print(
                    f"[bulk-worker] progress Redis update failed job_id={payload.job_id}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                on_progress._last_err_log = now  # type: ignore[attr-defined]

    try:
        # Mark running immediately after mutex acquire so a crash cannot leave a pending mutex holder.
        run_redis_retry(
            "bulk_import_start",
            lambda: update_job(
                payload.job_id, status="running", message="Starting bulk import…"
            ),
            max_attempts=max(
                3,
                int(getattr(get_settings(), "redis_retry_read_max_attempts", 15) or 15),
            ),
        )
        if payload.mode == "replace":
            run_redis_retry(
                "bulk_import_replacing",
                lambda: update_job(
                    payload.job_id,
                    status="replacing",
                    message="Loading replacement data into staging…",
                ),
                max_attempts=max(
                    3,
                    int(getattr(get_settings(), "redis_retry_read_max_attempts", 15) or 15),
                ),
            )
        created, failed, err = _run_vector_import(path, payload, on_progress)
        terminal = get_job(payload.job_id)
        if terminal and terminal.status in ("failed", "cancelled"):
            return
        if err == "cancelled":
            update_job(
                payload.job_id,
                status="cancelled",
                message="Cancelled by user.",
                items_created=created,
                items_failed=failed,
            )
            return
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
            _queue_tile_build_if_requested(
                payload.collection_id,
                payload.owner_id,
                payload.queue_compute_tiles,
            )
    except BulkImportCancelled:
        update_job(
            payload.job_id,
            status="cancelled",
            message="Cancelled by user.",
        )
    except Exception as e:
        print(
            f"[bulk-worker] job_id={payload.job_id} failed: {type(e).__name__}: {e}",
            flush=True,
        )
        update_job(payload.job_id, status="failed", message=str(e))
    finally:
        _release_bulk_collection_mutex(payload)
        try:
            storage.delete(payload.storage_key)
        except Exception:
            pass
        unregister_bulk_import_job(payload.job_id)
