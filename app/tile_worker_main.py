#!/usr/bin/env python3
"""Worker that builds MBTiles for collections. Consumes from Redis tile_build queue."""
from __future__ import annotations

import os
import sys

from app.core.config import get_settings
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.job_store import list_all_jobs
from app.services.tile_build_queue import (
    TILE_BUILD_QUEUE_KEY,
    TileBuildPayload,
    clear_pending,
    get_latest_tile_build_job,
    get_tile_build_job,
    update_tile_build_job,
)
from app.services.bulk_collection_activity import wait_until_collection_bulk_idle
from app.services.tile_build_verify import format_build_success_message, verify_mbtiles_artifact
from app.services.tile_builder import BUILD_CANCELLED, build_pmtiles_sync
from app.services.composite_tile_builder import build_composite_pmtiles_sync


def _collection_type_sync(collection_id: str) -> str:
    from sqlalchemy import create_engine, text

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT collection_type FROM collections WHERE id = :cid"),
                {"cid": collection_id},
            ).first()
        return str(row[0]) if row and row[0] else ""
    finally:
        engine.dispose()


def _composite_member_ids_sync(collection_id: str) -> list[str]:
    import json
    from sqlalchemy import create_engine, text

    from app.services.composite_collections import member_collection_ids, parse_composite_members

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT composite_members FROM collections WHERE id = :cid"),
                {"cid": collection_id},
            ).first()
        raw = row[0] if row else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        return member_collection_ids(parse_composite_members(raw))
    finally:
        engine.dispose()


def _recover_orphaned_tile_builds() -> None:
    """Mark any tile build jobs stuck in 'running' as failed (worker restarted; build no longer active)."""
    for job in list_all_jobs(limit=300):
        if job.status != "running":
            continue
        latest = get_latest_tile_build_job(job.collection_id)
        if latest is None or latest.job_id != job.job_id:
            continue
        update_tile_build_job(job.job_id, status="failed", message="Worker restarted; build interrupted.")
        clear_pending(job.collection_id)
        print(f"Recovered orphaned tile build job {job.job_id} ({job.collection_id})", flush=True)


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis for tile worker.", file=sys.stderr)
        sys.exit(1)
    _recover_orphaned_tile_builds()
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
        job = get_tile_build_job(job_id)
        if job and job.status == "cancelled":
            print(f"Skipping cancelled tile build for {cid} (job_id={job_id})", flush=True)
            clear_pending(cid)
            continue
        print(f"Building tiles for {cid} (job_id={job_id})...", flush=True)
        update_tile_build_job(job_id, status="building")

        def stop_check() -> bool:
            j = get_tile_build_job(job_id)
            return j is not None and j.status == "cancelled"

        if not wait_until_collection_bulk_idle(
            cid,
            stop_check=stop_check,
            on_waiting_message=lambda: update_tile_build_job(
                job_id, message="Waiting for bulk import to finish..."
            ),
        ):
            print(f"Tile build for {cid} cancelled while waiting for bulk import (job_id={job_id})", flush=True)
            clear_pending(cid)
            continue

        if _collection_type_sync(cid) == "composite":
            member_ids = _composite_member_ids_sync(cid)
            err = build_composite_pmtiles_sync(
                cid,
                member_ids,
                options=payload.options,
                stop_check=stop_check,
            )
        else:
            err = build_pmtiles_sync(cid, options=payload.options, stop_check=stop_check)
        clear_pending(cid)
        if err == BUILD_CANCELLED:
            update_tile_build_job(job_id, status="cancelled", message="Tile build cancelled")
            print(f"Tile build for {cid} was cancelled; intermediate files cleaned up", flush=True)
            continue
        job = get_tile_build_job(job_id)
        if job and job.status == "cancelled":
            print(f"Tile build for {cid} was cancelled; not marking completed", flush=True)
            continue
        if err:
            update_tile_build_job(job_id, status="failed", message=err)
            print(f"Build FAILED for {cid}: {err}", file=sys.stderr, flush=True)
        else:
            out_path = os.path.join(settings.tiles_storage_path, f"{cid}.mbtiles")
            verify_err = verify_mbtiles_artifact(out_path)
            if verify_err:
                update_tile_build_job(job_id, status="failed", message=verify_err)
                print(f"Build FAILED for {cid}: {verify_err}", file=sys.stderr, flush=True)
            else:
                update_tile_build_job(
                    job_id,
                    status="completed",
                    message=format_build_success_message(out_path),
                )
                invalidate_collection_cache(cid)
                print(f"Build completed for {cid} (job_id={job_id})", flush=True)


if __name__ == "__main__":
    main()
