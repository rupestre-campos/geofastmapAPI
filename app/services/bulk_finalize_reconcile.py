"""Bulk reconcile stuck finalize jobs and orphan partitions (no per-job SQL)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.db.features_partitions import (
    _partition_is_attached_conn,
    _safe_partition_name,
    _table_exists_conn,
    cleanup_detached_orphan_feature_partitions_sync,
    list_attached_feature_partitions_sync,
    partition_swap_already_complete_sync,
    resolve_features_partition_relname_sync,
)
from app.services.bulk_collection_activity import holds_collection_bulk_mutex
from app.services.bulk_copy_ingest import _finalize_after_promote
from app.services.bulk_finalize_queue import (
    BulkFinalizePayload,
    clear_finalize_pending,
    enqueue_finalize,
    get_finalize_state,
    is_finalize_pending,
)
from app.services.bulk_queue import unregister_bulk_import_job
from app.services.bulk_staging import (
    STAGING_TABLE_PREFIX,
    drop_staging_table_sync,
    promote_staging_sync,
    staging_row_count_sync,
    staging_table_exists_sync,
    staging_table_name,
)
from app.services.bulk_staging_recovery import (
    StagingDuplicateUnrecoverableError,
    abandon_staging_finalize_job,
    is_staging_pk_duplicate_error,
    prepare_staging_for_promote_sync,
)
from app.services.job_store import list_all_jobs, update_job


@dataclass
class ReconcileStats:
    orphans_dropped: int = 0
    completed_already_swapped: int = 0
    promoted: int = 0
    enqueued: int = 0
    empty_closed: int = 0
    abandoned: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"orphans_dropped={self.orphans_dropped} "
            f"completed_already_swapped={self.completed_already_swapped} "
            f"promoted={self.promoted} enqueued={self.enqueued} "
            f"empty_closed={self.empty_closed} abandoned={self.abandoned} "
            f"skipped={self.skipped} errors={len(self.errors)}"
        )


def _infer_import_mode(job) -> str:
    msg = (job.message or "").lower()
    if (job.status or "") == "replacing" or "replace" in msg:
        return "replace"
    state = get_finalize_state(job.job_id)
    if state.get("mode") in ("replace", "append"):
        return str(state["mode"])
    return "append"


def _job_last_activity(job) -> datetime | None:
    for ts in (job.last_progress_at, job.updated_at, job.created_at):
        if ts is None:
            continue
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _job_activity_age_seconds(job) -> float:
    last = _job_last_activity(job)
    if last is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - last).total_seconds())


def _load_still_in_progress(job) -> bool:
    """
    True when a load worker is likely still COPY-ing into staging.
    Reconcile must not touch these jobs (empty staging is normal early on).
    """
    status = (job.status or "").lower()
    if status in ("pending", "running", "replacing"):
        return True
    if holds_collection_bulk_mutex(job.collection_id, job.job_id):
        return True
    min_age = max(
        60.0,
        float(getattr(get_settings(), "bulk_running_recover_staging_seconds", 600.0) or 600.0),
    )
    if status == "finalizing" and (job.items_created or 0) <= 0 and _job_activity_age_seconds(job) < min_age:
        return True
    return False


def _is_reconcile_candidate(job) -> bool:
    status = (job.status or "").lower()
    if status not in ("finalizing", "failed", "running", "replacing"):
        return False
    if _load_still_in_progress(job):
        return False
    if status == "finalizing":
        return True
    if (job.items_created or 0) > 0:
        return True
    msg = (job.message or "").lower()
    if any(
        k in msg
        for k in (
            "partition swap",
            "overlap",
            "deadlock",
            "watchdog",
            "finalize",
            "bulk import stalled",
            "duplicate key",
            "uniqueviolation",
        )
    ):
        return True
    return False


def _duplicate_give_up_attempts() -> int:
    return max(1, int(getattr(get_settings(), "bulk_finalize_duplicate_give_up_attempts", 3) or 3))


def _should_abandon_duplicate_job(job) -> bool:
    """Jobs stuck retrying duplicate-key finalize errors should be abandoned, not re-queued forever."""
    if not is_staging_pk_duplicate_error(job.message or ""):
        return False
    state = get_finalize_state(job.job_id)
    attempt = int(state.get("attempt") or 0)
    if attempt >= _duplicate_give_up_attempts():
        return True
    # Message already shows very high attempt counts from finalize worker retries.
    msg = job.message or ""
    if "attempt " in msg.lower():
        import re

        m = re.search(r"attempt\s+(\d+)", msg, re.IGNORECASE)
        if m and int(m.group(1)) >= _duplicate_give_up_attempts():
            return True
    return False


def _abandon_duplicate_job(engine: Engine, job, staged: int, *, stats: ReconcileStats, reason: str) -> None:
    abandon_staging_finalize_job(
        engine,
        job_id=job.job_id,
        collection_id=job.collection_id,
        reason=reason,
        items_created=staged or job.items_created or 0,
        items_failed=job.items_failed,
    )
    stats.abandoned += 1
    print(f"[reconcile] abandoned job_id={job.job_id} collection={job.collection_id}", flush=True)


def _complete_swapped_job(
    engine: Engine,
    job,
    row_count: int,
    *,
    dry_run: bool,
    note: str,
    stats: ReconcileStats,
) -> None:
    if dry_run:
        stats.completed_already_swapped += 1
        print(f"[reconcile] would complete job_id={job.job_id} {note} rows={row_count}", flush=True)
        return
    _finalize_after_promote(engine, job.collection_id)
    update_job(
        job.job_id,
        status="completed",
        message=f"Recovered: {note} ({row_count:,} features).",
        items_created=row_count,
        items_failed=job.items_failed,
    )
    clear_finalize_pending(job.job_id)
    unregister_bulk_import_job(job.job_id)
    stats.completed_already_swapped += 1
    print(f"[reconcile] completed job_id={job.job_id} collection={job.collection_id} rows={row_count}", flush=True)


def _reconcile_one_job(engine: Engine, job, *, dry_run: bool, stats: ReconcileStats) -> None:
    staging = staging_table_name(job.job_id)
    exists = staging_table_exists_sync(engine, job.job_id)
    if not exists:
        live = resolve_features_partition_relname_sync(engine, job.collection_id)
        if live and live.startswith(STAGING_TABLE_PREFIX) and (job.status or "").lower() == "finalizing":
            try:
                with engine.connect() as conn:
                    count = int(
                        conn.execute(text(f'SELECT COUNT(*) FROM "{live}"')).scalar() or 0
                    )
            except Exception:
                count = job.items_created or 0
            _complete_swapped_job(
                engine,
                job,
                count or job.items_created or 0,
                dry_run=dry_run,
                note="live partition already attached",
                stats=stats,
            )
        else:
            stats.skipped += 1
        return

    try:
        staged = staging_row_count_sync(engine, job.job_id)
    except Exception as e:
        stats.errors.append((job.job_id, str(e)))
        return

    if partition_swap_already_complete_sync(engine, job.collection_id, staging):
        _complete_swapped_job(
            engine,
            job,
            staged or job.items_created or 0,
            dry_run=dry_run,
            note="staging partition already attached",
            stats=stats,
        )
        return

    if staged <= 0:
        if dry_run:
            stats.empty_closed += 1
            return
        if _load_still_in_progress(job):
            stats.skipped += 1
            return
        live = resolve_features_partition_relname_sync(engine, job.collection_id)
        if live and not live.startswith(STAGING_TABLE_PREFIX):
            try:
                with engine.connect() as conn:
                    count = int(
                        conn.execute(text(f'SELECT COUNT(*) FROM "{live}"')).scalar() or 0
                    )
            except Exception:
                count = job.items_created or 0
            if count > 0 or (job.items_created or 0) > 0:
                _complete_swapped_job(
                    engine,
                    job,
                    count or job.items_created or 0,
                    dry_run=False,
                    note="live partition already has data (staging dropped)",
                    stats=stats,
                )
                return
        try:
            drop_staging_table_sync(engine, job.job_id)
        except Exception:
            pass
        if (job.items_created or 0) > 0:
            fail_msg = (
                "Bulk import lost staged data before partition swap could complete. "
                "Please re-import this file."
            )
        else:
            fail_msg = (
                "Bulk import produced no features (empty staging table). "
                "Check the source file and re-import."
            )
        update_job(
            job.job_id,
            status="failed",
            message=fail_msg,
            items_created=0,
            items_failed=job.items_failed,
        )
        clear_finalize_pending(job.job_id)
        unregister_bulk_import_job(job.job_id)
        stats.empty_closed += 1
        return

    mode = _infer_import_mode(job)
    if dry_run:
        if _should_abandon_duplicate_job(job):
            stats.abandoned += 1
            print(
                f"[reconcile] would abandon duplicate-key job_id={job.job_id} rows={staged}",
                flush=True,
            )
            return
        stats.enqueued += 1
        print(
            f"[reconcile] would promote job_id={job.job_id} collection={job.collection_id} "
            f"mode={mode} rows={staged}",
            flush=True,
        )
        return

    if _should_abandon_duplicate_job(job):
        try:
            prepare_staging_for_promote_sync(engine, job.job_id)
        except StagingDuplicateUnrecoverableError as e:
            _abandon_duplicate_job(engine, job, staged, stats=stats, reason=str(e))
            return

    try:
        prepare_staging_for_promote_sync(engine, job.job_id)
    except StagingDuplicateUnrecoverableError as e:
        _abandon_duplicate_job(engine, job, staged, stats=stats, reason=str(e))
        return

    try:
        count = promote_staging_sync(
            engine,
            collection_id=job.collection_id,
            job_id=job.job_id,
            mode=mode,
        )
        _finalize_after_promote(engine, job.collection_id)
        update_job(
            job.job_id,
            status="completed",
            message=f"Recovered: promoted {count:,} staged features (bulk reconcile).",
            items_created=count,
            items_failed=job.items_failed,
        )
        clear_finalize_pending(job.job_id)
        unregister_bulk_import_job(job.job_id)
        stats.promoted += 1
        print(
            f"[reconcile] promoted job_id={job.job_id} collection={job.collection_id} rows={count}",
            flush=True,
        )
    except StagingDuplicateUnrecoverableError as e:
        _abandon_duplicate_job(engine, job, staged, stats=stats, reason=str(e))
    except Exception as e:
        err = str(e)
        if is_staging_pk_duplicate_error(e):
            try:
                prepare_staging_for_promote_sync(engine, job.job_id)
                count = promote_staging_sync(
                    engine,
                    collection_id=job.collection_id,
                    job_id=job.job_id,
                    mode=mode,
                )
                _finalize_after_promote(engine, job.collection_id)
                update_job(
                    job.job_id,
                    status="completed",
                    message=f"Recovered: deduped and promoted {count:,} staged features (bulk reconcile).",
                    items_created=count,
                    items_failed=job.items_failed,
                )
                clear_finalize_pending(job.job_id)
                unregister_bulk_import_job(job.job_id)
                stats.promoted += 1
                print(
                    f"[reconcile] promoted after dedupe job_id={job.job_id} collection={job.collection_id} rows={count}",
                    flush=True,
                )
                return
            except StagingDuplicateUnrecoverableError as dup_err:
                _abandon_duplicate_job(engine, job, staged, stats=stats, reason=str(dup_err))
                return
        stats.errors.append((job.job_id, err))
        payload = BulkFinalizePayload(
            job_id=job.job_id,
            collection_id=job.collection_id,
            mode=mode,
            items_created=staged,
            items_failed=job.items_failed,
            owner_id=job.owner_id,
        )
        if not is_finalize_pending(job.job_id):
            enqueue_finalize(payload, force=True)
            stats.enqueued += 1
        update_job(
            job.job_id,
            status="finalizing",
            message=f"Reconcile: queued partition swap for {staged:,} features after error: {err[:200]}",
            items_created=staged,
        )
        print(f"[reconcile] enqueued job_id={job.job_id} after error: {err[:120]}", flush=True)


def _reconcile_jobs_by_staging_table(engine: Engine, *, dry_run: bool, stats: ReconcileStats) -> None:
    """Match attached bulk_staging_* partitions to Redis jobs and complete them."""
    job_by_staging: dict[str, object] = {}
    for job in list_all_jobs(limit=5000):
        job_by_staging[staging_table_name(job.job_id)] = job

    for relname, collection_id in list_attached_feature_partitions_sync(engine):
        if not relname.startswith(STAGING_TABLE_PREFIX):
            continue
        job = job_by_staging.get(relname)
        if not job:
            continue
        if (job.status or "").lower() == "completed":
            continue
        if not _is_reconcile_candidate(job):
            continue
        _reconcile_one_job(engine, job, dry_run=dry_run, stats=stats)


def reconcile_all_stuck_finalize_jobs(
    *,
    dry_run: bool = False,
    limit: int = 5000,
) -> ReconcileStats:
    """
    One-shot fleet reconcile: drop orphan partitions, complete already-swapped jobs,
    promote or re-queue the rest. Safe to run repeatedly.
    """
    settings = get_settings()
    stats = ReconcileStats()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        if not dry_run:
            dropped = cleanup_detached_orphan_feature_partitions_sync(engine)
            stats.orphans_dropped = len(dropped)
            for name in dropped:
                print(f"[reconcile] dropped orphan partition {name}", flush=True)
        else:
            would_drop: list[str] = []
            with engine.connect() as conn:
                for relname, collection_id in list_attached_feature_partitions_sync(engine):
                    canonical = _safe_partition_name(collection_id)
                    if canonical != relname and _table_exists_conn(conn, canonical):
                        if not _partition_is_attached_conn(conn, canonical):
                            would_drop.append(canonical)
            stats.orphans_dropped = len(would_drop)
            for name in would_drop:
                print(f"[reconcile] would drop orphan partition {name}", flush=True)

        seen: set[str] = set()
        for job in list_all_jobs(limit=limit):
            if not _is_reconcile_candidate(job):
                continue
            seen.add(job.job_id)
            _reconcile_one_job(engine, job, dry_run=dry_run, stats=stats)

        _reconcile_jobs_by_staging_table(engine, dry_run=dry_run, stats=stats)

    finally:
        engine.dispose()

    print(f"[reconcile] done {stats.summary()}", flush=True)
    return stats
