"""Per-job UNLOGGED staging tables for COPY bulk ingest and partition swap."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from app.core.config import get_settings
from app.db.features_partitions import (
    ensure_features_partition_sync,
    partition_swap_already_complete_sync,
    swap_staging_into_collection_partition_sync,
)
from app.services.bulk_staging_recovery import promote_with_staging_recovery

STAGING_TABLE_PREFIX = "bulk_staging_"

# Must match features partition columns (including generated properties_flat) for ATTACH PARTITION.
_STAGING_COLUMNS_DDL = """
    id varchar NOT NULL,
    collection_id varchar NOT NULL,
    part_index integer NOT NULL DEFAULT 0,
    geometry geometry(Geometry, 4326) NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    properties_flat text GENERATED ALWAYS AS (jsonb_flat_text(properties)) STORED,
    bulk_import_job_id varchar,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (id, collection_id, part_index)
"""


def staging_table_name(job_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", job_id)[:48].strip("_") or "job"
    return f"{STAGING_TABLE_PREFIX}{safe}"


def create_staging_table_sync(conn: Connection, job_id: str) -> str:
    """Create UNLOGGED staging table for a bulk job. Returns table name."""
    name = staging_table_name(job_id)
    conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    conn.execute(
        text(
            f"""
            CREATE UNLOGGED TABLE "{name}" (
                {_STAGING_COLUMNS_DDL.strip()}
            )
            """
        )
    )
    return name


def drop_staging_table_sync(engine: Engine, job_id: str) -> None:
    name = staging_table_name(job_id)
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))


def staging_table_exists_sync(engine: Engine, job_id: str) -> bool:
    name = staging_table_name(job_id)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname = :name
                  AND c.relkind = 'r'
                """
            ),
            {"name": name},
        ).first()
        return row is not None


def promote_staging_sync(
    engine: Engine,
    *,
    collection_id: str,
    job_id: str,
    mode: str,
) -> int:
    """
    Move staged rows into the live features partition.
    replace: detach/attach swap; append: INSERT SELECT then drop staging.
    Returns row count promoted.
    """
    staging = staging_table_name(job_id)
    with engine.connect() as conn:
        count = int(
            conn.execute(text(f'SELECT COUNT(*) FROM "{staging}"')).scalar() or 0
        )
    if count == 0:
        drop_staging_table_sync(engine, job_id)
        return 0

    skip_touch = bool(getattr(get_settings(), "bulk_skip_features_touch_trigger", True))
    if mode == "replace":
        if partition_swap_already_complete_sync(engine, collection_id, staging):
            return count
        promote_with_staging_recovery(
            engine,
            job_id=job_id,
            promote_fn=lambda: swap_staging_into_collection_partition_sync(
                engine, collection_id, staging
            ),
        )
    else:
        ensure_features_partition_sync(engine, collection_id)

        def _append_promote() -> None:
            with engine.begin() as conn:
                if skip_touch:
                    conn.execute(text("SET LOCAL geofast.bulk_skip_features_touch = 'on'"))
                conn.execute(
                    text(
                        f"""
                        INSERT INTO features (
                            id, collection_id, part_index, geometry, properties,
                            bulk_import_job_id, created_at, updated_at
                        )
                        SELECT
                            id, collection_id, part_index, geometry, properties,
                            bulk_import_job_id, created_at, updated_at
                        FROM "{staging}"
                        """
                    )
                )
                if skip_touch:
                    conn.execute(text("RESET geofast.bulk_skip_features_touch"))

        promote_with_staging_recovery(engine, job_id=job_id, promote_fn=_append_promote)
        drop_staging_table_sync(engine, job_id)
    return count


def list_orphan_staging_tables_sync(engine: Engine) -> list[str]:
    """Return relnames matching bulk_staging_* in the current schema."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relkind = 'r'
                  AND c.relname LIKE :pat
                """
            ),
            {"pat": f"{STAGING_TABLE_PREFIX}%"},
        ).fetchall()
    return [str(r[0]) for r in rows]


def cleanup_orphan_staging_tables_sync(engine: Engine, *, active_job_ids: set[str]) -> int:
    """
    Drop staging tables whose job_id is not in active_job_ids.
    active_job_ids should include running/replacing/pending job ids.
    """
    dropped = 0
    for relname in list_orphan_staging_tables_sync(engine):
        suffix = relname[len(STAGING_TABLE_PREFIX) :]
        # Match any active job whose sanitized name equals suffix
        if any(staging_table_name(jid) == relname for jid in active_job_ids):
            continue
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{relname}"'))
        dropped += 1
    return dropped


def staging_row_count_sync(engine: Engine, job_id: str) -> int:
    name = staging_table_name(job_id)
    with engine.connect() as conn:
        exists = conn.execute(
            text(
                """
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relname = :name
                """
            ),
            {"name": name},
        ).first()
        if not exists:
            return 0
        return int(conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
