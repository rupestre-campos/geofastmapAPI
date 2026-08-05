#!/usr/bin/env python3
"""Worker that builds MBTiles for collections. Consumes from Redis tile_build queue."""
from __future__ import annotations

import os
import sys
import threading
import time

from app.core.config import get_settings
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.job_store import list_all_jobs
from app.services.tile_build_queue import (
    TILE_BUILD_QUEUE_KEY,
    TileBuildPayload,
    clear_pending,
    get_latest_tile_build_job,
    get_pending_job_id,
    get_tile_build_job,
    refresh_pending,
    update_tile_build_job,
)
from app.services.bulk_collection_activity import wait_until_collection_bulk_idle
from app.services.storage_self_heal import log_self_heal_stats, run_storage_self_heal
from app.services.tile_build_verify import format_build_success_message, verify_mbtiles_artifact
from app.services.tile_builder import BUILD_CANCELLED, build_pmtiles_sync
from app.services.composite_tile_builder import build_composite_pmtiles_sync


def _collection_build_meta_sync(collection_id: str) -> tuple[str, list[str]]:
    """Return (collection_type, member_ids). Member ids non-empty means treat as composite."""
    import json
    from sqlalchemy import create_engine, text

    from app.services.composite_collections import member_collection_ids, parse_composite_members

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT collection_type, composite_members FROM collections WHERE id = :cid"
                ),
                {"cid": collection_id},
            ).first()
        if not row:
            return "", []
        ctype = str(row[0]) if row[0] else ""
        raw = row[1]
        if isinstance(raw, str):
            raw = json.loads(raw)
        members = member_collection_ids(parse_composite_members(raw))
        return ctype, members
    finally:
        engine.dispose()


# Tippecanoe / large composite exports can run for hours; only reclaim truly stuck jobs.
_STALE_RUNNING_TILE_BUILD_SECONDS = 6 * 60 * 60


def _job_age_seconds(job) -> float | None:
    from datetime import datetime, timezone

    ts = job.updated_at or job.created_at
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _recover_orphaned_tile_builds() -> None:
    """Fail only *stale* running tile builds with no active pending lease.

    Safe with multiple tile workers: never reclaim a job whose Redis pending
    lease is still held by the worker building it.
    """
    for job in list_all_jobs(limit=300):
        if job.status != "running":
            continue
        latest = get_latest_tile_build_job(job.collection_id)
        if latest is None or latest.job_id != job.job_id:
            continue
        pending = get_pending_job_id(job.collection_id)
        if pending == job.job_id:
            # Another (or this) worker still holds the lease — leave it alone.
            print(
                f"Skipping leased tile build {job.job_id} ({job.collection_id})",
                flush=True,
            )
            continue
        age = _job_age_seconds(job)
        if age is not None and age < _STALE_RUNNING_TILE_BUILD_SECONDS:
            print(
                f"Skipping recent running tile build {job.job_id} ({job.collection_id}) "
                f"age={int(age)}s < {_STALE_RUNNING_TILE_BUILD_SECONDS}s",
                flush=True,
            )
            continue
        msg = (
            f"Stale tile build recovered after worker restart "
            f"(running {int(age) if age is not None else '?'}s, no pending lease); "
            "re-queue with force=true"
        )
        update_tile_build_job(job.job_id, status="failed", message=msg)
        clear_pending(job.collection_id)
        print(f"Recovered orphaned tile build job {job.job_id} ({job.collection_id})", flush=True)


