#!/usr/bin/env python3
"""
Standalone bulk import worker. Consumes jobs from Redis and runs imports.
Run when BULK_QUEUE_TYPE=redis (e.g. in a separate container or machine).
One process = one job at a time; scale by running more workers.
"""
from __future__ import annotations

import json
import sys

from app.core.config import get_settings
from app.services.bulk_queue import QUEUE_KEY, BulkJobPayload
from app.services.bulk_worker import process_bulk_job


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis to use the standalone worker.", file=sys.stderr)
        sys.exit(1)

    import redis
    r = redis.from_url(settings.redis_url, decode_responses=True)

    print("Bulk import worker started. Waiting for jobs...", flush=True)
    while True:
        # BRPOP blocks until a job is available; one job at a time per worker
        result = r.brpop(QUEUE_KEY, timeout=5)
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
