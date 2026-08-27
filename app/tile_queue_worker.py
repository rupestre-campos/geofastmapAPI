#!/usr/bin/env python3
"""
Dynamic tile queue worker (TiTiler-style): LIFO Redis jobs, DB-free encode.

API fills Redis search GeoJSON once; workers only filter + encode MVT and write
tile cache. Spawns multiple OS processes so one container uses many CPU cores.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from multiprocessing import Event, Process, get_context
from typing import Any

from app.core.config import get_settings


def _resolve_concurrency(settings: Any) -> int:
    configured = int(getattr(settings, "tiles_dynamic_queue_concurrency", 0) or 0)
    if configured > 0:
        return max(1, configured)
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, 8))


def _encode_job(job: dict[str, Any], precompute_adjacent_zooms: bool = True) -> str:
    """Encode one tile. Returns a short status tag for logs."""
    from app.services.dynamic_tile_cache import (
        get_search_result,
        push_tile_job,
        set_tile_with_params,
    )
    from app.services.dynamic_tile_geojson import filter_geojson_to_tile_bbox
    from app.services.mvt_encode import encode_geojson_to_mvt

    collection_id = job["collection_id"]
    params_key = job["params_key"]
    z = int(job["z"])
    x = int(job["x"])
    y = int(job["y"])

    geojson_bytes = get_search_result(collection_id, params_key)
    if not geojson_bytes:
        return "cache_miss"

    filtered = filter_geojson_to_tile_bbox(geojson_bytes, z, x, y)
    try:
        tile_bytes = encode_geojson_to_mvt(filtered, collection_id, z, x, y)
    except Exception as e:
        print(f"[tile worker] MVT encode failed {collection_id} {z}/{x}/{y}: {e}", file=sys.stderr)
        return "encode_error"

    set_tile_with_params(collection_id, z, x, y, params_key, tile_bytes)

    if precompute_adjacent_zooms:
        # Parent/children go to the LIFO queue so newer viewport work still wins.
        if z > 0:
            push_tile_job(collection_id, params_key, z - 1, x // 2, y // 2)
        if z < 22:
            push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2)
            push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2)
            push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2 + 1)
            push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2 + 1)
    return "ok"


def process_one_job(precompute_adjacent_zooms: bool = True) -> bool:
    """Process one tile job. Returns True if a job ran."""
    from app.services.dynamic_tile_cache import pop_tile_job

    job = pop_tile_job(timeout=5)
    if not job:
        return False
    _encode_job(job, precompute_adjacent_zooms)
    return True


def _consumer_process(stop_event: Any, worker_id: int) -> None:
    """Independent process: BLPOP LIFO jobs and encode (no shared DB)."""
    from app.services.dynamic_tile_cache import pop_tile_job

    print(f"[tile worker #{worker_id}] ready", flush=True)
    while not stop_event.is_set():
        job = pop_tile_job(timeout=1)
        if not job:
            continue
        try:
            _encode_job(job, True)
        except Exception as e:
            print(f"[tile worker #{worker_id}] job failed: {e}", file=sys.stderr)


def main() -> None:
    settings = get_settings()
    if not getattr(settings, "tiles_dynamic_use_queue", False):
        print("Set TILES_DYNAMIC_USE_QUEUE=true to run tile queue workers.", file=sys.stderr)
        sys.exit(1)

    concurrency = _resolve_concurrency(settings)
    print(
        f"Dynamic tile queue worker started (LIFO, processes={concurrency}). Waiting for jobs...",
        flush=True,
    )

    # spawn: each child gets a clean interpreter (safe with Redis clients).
    ctx = get_context("spawn")
    stop_event = ctx.Event()
    procs: list[Process] = []

    def _stop(*_args: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    for i in range(concurrency):
        p = ctx.Process(target=_consumer_process, args=(stop_event, i + 1), daemon=True)
        p.start()
        procs.append(p)

    try:
        while not stop_event.is_set():
            # Restart crashed children so one bad encode cannot shrink capacity.
            alive = []
            for i, p in enumerate(procs):
                if p.is_alive():
                    alive.append(p)
                    continue
                print(f"[tile worker] restarting dead process #{i + 1}", flush=True)
                np = ctx.Process(target=_consumer_process, args=(stop_event, i + 1), daemon=True)
                np.start()
                alive.append(np)
            procs = alive
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()

    stop_event.set()
    for p in procs:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    print("Dynamic tile queue worker stopped.", flush=True)


if __name__ == "__main__":
    main()
