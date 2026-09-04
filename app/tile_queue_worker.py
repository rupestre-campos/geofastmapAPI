#!/usr/bin/env python3
"""
Dynamic tile queue worker (TiTiler-style): LIFO Redis jobs, DB-free encode.

API fills Redis search GeoJSON once and may enqueue adjacent zooms once.
Workers only filter + encode MVT and write tile cache — they must not
re-enqueue neighbors (that recursively floods Redis / docker-proxy).
"""
from __future__ import annotations

import os
import signal
import sys
import time
from multiprocessing import Process, get_context
from typing import Any

from app.core.config import get_settings


def _set_process_title(title: str) -> None:
    """Make workers readable in htop (comm / argv)."""
    try:
        import setproctitle

        setproctitle.setproctitle(title)
        return
    except Exception:
        pass
    try:
        import ctypes

        # PR_SET_NAME: 15-byte thread/process name shown by htop
        ctypes.CDLL(None).prctl(15, title[:15].encode("utf-8", "replace"), 0, 0, 0)
    except Exception:
        pass


def _resolve_concurrency(settings: Any) -> int:
    configured = int(getattr(settings, "tiles_dynamic_queue_concurrency", 0) or 0)
    if configured > 0:
        return max(1, configured)
    cpus = os.cpu_count() or 2
    # Leave headroom for API / Postgres / Redis on small hosts (htop showed 8 cores saturated).
    return max(1, min(cpus, 4))


def _encode_job(job: dict[str, Any]) -> str:
    """Encode one tile. Returns a short status tag for logs. Never enqueues more jobs."""
    from app.services.dynamic_tile_cache import (
        get_search_result,
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
        print(f"[dyn-tile] MVT encode failed {collection_id} {z}/{x}/{y}: {e}", file=sys.stderr)
        return "encode_error"

    set_tile_with_params(collection_id, z, x, y, params_key, tile_bytes)
    return "ok"


def process_one_job(precompute_adjacent_zooms: bool = False) -> bool:
    """Process one tile job. Returns True if a job ran.

    ``precompute_adjacent_zooms`` is ignored (API owns one-hop prefetch). Kept for call-site compat.
    """
    from app.services.dynamic_tile_cache import pop_tile_job

    _ = precompute_adjacent_zooms
    job = pop_tile_job(timeout=5)
    if not job:
        return False
    _encode_job(job)
    return True


def _consumer_process(stop_event: Any, worker_id: int) -> None:
    """Independent process: BLPOP LIFO jobs and encode (no shared DB)."""
    from app.services.dynamic_tile_cache import pop_tile_job

    _set_process_title(f"geofast-dyn-tile-{worker_id}")
    print(f"[dyn-tile #{worker_id}] ready", flush=True)
    while not stop_event.is_set():
        job = pop_tile_job(timeout=1)
        if not job:
            continue
        try:
            _encode_job(job)
        except Exception as e:
            print(f"[dyn-tile #{worker_id}] job failed: {e}", file=sys.stderr)


def main() -> None:
    settings = get_settings()
    if not getattr(settings, "tiles_dynamic_use_queue", False):
        print("Set TILES_DYNAMIC_USE_QUEUE=true to run tile queue workers.", file=sys.stderr)
        sys.exit(1)

    _set_process_title("geofast-dyn-tile-main")
    concurrency = _resolve_concurrency(settings)
    print(
        f"Dynamic tile queue worker started (LIFO, processes={concurrency}, no recursive prefetch). Waiting for jobs...",
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
        p = ctx.Process(
            target=_consumer_process,
            args=(stop_event, i + 1),
            name=f"geofast-dyn-tile-{i + 1}",
            daemon=True,
        )
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
                print(f"[dyn-tile] restarting dead process #{i + 1}", flush=True)
                np = ctx.Process(
                    target=_consumer_process,
                    args=(stop_event, i + 1),
                    name=f"geofast-dyn-tile-{i + 1}",
                    daemon=True,
                )
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
