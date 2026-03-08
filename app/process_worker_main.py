#!/usr/bin/env python3
"""Worker for OGC API - Processes (intersection, erase). Consumes from Redis process queue."""
from __future__ import annotations

import sys

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
    _safe_result_collection_id,
    _update_feature_count_sync,
    cleanup_process_worker_temp_dir,
    process_process_job_sync,
)
from app.services.tile_build_queue import create_tile_build_job, enqueue_tile_build


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
            result_id = _safe_result_collection_id(
                meta.get("process_id", ""),
                meta.get("collection_id_a", ""),
                meta.get("collection_id_b", ""),
            )
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


def main() -> None:
    settings = get_settings()
    if settings.process_queue_type != "redis":
        print("Set PROCESS_QUEUE_TYPE=redis for process worker.", file=sys.stderr)
        sys.exit(1)
    cleanup_process_worker_temp_dir()
    _recover_orphaned_running_jobs(settings)
    # One-time backfill: fix feature_count for existing process result collections that still show 0.
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
        # Backfill is best-effort; continue even if it fails.
        pass
    import redis
    r = redis.from_url(settings.redis_url, decode_responses=True)
    print("Process worker started. Waiting for jobs...", flush=True)
    while True:
        result = r.brpop(PROCESS_QUEUE_KEY, timeout=5)
        if not result:
            continue
        _, payload_json = result
        try:
            payload = ProcessJobPayload.from_json(payload_json)
        except Exception as e:
            print(f"Invalid payload: {e}", file=sys.stderr)
            continue
        job_id = payload.job_id
        job = get_job(job_id)
        if job and job.status == "cancelled":
            print(f"Skipping cancelled job {job_id}", flush=True)
            continue
        print(f"Running {payload.process_id} ({payload.collection_id_a}, {payload.collection_id_b}) job_id={job_id}...", flush=True)
        update_job(job_id, status="running", message=f"Computing {payload.process_id}...")
        err, count, items_in = process_process_job_sync(payload)
        if err:
            update_job(job_id, status="failed", message=err)
            print(f"Process FAILED: {err}", file=sys.stderr, flush=True)
        else:
            result_id = _safe_result_collection_id(
                payload.process_id, payload.collection_id_a, payload.collection_id_b
            )
            set_process_job_result(job_id, result_id)
            update_job(
                job_id,
                status="completed",
                message=f"Result collection: {result_id}. Input: {items_in}. Output: {count}.",
                items_in=items_in,
                items_created=count,
            )
            tile_job = create_tile_build_job(result_id)
            enqueue_tile_build(result_id, tile_job.job_id)
            print(f"Process completed. Result: {result_id} ({count} features)", flush=True)


if __name__ == "__main__":
    main()
