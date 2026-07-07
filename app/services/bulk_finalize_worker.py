"""Process bulk finalize jobs: idempotent staging promote (single-consumer queue)."""

from __future__ import annotations

import sys
import time

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.services.bulk_staging_recovery import (
    StagingDuplicateUnrecoverableError,
    abandon_staging_finalize_job,
    is_staging_pk_duplicate_error,
    prepare_staging_for_promote_sync,
)
from app.services.bulk_copy_ingest import _finalize_after_promote
from app.services.bulk_finalize_queue import (
    BulkFinalizePayload,
    clear_finalize_pending,
    enqueue_finalize,
    record_finalize_error,
)
from app.services.bulk_import import BulkImportCancelled
from app.services.bulk_queue import unregister_bulk_import_job
from app.services.bulk_staging import (
    drop_staging_table_sync,
    promote_staging_sync,
    staging_row_count_sync,
    staging_table_name,
)
from app.services.job_store import get_job, update_job
from app.services.redis_resilience import retry_wait_seconds


def _finalize_backoff_seconds(attempt: int) -> float:
    settings = get_settings()
    base = max(1.0, float(getattr(settings, "bulk_finalize_retry_base_seconds", 2.0) or 2.0))
    cap = max(base, float(getattr(settings, "bulk_finalize_retry_max_seconds", 120.0) or 120.0))
    return retry_wait_seconds(max(1, attempt), base=base, max_seconds=cap)


def _duplicate_give_up_attempts() -> int:
    return max(1, int(getattr(get_settings(), "bulk_finalize_duplicate_give_up_attempts", 3) or 3))


def process_bulk_finalize_job(payload: BulkFinalizePayload) -> None:
    """Promote staged rows into the live partition. Idempotent; safe to retry."""
    job = get_job(payload.job_id)
    if job and job.status == "cancelled":
        engine = create_engine(get_settings().database_sync_url, pool_pre_ping=True, future=True)
        try:
            drop_staging_table_sync(engine, payload.job_id)
        finally:
            engine.dispose()
        clear_finalize_pending(payload.job_id)
        unregister_bulk_import_job(payload.job_id)
        return

    staging = staging_table_name(payload.job_id)
    print(
        f"[bulk-finalize] start job_id={payload.job_id} collection={payload.collection_id} "
        f"mode={payload.mode} attempt={payload.attempt} staging={staging}",
        flush=True,
    )

    engine = create_engine(get_settings().database_sync_url, pool_pre_ping=True, future=True)
    try:
        staged = staging_row_count_sync(engine, payload.job_id)
        if staged <= 0:
            # Already promoted or never loaded.
            live_msg = "Finalize skipped: staging table empty (already promoted?)."
            print(f"[bulk-finalize] {live_msg} job_id={payload.job_id}", flush=True)
            update_job(
                payload.job_id,
                status="completed",
                message=live_msg,
                items_created=payload.items_created,
                items_failed=payload.items_failed,
            )
            clear_finalize_pending(payload.job_id)
            unregister_bulk_import_job(payload.job_id)
            return

        update_job(
            payload.job_id,
            status="finalizing",
            message=f"Swapping {staged:,} staged features into live partition…",
            items_created=payload.items_created or staged,
            items_failed=payload.items_failed,
        )

        try:
            prepare_staging_for_promote_sync(engine, payload.job_id)
        except StagingDuplicateUnrecoverableError as e:
            abandon_staging_finalize_job(
                engine,
                job_id=payload.job_id,
                collection_id=payload.collection_id,
                reason=str(e),
                items_created=payload.items_created or staged,
                items_failed=payload.items_failed,
            )
            return

        count = promote_staging_sync(
            engine,
            collection_id=payload.collection_id,
            job_id=payload.job_id,
            mode=payload.mode,
        )
        _finalize_after_promote(engine, payload.collection_id)

        update_job(
            payload.job_id,
            status="completed",
            message=(
                f"Imported {count or payload.items_created or staged:,} features."
                + (f" {payload.items_failed} failed." if payload.items_failed else "")
            ),
            items_created=count or payload.items_created or staged,
            items_failed=payload.items_failed,
        )
        clear_finalize_pending(payload.job_id)
        unregister_bulk_import_job(payload.job_id)

        from app.services.bulk_worker import _queue_tile_build_if_requested

        _queue_tile_build_if_requested(
            payload.collection_id,
            payload.owner_id,
            payload.queue_compute_tiles,
        )
        print(
            f"[bulk-finalize] completed job_id={payload.job_id} collection={payload.collection_id} rows={count}",
            flush=True,
        )
    except BulkImportCancelled:
        try:
            drop_staging_table_sync(engine, payload.job_id)
        except Exception:
            pass
        clear_finalize_pending(payload.job_id)
        unregister_bulk_import_job(payload.job_id)
    except StagingDuplicateUnrecoverableError as e:
        abandon_staging_finalize_job(
            engine,
            job_id=payload.job_id,
            collection_id=payload.collection_id,
            reason=str(e),
            items_created=payload.items_created,
            items_failed=payload.items_failed,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if is_staging_pk_duplicate_error(e):
            try:
                prepare_staging_for_promote_sync(engine, payload.job_id)
            except StagingDuplicateUnrecoverableError as dup_err:
                abandon_staging_finalize_job(
                    engine,
                    job_id=payload.job_id,
                    collection_id=payload.collection_id,
                    reason=str(dup_err),
                    items_created=payload.items_created,
                    items_failed=payload.items_failed,
                )
                return
            if payload.attempt >= _duplicate_give_up_attempts():
                abandon_staging_finalize_job(
                    engine,
                    job_id=payload.job_id,
                    collection_id=payload.collection_id,
                    reason=(
                        f"Duplicate staging primary keys persisted after "
                        f"{payload.attempt + 1} finalize attempts. {err[:200]}"
                    ),
                    items_created=payload.items_created,
                    items_failed=payload.items_failed,
                )
                return
        print(
            f"[bulk-finalize] failed job_id={payload.job_id} attempt={payload.attempt}: {err}",
            file=sys.stderr,
            flush=True,
        )
        record_finalize_error(payload.job_id, err, payload.attempt)
        update_job(
            payload.job_id,
            status="finalizing",
            message=f"Partition swap failed (attempt {payload.attempt + 1}); will retry automatically. {err[:300]}",
            items_created=payload.items_created,
            items_failed=payload.items_failed,
        )
        raise
    finally:
        engine.dispose()


def requeue_finalize_after_failure(payload: BulkFinalizePayload) -> None:
    """Self-heal: schedule another finalize attempt at the tail of the queue."""
    wait_s = _finalize_backoff_seconds(payload.attempt + 1)
    time.sleep(wait_s)
    payload.attempt += 1
    enqueue_finalize(payload, force=True)
    print(
        f"[bulk-finalize] requeued job_id={payload.job_id} attempt={payload.attempt} after {wait_s:.1f}s",
        flush=True,
    )
