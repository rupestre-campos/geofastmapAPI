"""Process a single bulk import job (used by in-process consumer or standalone worker)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from app.core.config import get_settings
from app.db.features_partitions import ensure_features_partition_sync
from app.services.bulk_import import (
    BulkImportCancelled,
    finalize_collection_import_sync,
    list_shp_in_zip,
    replace_collection_prestage_sync,
    run_bulk_import_sync,
)
from app.services.bulk_collection_activity import (
    get_collection_bulk_mutex_holder,
    holds_collection_bulk_mutex,
    refresh_collection_bulk_mutex,
    release_collection_bulk_mutex,
    try_acquire_collection_bulk_mutex,
)
from app.services.bulk_queue import (
    QUEUE_KEY,
    BulkJobPayload,
    clear_parent_state,
    enqueue,
    get_bulk_import_storage_key,
    get_parent_shard_state,
    init_parent_state,
    record_parent_shard_result,
    unregister_bulk_import_job,
)
from app.services.bulk_storage import get_bulk_storage
from app.services.job_store import get_job, update_job
from app.services.raster_batch import run_raster_batch_job
from app.services.tile_build_queue import (
    create_tile_build_job,
    enqueue_tile_build,
    update_tile_build_job,
)


def _queue_tile_build_if_requested(collection_id: str, owner_id: int | None, queue_requested: bool) -> None:
    if not queue_requested or get_settings().bulk_queue_type != "redis":
        return
    try:
        tile_job = create_tile_build_job(collection_id, owner_id=owner_id)
        update_tile_build_job(tile_job.job_id, message="Tile build")
        enqueue_tile_build(collection_id, tile_job.job_id)
    except Exception:
        pass


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


def _bulk_mutex_owner(payload: BulkJobPayload) -> str:
    if payload.job_kind == "shard" and payload.parent_job_id:
        return payload.parent_job_id
    return payload.job_id


def _bulk_job_status_id(payload: BulkJobPayload) -> str:
    if payload.job_kind == "shard" and payload.parent_job_id:
        return payload.parent_job_id
    return payload.job_id


def _defer_bulk_job_for_collection_mutex(payload: BulkJobPayload) -> bool:
    """Re-queue when another bulk job holds the collection mutex. Returns True if deferred."""
    owner = _bulk_mutex_owner(payload)
    if payload.job_kind == "shard":
        if holds_collection_bulk_mutex(payload.collection_id, owner):
            refresh_collection_bulk_mutex(payload.collection_id, owner)
            return False
    elif try_acquire_collection_bulk_mutex(payload.collection_id, owner):
        return False

    holder = get_collection_bulk_mutex_holder(payload.collection_id)
    status_job_id = _bulk_job_status_id(payload)
    try:
        update_job(
            status_job_id,
            status="pending",
            message=(
                "Waiting for another bulk import on this collection"
                + (f" (job {holder})" if holder else "")
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
    release_collection_bulk_mutex(payload.collection_id, _bulk_mutex_owner(payload))


def process_bulk_job(payload: BulkJobPayload) -> None:
    """Load file from storage, run import, update job status, delete file. Optionally queue tile build."""
    storage = get_bulk_storage()
    path = storage.get_path_or_uri(payload.storage_key)
    print(
        f"[bulk-worker] start job_id={payload.job_id} kind={payload.job_kind} collection={payload.collection_id} storage_key={payload.storage_key}",
        flush=True,
    )

    if payload.job_kind != "raster_batch" and _defer_bulk_job_for_collection_mutex(payload):
        return

    job = get_job(payload.job_id)
    if job and job.status == "cancelled":
        if payload.job_kind != "raster_batch":
            _release_bulk_collection_mutex(payload)
        try:
            storage.delete(payload.storage_key)
        except Exception:
            pass
        unregister_bulk_import_job(payload.job_id)
        return

    def on_progress(status: str, items_created: int, _total: int | None) -> None:
        try:
            update_job(payload.job_id, status=status, items_created=items_created)
        except Exception:
            # Progress updates are best-effort; do not fail a long-running import due to transient Redis issues.
            pass

    try:
        if payload.job_kind == "parent":
            _process_parent_bulk_job(payload, path)
            return
        if payload.job_kind == "shard":
            _process_shard_bulk_job(payload, path)
            return
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
        update_job(payload.job_id, status="running")
        replace_filters = payload.parsed_replace_filters() if payload.mode == "replace_filtered" else None
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            payload.mode,
            payload.batch_size,
            on_progress=on_progress,
            zip_inner_shp_paths=payload.zip_inner_shp_paths,
            bulk_import_job_id=payload.job_id,
            replace_filters=replace_filters or None,
        )
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
            # Extent is recomputed inside run_bulk_import_sync after commits.
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
    except Exception as e:
        print(f"[bulk-worker] job_id={payload.job_id} kind={payload.job_kind} failed: {type(e).__name__}: {e}", flush=True)
        update_job(payload.job_id, status="failed", message=str(e))
    finally:
        # Cleanup strategy differs by job kind.
        if payload.job_kind == "single":
            _release_bulk_collection_mutex(payload)
            try:
                storage.delete(payload.storage_key)
            except Exception:
                pass
            unregister_bulk_import_job(payload.job_id)
        elif payload.job_kind == "parent":
            # Keep parent upload while shards run; final shard completion/finalizer handles lifecycle.
            pass
        else:
            # shard jobs are ephemeral and not registered in bulk cancel registry.
            pass


def _shard_progress_heartbeat_seconds() -> float:
    return max(0.0, float(getattr(get_settings(), "bulk_progress_heartbeat_seconds", 5.0) or 5.0))


def _parent_shard_progress_message(
    *,
    shard_index: int | None,
    shard_total: int | None,
    status: str,
    shard_created: int,
    parent_state: dict[str, int] | None,
) -> tuple[str, int]:
    prior_created = int(parent_state.get("items_created", 0) or 0) if parent_state else 0
    completed_shards = int(parent_state.get("completed_shards", 0) or 0) if parent_state else 0
    total_created = prior_created + max(0, int(shard_created))
    idx = int(shard_index or 0)
    total = int(shard_total or 0)
    if status == "replacing":
        return "Deleting existing features before import…", prior_created
    if idx and total:
        msg = f"Shard {idx}/{total}: {status}"
        if completed_shards:
            msg += f" ({completed_shards} shard(s) finished)"
        if shard_created:
            msg += f"; {shard_created} in this shard"
        msg += f"; {total_created} features total so far"
        return msg, total_created
    if shard_created:
        return f"Importing… {total_created} features so far", total_created
    return f"Importing… ({status})", total_created


def _make_parent_shard_progress_cb(
    parent_job_id: str,
    shard_index: int | None,
    shard_total: int | None,
) -> Callable[[str, int, int | None], None]:
    last_sent = 0.0
    heartbeat = _shard_progress_heartbeat_seconds()

    def on_progress(status: str, shard_created: int, _total: int | None) -> None:
        nonlocal last_sent
        now = time.monotonic()
        if heartbeat > 0 and status == "running" and (now - last_sent) < heartbeat:
            return
        last_sent = now
        parent_state = get_parent_shard_state(parent_job_id)
        msg, total_created = _parent_shard_progress_message(
            shard_index=shard_index,
            shard_total=shard_total,
            status=status,
            shard_created=shard_created,
            parent_state=parent_state,
        )
        try:
            update_job(
                parent_job_id,
                status="running",
                message=msg,
                items_created=total_created,
            )
        except Exception:
            pass

    return on_progress


def _notify_parent_shard_started(payload: BulkJobPayload) -> None:
    parent_job_id = payload.parent_job_id or ""
    if not parent_job_id:
        return
    parent_state = get_parent_shard_state(parent_job_id)
    prior_created = int(parent_state.get("items_created", 0) or 0) if parent_state else 0
    completed = int(parent_state.get("completed_shards", 0) or 0) if parent_state else 0
    idx = payload.shard_index or 0
    total = payload.shard_total or 0
    try:
        update_job(
            parent_job_id,
            status="running",
            message=(
                f"Processing shard {idx}/{total}"
                + (f" ({completed} finished, {prior_created} features imported)" if completed or prior_created else "")
                + "…"
            ),
            items_created=prior_created,
        )
    except Exception:
        pass


def _split_jsonl_to_chunk_keys(path: str, parent_job_id: str, lines_per_part: int) -> list[str]:
    storage = get_bulk_storage()
    out_keys: list[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as src:
        part_idx = 0
        line_count = 0
        dst = None
        dst_key = ""
        try:
            for line in src:
                if line_count % lines_per_part == 0:
                    if dst is not None:
                        dst.close()
                    part_idx += 1
                    dst_key = f"{parent_job_id}.shard.{part_idx:04d}.geojsonl"
                    out_keys.append(dst_key)
                    dst = open(storage.get_write_path(dst_key), "w", encoding="utf-8")
                if dst is not None:
                    dst.write(line)
                line_count += 1
        finally:
            if dst is not None:
                dst.close()
    return out_keys


def _process_parent_bulk_job(payload: BulkJobPayload, path: str) -> None:
    print(f"[bulk-parent] plan start parent_job_id={payload.job_id} mode={payload.mode} path={path}", flush=True)
    update_job(payload.job_id, status="running", message="Planning sharded import...")
    settings = get_settings()
    shard_payloads: list[BulkJobPayload] = []
    ext = Path(path).suffix.lower()
    def _prestage_progress(status: str, _created: int, deleted: int | None) -> None:
        try:
            if status == "replacing" and deleted is not None:
                update_job(
                    payload.job_id,
                    status="replacing",
                    message=f"Deleting existing features… ({deleted} rows removed so far)",
                )
            else:
                update_job(payload.job_id, status=status)
        except Exception:
            pass

    try:
        if payload.mode == "replace":
            update_job(payload.job_id, status="replacing", message="Deleting existing features before import…")
            replace_collection_prestage_sync(
                payload.collection_id,
                bulk_import_job_id=payload.job_id,
                on_progress=_prestage_progress,
            )
        elif payload.mode == "replace_filtered":
            update_job(payload.job_id, status="replacing", message="Deleting features matching filter before import…")
            replace_collection_prestage_sync(
                payload.collection_id,
                replace_filters=payload.parsed_replace_filters(),
                bulk_import_job_id=payload.job_id,
                on_progress=_prestage_progress,
            )
    except BulkImportCancelled:
        _release_bulk_collection_mutex(payload)
        update_job(payload.job_id, status="cancelled", message="Cancelled by user.")
        print(f"[bulk-parent] cancelled during prestage parent_job_id={payload.job_id}", flush=True)
        return

    if ext == ".zip":
        inner = payload.zip_inner_shp_paths
        if not inner:
            try:
                inner = list_shp_in_zip(path)
            except Exception:
                inner = None
        if inner and len(inner) > 1:
            for i, shp in enumerate(inner, start=1):
                shard_payloads.append(
                    BulkJobPayload(
                        job_id=f"{payload.job_id}:shard:{i}",
                        collection_id=payload.collection_id,
                        storage_key=payload.storage_key,
                        mode="append",
                        batch_size=payload.batch_size,
                        owner_id=payload.owner_id,
                        queue_compute_tiles=False,
                        zip_inner_shp_paths=[shp],
                        job_kind="shard",
                        parent_job_id=payload.job_id,
                        shard_index=i,
                        shard_total=len(inner),
                        finalize_collection=False,
                    )
                )
    elif ext in (".geojsonl", ".geojsonseq", ".jsonl") and bool(settings.bulk_sharded_ingest_enabled):
        update_job(payload.job_id, status="running", message="Splitting upload into shards…")
        shard_keys = _split_jsonl_to_chunk_keys(
            path, payload.job_id, max(1000, int(settings.bulk_shard_lines_per_part or 50000))
        )
        if len(shard_keys) > 1:
            for i, sk in enumerate(shard_keys, start=1):
                shard_payloads.append(
                    BulkJobPayload(
                        job_id=f"{payload.job_id}:shard:{i}",
                        collection_id=payload.collection_id,
                        storage_key=sk,
                        mode="append",
                        batch_size=payload.batch_size,
                        owner_id=payload.owner_id,
                        queue_compute_tiles=False,
                        job_kind="shard",
                        parent_job_id=payload.job_id,
                        shard_index=i,
                        shard_total=len(shard_keys),
                        finalize_collection=False,
                    )
                )

    if not shard_payloads:
        print(f"[bulk-parent] fallback single ingest parent_job_id={payload.job_id}", flush=True)
        replace_filters = payload.parsed_replace_filters() if payload.mode == "replace_filtered" else None
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            payload.mode,
            payload.batch_size,
            on_progress=lambda st, c, _t: update_job(payload.job_id, status=st, items_created=c),
            zip_inner_shp_paths=payload.zip_inner_shp_paths,
            bulk_import_job_id=payload.job_id,
            finalize_collection=True,
            replace_filters=replace_filters or None,
            replace_prestaged=payload.mode == "replace_filtered",
        )
        if err:
            update_job(payload.job_id, status="failed", message=err, items_created=created, items_failed=failed)
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
        _release_bulk_collection_mutex(payload)
        return

    try:
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        try:
            ensure_features_partition_sync(engine, payload.collection_id)
        finally:
            engine.dispose()
    except Exception as e:
        update_job(payload.job_id, status="failed", message=f"Partition setup failed: {e}")
        _release_bulk_collection_mutex(payload)
        print(f"[bulk-parent] partition setup failed parent_job_id={payload.job_id}: {e}", flush=True)
        return

    init_parent_state(
        parent_job_id=payload.job_id,
        collection_id=payload.collection_id,
        expected_shards=len(shard_payloads),
        mode=payload.mode,
        queue_compute_tiles=payload.queue_compute_tiles,
    )
    for sp in shard_payloads:
        enqueue(sp)
    print(f"[bulk-parent] queued parent_job_id={payload.job_id} shards={len(shard_payloads)}", flush=True)
    update_job(
        payload.job_id,
        status="running",
        message=f"Sharded ingest queued ({len(shard_payloads)} shards).",
        items_in=len(shard_payloads),
    )


def _process_shard_bulk_job(payload: BulkJobPayload, path: str) -> None:
    parent_job_id = payload.parent_job_id or ""
    created = 0
    failed = 0
    shard_failed = False
    err_msg = None
    print(
        f"[bulk-shard] start parent_job_id={parent_job_id} shard={payload.shard_index}/{payload.shard_total} path={path}",
        flush=True,
    )
    if parent_job_id:
        _notify_parent_shard_started(payload)
    shard_progress = (
        _make_parent_shard_progress_cb(parent_job_id, payload.shard_index, payload.shard_total)
        if parent_job_id
        else None
    )
    try:
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            "append",
            payload.batch_size,
            on_progress=shard_progress,
            zip_inner_shp_paths=payload.zip_inner_shp_paths,
            bulk_import_job_id=parent_job_id or payload.job_id,
            finalize_collection=False,
        )
        if err:
            shard_failed = True
            err_msg = err
    except Exception as e:
        shard_failed = True
        err_msg = str(e)
    finally:
        if payload.storage_key != (payload.parent_job_id or ""):
            try:
                get_bulk_storage().delete(payload.storage_key)
            except Exception:
                pass
    if shard_failed:
        print(
            f"[bulk-shard] failed parent_job_id={parent_job_id} shard={payload.shard_index}: {err_msg}",
            flush=True,
        )
    else:
        print(
            f"[bulk-shard] done parent_job_id={parent_job_id} shard={payload.shard_index} created={created} failed_items={failed}",
            flush=True,
        )

    if not parent_job_id:
        return
    st = record_parent_shard_result(
        parent_job_id=parent_job_id,
        created=created,
        failed=failed,
        shard_failed=shard_failed,
        shard_index=payload.shard_index,
        error_message=err_msg,
    )
    if not st:
        return
    update_job(
        parent_job_id,
        status="running",
        message=f"Shards {st['completed_shards']}/{st['expected_shards']} done"
        + (f"; failures={st['failed_shards']}" if st["failed_shards"] else ""),
        items_created=st["items_created"],
        items_failed=st["items_failed"],
    )
    if st["completed_shards"] >= st["expected_shards"]:
        error_samples: list[dict] = []
        try:
            error_samples = json.loads(st.get("error_samples_json", "[]") or "[]")
            if not isinstance(error_samples, list):
                error_samples = []
        except Exception:
            error_samples = []
        try:
            finalize_collection_import_sync(payload.collection_id)
            if st["failed_shards"] > 0:
                msg = f"Sharded import completed with errors: {st['failed_shards']} shard(s) failed."
                if error_samples:
                    tail = "; ".join(
                        f"shard {s.get('shard_index')}: {s.get('error')}"
                        for s in error_samples[:5]
                    )
                    if tail:
                        msg += f" Sample errors: {tail}"
                elif err_msg:
                    msg += f" Last error: {err_msg}"
                update_job(
                    parent_job_id,
                    status="completed",
                    message=msg,
                    items_created=st["items_created"],
                    items_failed=st["items_failed"],
                )
                print(f"[bulk-parent] completed_with_errors parent_job_id={parent_job_id} failed_shards={st['failed_shards']}", flush=True)
            else:
                update_job(
                    parent_job_id,
                    status="completed",
                    message=f"Imported {st['items_created']} features via {st['expected_shards']} shards.",
                    items_created=st["items_created"],
                    items_failed=st["items_failed"],
                )
                print(f"[bulk-parent] completed parent_job_id={parent_job_id} shards={st['expected_shards']}", flush=True)
                _queue_tile_build_if_requested(
                    payload.collection_id,
                    payload.owner_id,
                    bool(st.get("queue_compute_tiles", True)),
                )
        except Exception as e:
            print(f"[bulk-parent] finalize failed parent_job_id={parent_job_id}: {e}", flush=True)
            update_job(parent_job_id, status="failed", message=f"Finalize failed: {e}")
        finally:
            try:
                parent_storage_key = get_bulk_import_storage_key(parent_job_id)
                if parent_storage_key:
                    get_bulk_storage().delete(parent_storage_key)
            except Exception:
                pass
            unregister_bulk_import_job(parent_job_id)
            clear_parent_state(parent_job_id)
            release_collection_bulk_mutex(payload.collection_id, parent_job_id)