#!/usr/bin/env python3
"""Worker for OGC API - Processes and property-index sync jobs (Redis queues)."""
from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.services.job_store import get_job, update_job
from app.services.process_queue import (
    PROCESS_QUEUE_KEY,
    ProcessJobPayload,
    get_process_job_meta,
    list_process_job_ids,
    set_process_job_result,
)
from app.services.process_worker import (
    _cleanup_result_collection_sync,
    _default_result_collection_id,
    _default_result_collection_id_feature,
    _update_feature_count_sync,
    cleanup_process_worker_temp_dir,
    process_process_job_sync,
)
from app.services.tile_build_queue import create_tile_build_job, enqueue_tile_build
from app.services.redis_resilience import retry_wait_seconds


def _recover_orphaned_running_jobs(settings) -> None:
    """Mark any process jobs still 'running' as failed and remove partial result data."""
    engine = create_engine(
        settings.database_sync_url,
        pool_pre_ping=True,
        future=True,
        pool_size=2,
        max_overflow=0,
    )
    try:
        for job_id in list_process_job_ids(100):
            job = get_job(job_id)
            if not job or job.status != "running":
                continue
            meta = get_process_job_meta(job_id)
            if not meta:
                update_job(job_id, status="failed", message="Job interrupted (worker restarted).")
                continue
            result_id = meta.get("result_collection_id")
            if not result_id and meta.get("collection_id_a") and meta.get("collection_id_b"):
                result_id = _default_result_collection_id(
                    meta.get("process_id", ""),
                    meta.get("collection_id_a", ""),
                    meta.get("collection_id_b", ""),
                )
            if not result_id and meta.get("collection_ids") and meta.get("feature_id"):
                result_id = _default_result_collection_id_feature(
                    meta.get("process_id", ""),
                    meta.get("feature_id", ""),
                    meta.get("collection_ids", []),
                )
            if result_id:
                try:
                    _cleanup_result_collection_sync(engine, result_id)
                except Exception:
                    pass
            update_job(
                job_id,
                status="failed",
                message="Job interrupted (worker restarted). Partial data removed.",
            )
    finally:
        engine.dispose()


def _run_property_index_payload(payload_json: str) -> None:
    from app.services.property_index_queue import PropertyIndexPayload
    from app.services.property_index_worker import run_property_index_job_sync

    try:
        idx_payload = PropertyIndexPayload.from_json(payload_json)
    except Exception as e:
        print(f"Invalid property-index payload: {e}", file=sys.stderr, flush=True)
        return
    print(
        f"Property index sync for {idx_payload.collection_id} (job_id={idx_payload.job_id})...",
        flush=True,
    )
    try:
        run_property_index_job_sync(idx_payload)
        print(
            f"Property index sync completed for {idx_payload.collection_id} "
            f"(job_id={idx_payload.job_id})",
            flush=True,
        )
    except Exception as e:
        print(f"Property index job FAILED: {e}", file=sys.stderr, flush=True)


def _run_process_payload(payload_json: str) -> None:
    try:
        payload = ProcessJobPayload.from_json(payload_json)
    except Exception as e:
        print(f"Invalid payload: {e}", file=sys.stderr, flush=True)
        return
    job_id = payload.job_id
    job = get_job(job_id)
    if job and job.status == "cancelled":
        print(f"Skipping cancelled job {job_id}", flush=True)
        return
    if payload.is_feature_vs_layers:
        print(
            f"Running {payload.process_id} (feature vs {len(payload.collection_ids)} layers) "
            f"job_id={job_id}...",
            flush=True,
        )
    else:
        print(
            f"Running {payload.process_id} ({payload.collection_id_a}, {payload.collection_id_b}) "
            f"job_id={job_id}...",
            flush=True,
        )
    update_job(job_id, status="running", message=f"Computing {payload.process_id}...")
    err, count, items_in, result_id = process_process_job_sync(payload)
    if err == "cancelled":
        update_job(job_id, status="cancelled", message="Cancelled by user.")
        print(f"Process cancelled: {job_id}", flush=True)
        return
    if err:
        update_job(job_id, status="failed", message=err)
        print(f"Process FAILED: {err}", file=sys.stderr, flush=True)
        return
    set_process_job_result(job_id, result_id)
    update_job(
        job_id,
        status="completed",
        message=f"Result collection: {result_id}. Input: {items_in}. Output: {count}.",
        items_in=items_in,
        items_created=count,
    )
    if getattr(payload, "queue_compute_tiles", True):
        try:
            from app.services.tile_build_queue import TileBuildOptions, update_tile_build_job

            opts = None
            if getattr(payload, "tile_build_options", None):
                opts = TileBuildOptions.from_dict(payload.tile_build_options)
            tile_job = create_tile_build_job(result_id)
            update_tile_build_job(tile_job.job_id, message="Tile build")
            enqueue_tile_build(result_id, tile_job.job_id, options=opts)
        except Exception:
            pass
    print(f"Process completed. Result: {result_id} ({count} features)", flush=True)


