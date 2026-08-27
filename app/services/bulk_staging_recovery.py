"""Recover or abandon bulk staging jobs with duplicate PK rows (no manual SQL)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.db.features_partitions import (
    _partition_is_attached_conn,
    _table_exists_conn,
    ensure_features_partition_sync,
)
from app.services.bulk_finalize_queue import clear_finalize_pending, remove_finalize_from_queue
from app.services.bulk_queue import unregister_bulk_import_job
from app.services.job_store import update_job


def _staging_table(job_id: str) -> str:
    from app.services.bulk_staging import staging_table_name

    return staging_table_name(job_id)


class StagingDuplicateUnrecoverableError(Exception):
    """Staging has duplicate primary keys that cannot be promoted."""

    def __init__(
        self,
        *,
        job_id: str,
        message: str,
        duplicates_removed: int = 0,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.duplicates_removed = duplicates_removed


def is_staging_pk_duplicate_error(exc_or_msg: BaseException | str) -> bool:
    """
    Detect duplicate-key failures on staging PK from full or truncated error text.
    Works with messages like: IntegrityError: ... UniqueViolation ... bulk_staging_*_pkey
    """
    parts: list[str] = [str(exc_or_msg).lower()]
    if isinstance(exc_or_msg, BaseException):
        orig = getattr(exc_or_msg, "__cause__", None) or getattr(exc_or_msg, "orig", None)
        if orig is not None:
            parts.append(str(orig).lower())
            pgcode = getattr(orig, "pgcode", None)
            if pgcode == "23505":
                parts.append("uniqueviolation")
    msg = " ".join(parts)
    if "uniqueviolation" in msg or "duplicate key value" in msg:
        if "bulk_staging" in msg or "_pkey" in msg or "staging" in msg:
            return True
    return False


def staging_duplicate_count_sync(engine: Engine, job_id: str) -> int:
    """Count extra rows that share the same (id, collection_id, part_index)."""
    staging = _staging_table(job_id)
    with engine.connect() as conn:
        if not _table_exists_conn(conn, staging):
            return 0
        return int(
            conn.execute(
                text(
                    f"""
                    WITH ranked AS (
                        SELECT
                            ROW_NUMBER() OVER (
                                PARTITION BY id, collection_id, part_index
                                ORDER BY updated_at DESC, ctid DESC
                            ) AS rn
                        FROM "{staging}"
                    )
                    SELECT COUNT(*) FROM ranked WHERE rn > 1
                    """
                )
            ).scalar()
            or 0
        )


def dedupe_staging_table_sync(engine: Engine, job_id: str) -> int:
    """Delete duplicate PK rows from staging; keep newest by updated_at. Returns rows removed."""
    staging = _staging_table(job_id)
    with engine.begin() as conn:
        if not _table_exists_conn(conn, staging):
            return 0
        before = int(conn.execute(text(f'SELECT COUNT(*) FROM "{staging}"')).scalar() or 0)
        conn.execute(
            text(
                f"""
                WITH ranked AS (
                    SELECT
                        ctid,
                        ROW_NUMBER() OVER (
                            PARTITION BY id, collection_id, part_index
                            ORDER BY updated_at DESC, ctid DESC
                        ) AS rn
                    FROM "{staging}"
                )
                DELETE FROM "{staging}"
                WHERE ctid IN (SELECT ctid FROM ranked WHERE rn > 1)
                """
            )
        )
        after = int(conn.execute(text(f'SELECT COUNT(*) FROM "{staging}"')).scalar() or 0)
    return max(0, before - after)


def prepare_staging_for_promote_sync(engine: Engine, job_id: str) -> int:
    """
    Remove duplicate PK rows before partition swap / append insert.
    Raises StagingDuplicateUnrecoverableError when duplicates remain.
    """
    dupes = staging_duplicate_count_sync(engine, job_id)
    if dupes <= 0:
        return 0
    removed = dedupe_staging_table_sync(engine, job_id)
    remaining = staging_duplicate_count_sync(engine, job_id)
    if remaining > 0:
        raise StagingDuplicateUnrecoverableError(
            job_id=job_id,
            message=(
                f"Staging still has {remaining} duplicate primary-key rows "
                f"after removing {removed}."
            ),
            duplicates_removed=removed,
        )
    return removed


def cleanup_failed_staging_import_sync(engine: Engine, job_id: str, collection_id: str) -> None:
    """
    Drop staging table (detach first if attached) and ensure an empty live partition exists.
    Safe when replace swap failed after dropping the old partition.
    """
    from app.core.config import get_settings

    staging = _staging_table(job_id)
    lock_ms = int(
        max(1.0, float(getattr(get_settings(), "bulk_swap_lock_timeout_seconds", 5.0) or 5.0)) * 1000
    )
    with engine.begin() as conn:
        if _table_exists_conn(conn, staging):
            if _partition_is_attached_conn(conn, staging):
                # DETACH takes ACCESS EXCLUSIVE on parent `features`; never queue indefinitely
                # (a waiting DDL blocks every new query on all collections).
                conn.execute(text(f"SET LOCAL lock_timeout = {lock_ms}"))
                conn.execute(text(f'ALTER TABLE features DETACH PARTITION "{staging}"'))
            conn.execute(text(f'DROP TABLE IF EXISTS "{staging}"'))
    ensure_features_partition_sync(engine, collection_id)


def abandon_staging_finalize_job(
    engine: Engine,
    *,
    job_id: str,
    collection_id: str,
    reason: str,
    items_created: int = 0,
    items_failed: int = 0,
) -> None:
    """Fail job, remove queue state, and drop staging so the user can re-import."""
    cleanup_failed_staging_import_sync(engine, job_id, collection_id)
    remove_finalize_from_queue(job_id)
    clear_finalize_pending(job_id)
    unregister_bulk_import_job(job_id)
    update_job(
        job_id,
        status="failed",
        message=(
            f"Bulk import abandoned: {reason} "
            "Staging was removed — please re-import this file."
        )[:2000],
        items_created=items_created,
        items_failed=items_failed,
    )
    print(
        f"[bulk-staging-recovery] abandoned job_id={job_id} collection={collection_id}: {reason}",
        flush=True,
    )


def promote_with_staging_recovery(
    engine: Engine,
    *,
    job_id: str,
    promote_fn,
) -> None:
    """
    Run promote_fn() after deduping staging; retry once on duplicate-key IntegrityError.
    promote_fn should perform swap/insert and may raise IntegrityError.
    """
    prepare_staging_for_promote_sync(engine, job_id)
    try:
        promote_fn()
    except IntegrityError as e:
        if not is_staging_pk_duplicate_error(e):
            raise
        prepare_staging_for_promote_sync(engine, job_id)
        try:
            promote_fn()
        except IntegrityError as e2:
            if is_staging_pk_duplicate_error(e2):
                raise StagingDuplicateUnrecoverableError(
                    job_id=job_id,
                    message=str(e2)[:500],
                ) from e2
            raise
