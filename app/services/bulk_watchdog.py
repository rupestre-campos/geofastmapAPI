"""Watchdog: reclaim stale bulk mutexes; recover or fail stuck jobs."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.bulk_collection_activity import (
    BULK_COLLECTION_MUTEX_PREFIX,
    _job_age_seconds,
    _pending_stale_seconds,
    get_collection_bulk_mutex_holder,
    holds_collection_bulk_mutex,
    is_terminal_job_status,
    reclaim_stale_collection_bulk_mutex,
    release_collection_bulk_mutex,
)
from app.services.bulk_staging import cleanup_orphan_staging_tables_sync, drop_staging_table_sync, staging_row_count_sync
from app.services.job_store import get_job, list_all_jobs, update_job
from app.services.redis_resilience import run_redis_retry


def _stale_seconds() -> float:
    return max(60.0, float(getattr(get_settings(), "bulk_job_stale_seconds", 14400.0) or 14400.0))


def _finalize_stale_seconds() -> float:
    return max(
        300.0,
        float(getattr(get_settings(), "bulk_finalize_stale_seconds", 86400.0) or 86400.0),
    )


def _should_fail_stale_running() -> bool:
    settings = get_settings()
    explicit = getattr(settings, "bulk_watchdog_fail_stale_running", None)
    if explicit is not None:
        return bool(explicit)
    return not bool(getattr(settings, "bulk_finalize_queue_enabled", True))


def _job_last_progress(job) -> datetime | None:
    if job.last_progress_at is not None:
        return job.last_progress_at
    return job.updated_at


def _running_recover_min_age_seconds() -> float:
    return max(
        300.0,
        float(getattr(get_settings(), "bulk_running_recover_staging_seconds", 600.0) or 600.0),
    )


def _should_attempt_running_staging_recovery(job) -> bool:
    """Avoid finalizing while the load worker likely still holds the collection mutex."""
    last = _job_last_progress(job)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age < _running_recover_min_age_seconds():
        return False
    if holds_collection_bulk_mutex(job.collection_id, job.job_id) and age < _stale_seconds():
        return False
    return True


def _recover_running_job_to_finalize(engine, job) -> bool:
    """
    COPY may finish while Redis progress heartbeats were flaky (items_created still 0 in Redis).
    Promote to finalizing instead of failing and dropping staging.
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis" or not bool(
        getattr(settings, "bulk_finalize_queue_enabled", True)
    ):
        return False
    try:
        staged = staging_row_count_sync(engine, job.job_id)
    except Exception:
        staged = 0
    if staged <= 0:
        return False

    from app.services.bulk_finalize_queue import BulkFinalizePayload, enqueue_finalize, is_finalize_pending

    msg = (job.message or "").lower()
    mode = "replace" if "replace" in msg or (job.status or "") == "replacing" else "append"
    if is_finalize_pending(job.job_id):
        update_job(
            job.job_id,
            status="finalizing",
            message=f"Loaded {staged:,} features into staging; partition swap queued…",
            items_created=staged,
        )
        print(
            f"[bulk-watchdog] recovered running→finalizing (already queued) job_id={job.job_id} rows={staged}",
            flush=True,
        )
        return True

    payload = BulkFinalizePayload(
        job_id=job.job_id,
        collection_id=job.collection_id,
        mode=mode,
        items_created=staged,
        items_failed=job.items_failed,
        owner_id=job.owner_id,
    )
    if not enqueue_finalize(payload):
        return False
    update_job(
        job.job_id,
        status="finalizing",
        message=f"Recovered: {staged:,} features in staging; partition swap queued (watchdog).",
        items_created=staged,
    )
    release_collection_bulk_mutex(job.collection_id, job.job_id)
    print(
        f"[bulk-watchdog] recovered running→finalizing job_id={job.job_id} collection={job.collection_id} rows={staged}",
        flush=True,
    )
    return True


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
    Skips jobs with staged rows (recovers to finalizing instead).
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return []
    if not _should_fail_stale_running():
        return []

    threshold = _stale_seconds()
    now = datetime.now(timezone.utc)
    failed: list[str] = []
    active_ids: set[str] = set()

    from sqlalchemy import create_engine

    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
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
            if _should_attempt_running_staging_recovery(job) and _recover_running_job_to_finalize(engine, job):
                active_ids.add(job.job_id)
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
                drop_staging_table_sync(engine, job.job_id)
            except Exception:
                pass
            failed.append(job.job_id)
            print(
                f"[bulk-watchdog] failed stale job_id={job.job_id} collection={job.collection_id} age={int(age)}s",
                flush=True,
            )

        try:
            cleanup_orphan_staging_tables_sync(engine, active_job_ids=active_ids)
        except Exception:
            pass
    finally:
        engine.dispose()

    return failed


def recover_stale_running_jobs_with_staging() -> list[str]:
    """
    Even when fail_stale_running is disabled, scan running/replacing jobs with staging data
    and queue finalize (worker died after COPY, or Redis never recorded progress).
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis" or not bool(
        getattr(settings, "bulk_finalize_queue_enabled", True)
    ):
        return []
    from sqlalchemy import create_engine

    recovered: list[str] = []
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        for job in list_all_jobs(limit=500):
            if job.status not in ("running", "replacing"):
                continue
            if not _should_attempt_running_staging_recovery(job):
                continue
            if _recover_running_job_to_finalize(engine, job):
                recovered.append(job.job_id)
    finally:
        engine.dispose()
    return recovered


def fail_stale_finalizing_jobs() -> list[str]:
    """Fail finalizing jobs stuck longer than bulk_finalize_stale_seconds (no staging recovery)."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return []
    threshold = _finalize_stale_seconds()
    now = datetime.now(timezone.utc)
    failed: list[str] = []

    for job in list_all_jobs(limit=500):
        if (job.status or "").lower() != "finalizing":
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
                message=(
                    f"Partition swap stalled (no progress for {int(age)}s). "
                    "Staging data kept; re-run bulk_recover_staging or re-upload."
                ),
            )
        except Exception:
            continue
        failed.append(job.job_id)
        print(
            f"[bulk-finalize-watchdog] failed stale finalizing job_id={job.job_id} age={int(age)}s",
            flush=True,
        )
    return failed


def run_bulk_watchdog_pass(*, fail_stale_running: bool | None = None) -> None:
    """Reclaim terminal-holder mutexes; optionally fail stale running jobs (off by default with finalize queue)."""
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
    recover_stale_running_jobs_with_staging()
    if fail_stale_running if fail_stale_running is not None else _should_fail_stale_running():
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

    requeued: list[str] = []
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        for job in list_all_jobs(limit=500):
            status = (job.status or "").lower()
            if status not in ("finalizing", "failed", "running", "replacing"):
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
            if status == "failed" and not state and "deadlock" not in msg and "partition swap" not in msg and "staging" not in msg and "watchdog" not in msg:
                continue
            mode = state.get("mode") or ("replace" if "replace" in msg or status == "replacing" else "append")
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
    """Re-queue finalize work for orphaned staging; fail only very stale finalizing jobs."""
    reconcile_orphan_finalize_jobs()
    fail_stale_finalizing_jobs()
