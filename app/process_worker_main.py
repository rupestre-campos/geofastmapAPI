#!/usr/bin/env python3
"""Worker for OGC API - Processes (intersection, erase). Consumes from Redis process queue."""
from __future__ import annotations

import sys

from app.core.config import get_settings
from app.services.process_queue import PROCESS_QUEUE_KEY, ProcessJobPayload
from app.services.process_worker import process_process_job_sync
from app.services.job_store import update_job


def main() -> None:
    settings = get_settings()
    if settings.process_queue_type != "redis":
        print("Set PROCESS_QUEUE_TYPE=redis for process worker.", file=sys.stderr)
        sys.exit(1)
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
        print(f"Running {payload.process_id} ({payload.collection_id_a}, {payload.collection_id_b}) job_id={job_id}...", flush=True)
        update_job(job_id, status="running", message=f"Computing {payload.process_id}...")
        err, count = process_process_job_sync(payload)
        if err:
            update_job(job_id, status="failed", message=err)
            print(f"Process FAILED: {err}", file=sys.stderr, flush=True)
        else:
            from app.services.process_worker import _safe_result_collection_id
            result_id = _safe_result_collection_id(
                payload.process_id, payload.collection_id_a, payload.collection_id_b
            )
            update_job(
                job_id,
                status="completed",
                message=f"Result collection: {result_id}. {count} features.",
                items_created=count,
            )
            print(f"Process completed. Result: {result_id} ({count} features)", flush=True)


if __name__ == "__main__":
    main()
