"""Process a single bulk import job (used by in-process consumer or standalone worker)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.config import get_settings
from app.services.bulk_import import (
    finalize_collection_import_sync,
    list_shp_in_zip,
    replace_collection_prestage_sync,
    run_bulk_import_sync,
)
from app.services.bulk_queue import (
    QUEUE_KEY,
    BulkJobPayload,
    clear_parent_state,
    enqueue,
    get_bulk_import_storage_key,
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


def process_bulk_job(payload: BulkJobPayload) -> None:
    """Load file from storage, run import, update job status, delete file. Optionally queue tile build."""
    storage = get_bulk_storage()
    path = storage.get_path_or_uri(payload.storage_key)
    print(
        f"[bulk-worker] start job_id={payload.job_id} kind={payload.job_kind} collection={payload.collection_id} storage_key={payload.storage_key}",
        flush=True,
    )

    job = get_job(payload.job_id)
    if job and job.status == "cancelled":
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
    if payload.mode == "replace":
        replace_collection_prestage_sync(payload.collection_id)
    elif payload.mode == "replace_filtered":
        replace_collection_prestage_sync(
            payload.collection_id,
            replace_filters=payload.parsed_replace_filters(),
        )

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
    try:
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            "append",
            payload.batch_size,
            on_progress=None,
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