"""Watchdog: reclaim stale bulk mutexes and fail jobs with no progress heartbeat."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.bulk_collection_activity import (
    BULK_COLLECTION_MUTEX_PREFIX,
    _job_age_seconds,
    _pending_stale_seconds,
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


def fail_stale_pending_mutex_holders() -> list[str]:
    """
    Fail pending jobs that still hold the collection mutex (worker died before running).
    Releases the mutex so other imports on that collection can proceed.
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return []
    import redis

    threshold = _pending_stale_seconds()
    failed: list[str] = []

    def _scan_mutex_keys() -> list[str]:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return list(r.scan_iter(match=f"{BULK_COLLECTION_MUTEX_PREFIX}*", count=200))

    try:
        keys = run_redis_retry("bulk_watchdog_pending_mutex_scan", _scan_mutex_keys)
    except Exception:
        keys = []

    prefix_len = len(BULK_COLLECTION_MUTEX_PREFIX)
    for key in keys:
        collection_id = key[prefix_len:]
        if not collection_id:
            continue
        holder = get_collection_bulk_mutex_holder(collection_id)
        if not holder:
            continue
        job = get_job(holder)
        if not job or (job.status or "").lower() != "pending":
            continue
        age = _job_age_seconds(job)
        if age < threshold:
            continue
        release_collection_bulk_mutex(collection_id, holder)
        try:
            update_job(
                holder,
                status="failed",
                message=(
                    f"Bulk import interrupted before start (pending {int(age)}s with collection lock); "
                    "re-submit the upload."
                ),
            )
        except Exception:
            continue
        failed.append(holder)
        print(
            f"[bulk-watchdog] failed pending mutex holder job_id={holder} "
            f"collection={collection_id} age={int(age)}s",
            flush=True,
        )
    return failed


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
        if job.status in ("pending", "running", "replacing", "finalizing"):
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
    fail_stale_pending_mutex_holders()
    fail_stale_running_jobs()


def reconcile_orphan_finalize_jobs() -> list[str]:
    """
    Self-heal: jobs stuck in finalizing (or failed after load) with staging rows but no queue entry.
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis" or not bool(
        getattr(settings, "bulk_finalize_queue_enabled", True)
    ):
        return []
    from sqlalchemy import create_engine

    from app.services.bulk_finalize_queue import BulkFinalizePayload, enqueue_finalize, get_finalize_state, is_finalize_pending
    from app.services.bulk_staging import staging_row_count_sync

    requeued: list[str] = []
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        for job in list_all_jobs(limit=500):
            status = (job.status or "").lower()
            if status not in ("finalizing", "failed"):
                continue
            if is_finalize_pending(job.job_id):
                continue
            try:
                staged = staging_row_count_sync(engine, job.job_id)
            except Exception:
                staged = 0
            if staged <= 0:
                continue
            state = get_finalize_state(job.job_id)
            msg = (job.message or "").lower()
            if status == "failed" and not state and "deadlock" not in msg and "partition swap" not in msg and "staging" not in msg:
                continue
            mode = state.get("mode") or ("replace" if "replace" in msg else "append")
            owner_raw = state.get("owner_id") or ""
            owner_id = int(owner_raw) if owner_raw.isdigit() else job.owner_id
            payload = BulkFinalizePayload(
                job_id=job.job_id,
                collection_id=state.get("collection_id") or job.collection_id,
                mode=mode,
                items_created=int(state.get("items_created") or job.items_created or staged),
                items_failed=int(state.get("items_failed") or job.items_failed or 0),
                owner_id=owner_id,
                queue_compute_tiles=str(state.get("queue_compute_tiles", "0")).lower() in ("1", "true", "yes"),
            )
            if enqueue_finalize(payload, force=True):
                update_job(
                    job.job_id,
                    status="finalizing",
                    message=f"Re-queued partition swap for {staged:,} staged features (self-heal).",
                    items_created=job.items_created or staged,
                )
                requeued.append(job.job_id)
                print(
                    f"[bulk-finalize-watchdog] requeued job_id={job.job_id} collection={job.collection_id} rows={staged}",
                    flush=True,
                )
    finally:
        engine.dispose()
    return requeued


def run_finalize_watchdog_pass() -> None:
    """Re-queue finalize work for orphaned staging tables."""
    reconcile_orphan_finalize_jobs()
