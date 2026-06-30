"""Watchdog: reclaim stale bulk mutexes and fail jobs with no progress heartbeat."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.bulk_collection_activity import (
    BULK_COLLECTION_MUTEX_PREFIX,
    get_collection_bulk_mutex_holder,
    is_terminal_job_status,
    reclaim_stale_collection_bulk_mutex,
    release_collection_bulk_mutex,
)
from app.services.bulk_staging import cleanup_orphan_staging_tables_sync, drop_staging_table_sync
from app.services.job_store import get_job, list_all_jobs, update_job
from app.services.redis_resilience import run_redis_retry


def _stale_seconds() -> float:
    return max(60.0, float(getattr(get_settings(), "bulk_job_stale_seconds", 3600.0) or 3600.0))


def _job_last_progress(job) -> datetime | None:
    if job.last_progress_at is not None:
        return job.last_progress_at
    return job.updated_at


def fail_stale_running_jobs() -> list[str]:
    """
    Mark running/replacing jobs without recent progress as failed and reclaim their mutex.
    Returns list of failed job ids.
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return []
    threshold = _stale_seconds()
    now = datetime.now(timezone.utc)
    failed: list[str] = []
    active_ids: set[str] = set()

    for job in list_all_jobs(limit=500):
        if job.status in ("pending", "running", "replacing"):
            active_ids.add(job.job_id)
        if job.status not in ("running", "replacing"):
            continue
        last = _job_last_progress(job)
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (now - last).total_seconds()
        if age < threshold:
            continue
        try:
            update_job(
                job.job_id,
                status="failed",
                message=f"Bulk import stalled (no progress for {int(age)}s); job marked failed by watchdog.",
            )
        except Exception:
            continue
        release_collection_bulk_mutex(job.collection_id, job.job_id)
        try:
            from sqlalchemy import create_engine

            engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
            try:
                drop_staging_table_sync(engine, job.job_id)
            finally:
                engine.dispose()
        except Exception:
            pass
        failed.append(job.job_id)
        print(
            f"[bulk-watchdog] failed stale job_id={job.job_id} collection={job.collection_id} age={int(age)}s",
            flush=True,
        )

    try:
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        try:
            cleanup_orphan_staging_tables_sync(engine, active_job_ids=active_ids)
        finally:
            engine.dispose()
    except Exception:
        pass

    return failed


def run_bulk_watchdog_pass() -> None:
    """Reclaim terminal-holder mutexes and fail stale running jobs."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    def _scan_mutex_keys() -> list[str]:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return list(r.scan_iter(match=f"{BULK_COLLECTION_MUTEX_PREFIX}*", count=200))

    try:
        keys = run_redis_retry("bulk_watchdog_mutex_scan", _scan_mutex_keys)
    except Exception:
        keys = []
    prefix_len = len(BULK_COLLECTION_MUTEX_PREFIX)
    for key in keys:
        collection_id = key[prefix_len:]
        holder = get_collection_bulk_mutex_holder(collection_id)
        if not holder:
            continue
        job = get_job(holder)
        if job is None or is_terminal_job_status(job.status) or job.finished_at is not None:
            reclaimed = reclaim_stale_collection_bulk_mutex(collection_id)
            if reclaimed:
                print(
                    f"[bulk-watchdog] reclaimed mutex collection={collection_id} holder={reclaimed}",
                    flush=True,
                )
    fail_stale_running_jobs()
