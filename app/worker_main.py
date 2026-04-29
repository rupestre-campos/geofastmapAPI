#!/usr/bin/env python3
"""
Standalone bulk import worker. Consumes jobs from Redis and runs imports.
Run when BULK_QUEUE_TYPE=redis (e.g. in a separate container or machine).
One process = one job at a time; scale by running more workers.
"""
from __future__ import annotations

import json
import sys
import time

from app.core.config import get_settings
from app.services.bulk_queue import QUEUE_KEY, BulkJobPayload
from app.services.bulk_worker import cleanup_orphan_bulk_uploads, process_bulk_job
from app.services.redis_resilience import retry_wait_seconds


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis to use the standalone worker.", file=sys.stderr)
        sys.exit(1)

    cleanup_orphan_bulk_uploads()

    import redis
    r = redis.from_url(settings.redis_url, decode_responses=True)
    redis_failures = 0

    print("Bulk import worker started. Waiting for jobs...", flush=True)
    while True:
        # BRPOP blocks until a job is available; one job at a time per worker
        try:
            result = r.brpop(QUEUE_KEY, timeout=5)
            redis_failures = 0
        except Exception as e:
            redis_failures += 1
            wait = retry_wait_seconds(
                redis_failures,
                base=max(0.1, float(settings.redis_retry_base_seconds or 1.0)),
                max_seconds=max(1.0, float(settings.redis_retry_max_seconds or 30.0)),
            )
            print(
                f"[bulk-worker] Redis unavailable (attempt {redis_failures}), retrying in {wait:.2f}s: {e}",
                file=sys.stderr,
                flush=True,
            )
            try:
                r = redis.from_url(settings.redis_url, decode_responses=True)
            except Exception:
                pass
            time.sleep(wait)
            continue
        if not result:
            continue
        _key, payload_json = result
        try:
            payload = BulkJobPayload.from_json(payload_json)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Invalid job payload: {e}", file=sys.stderr)
            continue
        try:
            process_bulk_job(payload)
        except Exception as e:
            print(f"Job {payload.job_id} error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
