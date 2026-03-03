#!/usr/bin/env python3
"""Worker that builds MBTiles for collections. Consumes from Redis tile_build queue."""
from __future__ import annotations

import sys
from app.core.config import get_settings
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.tile_build_queue import (
    TILE_BUILD_QUEUE_KEY,
    TileBuildPayload,
    clear_pending,
    update_tile_build_job,
)
from app.services.tile_builder import build_pmtiles_sync


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis for tile worker.", file=sys.stderr)
        sys.exit(1)
    import redis
    r = redis.from_url(settings.redis_url, decode_responses=True)
    print("Tile worker started. Waiting for build jobs...", flush=True)
    while True:
        result = r.brpop(TILE_BUILD_QUEUE_KEY, timeout=5)
        if not result:
            continue
        _, payload_json = result
        try:
            payload = TileBuildPayload.from_json(payload_json)
        except Exception as e:
            print(f"Invalid payload: {e}", file=sys.stderr)
            continue
        cid = payload.collection_id
        job_id = payload.job_id
        print(f"Building tiles for {cid} (job_id={job_id})...", flush=True)
        update_tile_build_job(job_id, status="building")
        err = build_pmtiles_sync(cid)
        clear_pending(cid)
        if err:
            update_tile_build_job(job_id, status="failed", message=err)
            print(f"Build FAILED for {cid}: {err}", file=sys.stderr, flush=True)
        else:
            update_tile_build_job(job_id, status="completed")
            invalidate_collection_cache(cid)
            print(f"Build completed for {cid} (job_id={job_id})", flush=True)


if __name__ == "__main__":
    main()