class _BuildHeartbeat:
    """Refresh pending lease + job updated_at while a long tippecanoe/export runs."""

    def __init__(self, collection_id: str, job_id: str, interval_seconds: float = 60.0):
        self.collection_id = collection_id
        self.job_id = job_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, name="tile-build-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not refresh_pending(self.collection_id, self.job_id):
                    print(
                        f"Pending lease lost for {self.collection_id} job {self.job_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                # Touch updated_at so multi-worker orphan recovery sees activity.
                j = get_tile_build_job(self.job_id)
                if j and j.status == "running":
                    update_tile_build_job(self.job_id, message=j.message or "Building tiles...")
            except Exception as e:
                print(f"Tile build heartbeat error: {e}", file=sys.stderr, flush=True)


def main() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("Set BULK_QUEUE_TYPE=redis for tile worker.", file=sys.stderr)
        sys.exit(1)
    _recover_orphaned_tile_builds()
    try:
        log_self_heal_stats("tile-worker", run_storage_self_heal(bulk=False, tiles=True))
    except Exception as e:
        print(f"[tile-worker] storage self-heal error: {e}", file=sys.stderr, flush=True)

    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from app.services.redis_client import make_redis_client

    brpop_timeout = 5
    r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
    last_self_heal = time.monotonic()
    self_heal_interval = max(
        60.0, float(getattr(settings, "storage_self_heal_interval_seconds", 300.0) or 300.0)
    )
    print("Tile worker started. Waiting for build jobs...", flush=True)
    while True:
        if time.monotonic() - last_self_heal >= self_heal_interval:
            try:
                log_self_heal_stats("tile-worker", run_storage_self_heal(bulk=False, tiles=True))
            except Exception as e:
                print(f"[tile-worker] storage self-heal error: {e}", file=sys.stderr, flush=True)
            last_self_heal = time.monotonic()
        try:
            result = r.brpop(TILE_BUILD_QUEUE_KEY, timeout=brpop_timeout)
        except (RedisTimeoutError, RedisConnectionError, TimeoutError, OSError) as e:
            # Idle BRPOP must not crash the process (redis socket vs BRPOP timeout race).
            print(f"Redis BRPOP transient error (will retry): {e}", file=sys.stderr, flush=True)
            try:
                r = make_redis_client(for_brpop=True, brpop_timeout=brpop_timeout)
            except Exception as reconnect_err:
                print(f"Redis reconnect failed: {reconnect_err}", file=sys.stderr, flush=True)
            continue
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
        update_tile_build_job(job_id, status="building", message="Building tiles...")
        refresh_pending(cid, job_id)

        def stop_check() -> bool:
            j = get_tile_build_job(job_id)
            return j is not None and j.status == "cancelled"

        try:
            with _BuildHeartbeat(cid, job_id):
                if not wait_until_collection_bulk_idle(
                    cid,
                    stop_check=stop_check,
                    on_waiting_message=lambda: update_tile_build_job(
                        job_id, message="Waiting for bulk import to finish..."
                    ),
                ):
                    print(
                        f"Tile build for {cid} cancelled while waiting for bulk import (job_id={job_id})",
                        flush=True,
                    )
                    clear_pending(cid)
                    continue

                ctype, member_ids = _collection_build_meta_sync(cid)
                use_composite = bool(payload.is_composite) or ctype == "composite" or bool(member_ids)
                if use_composite:
                    if not member_ids:
                        err = (
                            f"Composite {cid} has no members (collection_type={ctype!r}); "
                            "add members before building tiles"
                        )
                    else:
                        update_tile_build_job(
                            job_id,
                            message=f"Building composite tiles ({len(member_ids)} members)...",
                        )
                        print(
                            f"Composite build for {cid}: {len(member_ids)} members "
                            f"(type={ctype!r}, payload.is_composite={payload.is_composite})",
                            flush=True,
                        )
                        err = build_composite_pmtiles_sync(
                            cid,
                            member_ids,
                            options=payload.options,
                            stop_check=stop_check,
                        )
                else:
                    err = build_pmtiles_sync(cid, options=payload.options, stop_check=stop_check)
        except Exception as e:
            clear_pending(cid)
            msg = f"Tile worker crash during build: {e}"
            update_tile_build_job(job_id, status="failed", message=msg)
            print(f"Build FAILED for {cid}: {msg}", file=sys.stderr, flush=True)
            continue

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
            continue

        out_path = os.path.join(settings.tiles_storage_path, f"{cid}.mbtiles")
        verify_err = verify_mbtiles_artifact(out_path)
        if verify_err:
            update_tile_build_job(job_id, status="failed", message=verify_err)
            print(f"Build FAILED for {cid}: {verify_err}", file=sys.stderr, flush=True)
            continue

        ok_msg = format_build_success_message(out_path)
        update_tile_build_job(job_id, status="completed", message=ok_msg)
        invalidate_collection_cache(cid)
        print(f"Build completed for {cid} (job_id={job_id}): {ok_msg}", flush=True)


if __name__ == "__main__":
    main()