def main() -> None:
    settings = get_settings()
    if settings.process_queue_type != "redis":
        print("Set PROCESS_QUEUE_TYPE=redis for process worker.", file=sys.stderr)
        sys.exit(1)
    cleanup_process_worker_temp_dir()
    _recover_orphaned_running_jobs(settings)
    try:
        engine = create_engine(
            settings.database_sync_url,
            pool_pre_ping=True,
            future=True,
            pool_size=2,
            max_overflow=0,
        )
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT id FROM collections "
                        "WHERE feature_count = 0 "
                        "AND (id LIKE 'intersection_%' OR id LIKE 'erase_%')"
                    )
                ).fetchall()
            for r in rows:
                try:
                    _update_feature_count_sync(engine, r.id)
                except Exception:
                    continue
        finally:
            engine.dispose()
    except Exception:
        pass

    from app.services.property_index_queue import PROPERTY_INDEX_QUEUE_KEY
    from app.services.redis_client import make_redis_client

    brpop_timeout = 5
    r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
    redis_failures = 0
    index_concurrency = max(1, int(getattr(settings, "property_index_worker_max_concurrent", 2) or 2))
    print(
        f"Process worker started (process + property-index queues; "
        f"index concurrency={index_concurrency}). Waiting for jobs...",
        flush=True,
    )

    index_futures: set[Future[None]] = set()
    with ThreadPoolExecutor(
        max_workers=index_concurrency, thread_name_prefix="propidx"
    ) as index_pool:

        def _reap_index_futures() -> None:
            done = {f for f in index_futures if f.done()}
            for fut in done:
                index_futures.discard(fut)
                try:
                    fut.result()
                except Exception as e:
                    print(f"[process-worker] index thread error: {e}", file=sys.stderr, flush=True)

        while True:
            _reap_index_futures()
            # Prefer property-index queue so large index backlogs are not starved by process jobs.
            # When index pool is saturated, only wait on the process queue.
            try:
                if len(index_futures) < index_concurrency:
                    # redis-py: brpop(keys, timeout=...) — keys must be a list/tuple,
                    # not multiple positional args (those collide with timeout).
                    result = r.brpop(
                        [PROPERTY_INDEX_QUEUE_KEY, PROCESS_QUEUE_KEY],
                        timeout=brpop_timeout,
                    )
                else:
                    result = r.brpop(PROCESS_QUEUE_KEY, timeout=brpop_timeout)
                redis_failures = 0
            except Exception as e:
                redis_failures += 1
                wait = retry_wait_seconds(
                    redis_failures,
                    base=max(0.1, float(settings.redis_retry_base_seconds or 1.0)),
                    max_seconds=max(1.0, float(settings.redis_retry_max_seconds or 30.0)),
                )
                print(
                    f"[process-worker] Redis unavailable (attempt {redis_failures}), "
                    f"retrying in {wait:.2f}s: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
                except Exception:
                    pass
                time.sleep(wait)
                continue
            if not result:
                continue
            queue_key, payload_json = result
            key = queue_key.decode() if isinstance(queue_key, (bytes, bytearray)) else str(queue_key)
            if key == PROPERTY_INDEX_QUEUE_KEY:
                fut = index_pool.submit(_run_property_index_payload, payload_json)
                index_futures.add(fut)
                continue
            # Geometric process jobs still run serially on the main thread.
            _run_process_payload(payload_json)


if __name__ == "__main__":
    main()
