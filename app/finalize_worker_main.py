#!/usr/bin/env python3
"""
Single-consumer bulk finalize worker.

Load workers COPY into staging tables in parallel; this process alone runs partition
swap DDL (serialized globally via advisory locks). Failed swaps are re-queued with
backoff until they succeed — no fixed retry cap.
"""
from __future__ import annotations

import json
import sys
import time

from app.core.config import get_settings
from app.services.bulk_finalize_queue import FINALIZE_QUEUE_KEY, BulkFinalizePayload
from app.services.bulk_finalize_worker import (
    process_bulk_finalize_job,
    requeue_finalize_after_failure,
)
from app.services.bulk_watchdog import run_finalize_watchdog_pass
from app.services.redis_client import make_redis_client
from app.services.redis_resilience import retry_wait_seconds


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis for the finalize worker.", file=sys.stderr)
        sys.exit(1)
    if not bool(getattr(settings, "bulk_finalize_queue_enabled", True)):
        print("bulk_finalize_queue_enabled=false; finalize worker has nothing to do.", file=sys.stderr)
        sys.exit(0)

    redis_failures = 0
    brpop_timeout = 5.0
    r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
    last_watchdog = time.monotonic()
    watchdog_interval = max(
        30.0, float(getattr(settings, "bulk_finalize_watchdog_interval_seconds", 60.0) or 60.0)
    )

    print("Bulk finalize worker started (single consumer). Waiting for promote jobs…", flush=True)

    while True:
        if time.monotonic() - last_watchdog >= watchdog_interval:
            try:
                run_finalize_watchdog_pass()
            except Exception as e:
                print(f"[bulk-finalize] watchdog error: {e}", file=sys.stderr, flush=True)
            last_watchdog = time.monotonic()

        try:
            result = r.brpop(FINALIZE_QUEUE_KEY, timeout=int(brpop_timeout))
            redis_failures = 0
        except Exception as e:
            redis_failures += 1
            wait_s = retry_wait_seconds(
                redis_failures,
                base=max(0.1, float(settings.redis_retry_base_seconds or 1.0)),
                max_seconds=max(1.0, float(settings.redis_retry_max_seconds or 30.0)),
            )
            print(
                f"[bulk-finalize] Redis unavailable (attempt {redis_failures}), retry in {wait_s:.2f}s: {e}",
                file=sys.stderr,
                flush=True,
            )
            try:
                r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
            except Exception:
                pass
            time.sleep(wait_s)
            continue

        if not result:
            continue

        _key, raw = result
        try:
            payload = BulkFinalizePayload.from_json(raw)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[bulk-finalize] invalid payload: {e}", file=sys.stderr, flush=True)
            continue

        try:
            process_bulk_finalize_job(payload)
        except Exception:
            try:
                requeue_finalize_after_failure(payload)
            except Exception as requeue_err:
                print(
                    f"[bulk-finalize] requeue failed job_id={payload.job_id}: {requeue_err}",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    main()
