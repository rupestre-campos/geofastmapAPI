"""Bulk reconcile stuck finalize jobs and orphan partitions (no per-job SQL)."""

from __future__ import annotations

from dataclasses import dataclass, field

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
from app.services.job_store import list_all_jobs, update_job


@dataclass
class ReconcileStats:
    orphans_dropped: int = 0
    completed_already_swapped: int = 0
    promoted: int = 0
    enqueued: int = 0
    empty_closed: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"orphans_dropped={self.orphans_dropped} "
            f"completed_already_swapped={self.completed_already_swapped} "
            f"promoted={self.promoted} enqueued={self.enqueued} "
            f"empty_closed={self.empty_closed} skipped={self.skipped} "
            f"errors={len(self.errors)}"
        )


def _infer_import_mode(job) -> str:
    msg = (job.message or "").lower()
    if (job.status or "") == "replacing" or "replace" in msg:
        return "replace"
    state = get_finalize_state(job.job_id)
    if state.get("mode") in ("replace", "append"):
        return str(state["mode"])
    return "append"


def _is_reconcile_candidate(job) -> bool:
    status = (job.status or "").lower()
    if status not in ("finalizing", "failed", "running", "replacing"):
        return False
    msg = (job.message or "").lower()
    if status == "finalizing":
        return True
    if (job.items_created or 0) > 0:
        return True
    if any(
        k in msg
        for k in (
            "partition swap",
            "staging",
            "overlap",
            "deadlock",
            "watchdog",
            "finalize",
            "bulk import stalled",
        )
    ):
        return True
    return False


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
        try:
            drop_staging_table_sync(engine, job.job_id)
        except Exception:
            pass
        update_job(
            job.job_id,
            status="failed",
            message="Reconcile: empty staging table; nothing to promote.",
        )
        clear_finalize_pending(job.job_id)
        stats.empty_closed += 1
        return

    mode = _infer_import_mode(job)
    if dry_run:
        stats.enqueued += 1
        print(
            f"[reconcile] would promote job_id={job.job_id} collection={job.collection_id} "
            f"mode={mode} rows={staged}",
            flush=True,
        )
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
    except Exception as e:
        err = str(e)
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
