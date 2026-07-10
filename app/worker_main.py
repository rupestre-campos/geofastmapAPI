#!/usr/bin/env python3
"""
Standalone bulk import worker. Consumes jobs from Redis and runs imports.
Run when BULK_QUEUE_TYPE=redis (e.g. in a separate container or machine).

One job loads one file; GeoJSONSeq parsing uses (cpu_count - 1) processes inside the job.
Default bulk_worker_max_concurrent=1 — scale by running more worker containers.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from app.core.config import get_settings
from app.services.bulk_queue import QUEUE_KEY, BulkJobPayload
from app.services.bulk_watchdog import run_bulk_watchdog_pass
from app.services.bulk_worker import cleanup_orphan_bulk_uploads, process_bulk_job
from app.services.redis_client import make_redis_client
from app.services.redis_resilience import retry_wait_seconds

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _process_payload_json(payload_json: str) -> None:
    try:
        payload = BulkJobPayload.from_json(payload_json)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Invalid job payload: {e}", file=sys.stderr, flush=True)
        return
    try:
        process_bulk_job(payload)
    except Exception as e:
        print(f"Job {payload.job_id} error: {e}", file=sys.stderr, flush=True)


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis to use the standalone worker.", file=sys.stderr)
        sys.exit(1)

    cleanup_orphan_bulk_uploads()

    _raster_root = Path(settings.raster_storage_path)
    try:
        _raster_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"WARNING: cannot create RASTER_STORAGE_PATH {_raster_root}: {e}", file=sys.stderr, flush=True)
    _w = os.access(_raster_root, os.W_OK) if _raster_root.exists() else False
    print(
        f"RASTER_STORAGE_PATH={_raster_root} resolved={_raster_root.resolve()} "
        f"exists={_raster_root.exists()} writable={_w}",
        flush=True,
    )

    redis_failures = 0

    def _connect_consumer(brpop_timeout: float):
        return make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)

    r = _connect_consumer(5.0)

    max_workers = max(1, int(getattr(settings, "bulk_worker_max_concurrent", 2) or 2))
    dispatch_cooldown = max(
        0.0, float(getattr(settings, "bulk_worker_dispatch_cooldown_seconds", 0.5) or 0.0)
    )
    print(
        f"Bulk import worker started id={WORKER_ID} (max {max_workers} concurrent jobs, "
        f"dispatch_cooldown={dispatch_cooldown:.2f}s). Waiting for jobs...",
        flush=True,
    )

    futures: set[Future[None]] = set()
    last_watchdog = time.monotonic()
    watchdog_interval = max(60.0, float(getattr(settings, "bulk_watchdog_interval_seconds", 300.0) or 300.0))

    def _reap_done() -> None:
        nonlocal futures
        done = {f for f in futures if f.done()}
        for fut in done:
            futures.discard(fut)
            try:
                fut.result()
            except Exception as e:
                print(f"[bulk-worker] job thread error: {e}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bulkjob") as executor:
        while True:
            if time.monotonic() - last_watchdog >= watchdog_interval:
                try:
                    run_bulk_watchdog_pass()
                except Exception as e:
                    print(f"[bulk-worker] watchdog error: {e}", file=sys.stderr, flush=True)
                last_watchdog = time.monotonic()
            _reap_done()
            in_flight = len(futures)
            slots = max_workers - in_flight

            if slots <= 0:
                done_set, not_done = wait(futures, return_when=FIRST_COMPLETED, timeout=120)
                futures = set(not_done)
                for fut in done_set:
                    try:
                        fut.result()
                    except Exception as e:
                        print(f"[bulk-worker] job thread error: {e}", file=sys.stderr, flush=True)
                continue

            brpop_timeout = 1 if in_flight > 0 else 5
            try:
                result = r.brpop(QUEUE_KEY, timeout=brpop_timeout)
                redis_failures = 0
            except Exception as e:
                redis_failures += 1
                wait_s = retry_wait_seconds(
                    redis_failures,
                    base=max(0.1, float(settings.redis_retry_base_seconds or 1.0)),
                    max_seconds=max(1.0, float(settings.redis_retry_max_seconds or 30.0)),
                )
                print(
                    f"[bulk-worker] Redis unavailable (attempt {redis_failures}), retrying in {wait_s:.2f}s: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    r = _connect_consumer(brpop_timeout)
                except Exception:
                    pass
                time.sleep(wait_s)
                continue

            if not result:
                if in_flight > 0:
                    done_set, not_done = wait(futures, return_when=FIRST_COMPLETED, timeout=2)
                    futures = set(not_done)
                    for fut in done_set:
                        try:
                            fut.result()
                        except Exception as e:
                            print(f"[bulk-worker] job thread error: {e}", file=sys.stderr, flush=True)
                continue

            _key, payload_json = result
            try:
                _claimed = BulkJobPayload.from_json(payload_json)
                print(
                    f"[bulk-worker] claimed job_id={_claimed.job_id} "
                    f"collection={_claimed.collection_id} worker={WORKER_ID} "
                    f"in_flight={in_flight + 1}/{max_workers}",
                    flush=True,
                )
            except Exception:
                pass
            futures.add(executor.submit(_process_payload_json, payload_json))

            # Fair dispatch: if this host still has free slots, pause briefly so an idle
            # worker machine can claim the next queued job before we grab it ourselves.
            if dispatch_cooldown > 0 and (max_workers - len(futures)) > 0:
                deadline = time.monotonic() + dispatch_cooldown
                while time.monotonic() < deadline:
                    _reap_done()
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
