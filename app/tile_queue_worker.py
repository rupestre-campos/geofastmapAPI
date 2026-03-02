#!/usr/bin/env python3
"""
Tiler queue worker: reads tile jobs from Redis, gets GeoJSON from search result cache
(no DB), filters to tile bbox, encodes MVT in-process, caches tile. Run 3 instances for
current zoom + precompute above/below. No database connection.
"""
from __future__ import annotations

import sys

from app.core.config import get_settings
from app.services.dynamic_tile_cache import (
    get_search_result,
    pop_tile_job,
    push_tile_job,
    set_tile_with_params,
)
from app.services.dynamic_tile_geojson import filter_geojson_to_tile_bbox
from app.services.mvt_encode import encode_geojson_to_mvt


def process_one_job(precompute_adjacent_zooms: bool = True) -> bool:
    """Process one tile job. Returns True if a job was processed."""
    job = pop_tile_job(timeout=5)
    if not job:
        return False
    collection_id = job["collection_id"]
    params_key = job["params_key"]
    z = int(job["z"])
    x = int(job["x"])
    y = int(job["y"])

    geojson_bytes = get_search_result(collection_id, params_key)
    if not geojson_bytes:
        # Search cache miss; skip (API should have ensured cache before pushing)
        return True
    filtered = filter_geojson_to_tile_bbox(geojson_bytes, z, x, y)
    try:
        tile_bytes = encode_geojson_to_mvt(filtered, collection_id, z, x, y)
    except Exception as e:
        print(f"[tile worker] MVT encode failed {collection_id} {z}/{x}/{y}: {e}", file=sys.stderr)
        return True
    set_tile_with_params(collection_id, z, x, y, params_key, tile_bytes)

    if precompute_adjacent_zooms:
        # Precompute parent (z-1) and children (z+1) for faster pan/zoom
        if z > 0:
            push_tile_job(collection_id, params_key, z - 1, x // 2, y // 2)
        if z < 22:
            push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2)
            push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2)
            push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2 + 1)
            push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2 + 1)
    return True


def main() -> None:
    settings = get_settings()
    if not getattr(settings, "tiles_dynamic_use_queue", False):
        print("Set TILES_DYNAMIC_USE_QUEUE=true to run tile queue workers.", file=sys.stderr)
        sys.exit(1)
    print("Tile queue worker started. Waiting for jobs...", flush=True)
    while True:
        process_one_job(precompute_adjacent_zooms=True)


if __name__ == "__main__":
    main()
