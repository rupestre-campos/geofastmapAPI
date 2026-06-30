"""Create named LIST partitions for the features table (partition key: collection_id)."""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings


def _safe_partition_name(collection_id: str) -> str:
    """Return a valid, unique table name for a features partition (max 63 chars)."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)[:45].strip("_") or "default"
    h = hashlib.sha256(collection_id.encode()).hexdigest()[:8]
    return f"features_{safe}_{h}"


_PARTITION_LIST_SQL = """
    SELECT c.relname AS relname, pg_get_expr(c.relpartbound, c.oid) AS bound
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relname = 'features'
      AND c.relname != 'features_default'
"""


def _partition_bound_literal(collection_id: str) -> str:
    return ("FOR VALUES IN ('" + collection_id.replace("'", "''") + "')").replace(" ", "")


def _migrate_default_rows_to_partition_sync(conn, collection_id: str) -> None:
    """Move any rows for collection_id from features_default into the routed partition."""
    conn.execute(
        text(
            """
            INSERT INTO features (
                id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
            )
            SELECT id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
            FROM features_default
            WHERE collection_id = :cid
            """
        ),
        {"cid": collection_id},
    )
    conn.execute(
        text("DELETE FROM features_default WHERE collection_id = :cid"),
        {"cid": collection_id},
    )


def _create_partition_sync(conn, collection_id: str) -> str:
    name = _safe_partition_name(collection_id)
    cid_escaped = collection_id.replace("'", "''")
    conn.execute(
        text(f'CREATE TABLE "{name}" PARTITION OF features FOR VALUES IN (\'{cid_escaped}\')'),
    )
    return name


def _advisory_lock_partition(conn, collection_id: str) -> None:
    """Serialize partition create/migrate for one collection (concurrent bulk shards)."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:cid)::bigint)"),
        {"cid": collection_id},
    )


def ensure_features_partition_sync(engine: Engine, collection_id: str) -> str:
    """
    Ensure a dedicated LIST partition exists for collection_id and migrate rows out of features_default.
    Returns partition relname. Idempotent.
    """
    existing = resolve_features_partition_relname_sync(engine, collection_id)
    if existing:
        with engine.begin() as conn:
            _advisory_lock_partition(conn, collection_id)
            _migrate_default_rows_to_partition_sync(conn, collection_id)
        return existing

    with engine.begin() as conn:
        _advisory_lock_partition(conn, collection_id)
        existing = resolve_features_partition_relname_sync(engine, collection_id)
        if existing:
            _migrate_default_rows_to_partition_sync(conn, collection_id)
            return existing
        in_default = int(
            conn.execute(
                text("SELECT COUNT(*) FROM features_default WHERE collection_id = :cid"),
                {"cid": collection_id},
            ).scalar()
            or 0
        )
        if in_default > 0:
            # Cannot CREATE PARTITION while default still holds these rows (PG LIST overlap rule).
            temp_name = "_ensure_" + hashlib.sha256(collection_id.encode()).hexdigest()[:12]
            conn.execute(
                text(
                    f"""
                    CREATE TEMP TABLE "{temp_name}" ON COMMIT DROP AS
                    SELECT id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
                    FROM features_default
                    WHERE collection_id = :cid
                    """
                ),
                {"cid": collection_id},
            )
            conn.execute(
                text("DELETE FROM features_default WHERE collection_id = :cid"),
                {"cid": collection_id},
            )
            part_name = _create_partition_sync(conn, collection_id)
            conn.execute(
                text(
                    f"""
                    INSERT INTO features (
                        id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
                    )
                    SELECT id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
                    FROM "{temp_name}"
                    """
                ),
            )
            return part_name
        try:
            return _create_partition_sync(conn, collection_id)
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate" in msg:
                again = resolve_features_partition_relname_sync(engine, collection_id)
                if again:
                    return again
            raise


def resolve_features_partition_relname_sync(engine: Engine, collection_id: str) -> str | None:
    """Return dedicated features partition relname for collection_id, or None (rows may be in features_default)."""
    target_norm = _partition_bound_literal(collection_id)

    def _read() -> str | None:
        with engine.connect() as conn:
            rows = conn.execute(text(_PARTITION_LIST_SQL)).fetchall()
            for row in rows:
                bound = (row.bound or "").replace(" ", "")
                if bound == target_norm:
                    name = str(row.relname or "")
                    if re.fullmatch(r"features_[a-zA-Z0-9_]+", name):
                        return name
        return None

    return _read()


async def _partition_exists_for(db: AsyncSession, collection_id: str) -> bool:
    """Return True if a partition of features already exists for this collection_id."""
    r = await db.execute(text(_PARTITION_LIST_SQL))
    target_norm = _partition_bound_literal(collection_id)
    for row in r.fetchall():
        if row.bound and row.bound.replace(" ", "") == target_norm:
            return True
    return False


def swap_staging_into_collection_partition_sync(
    engine: Engine,
    collection_id: str,
    staging_table: str,
) -> None:
    """
    Replace a collection's dedicated partition by detaching the old child and attaching staging.
    Staging must be a standalone table with the same columns as features (except generated properties_flat).
    """
    cid_escaped = collection_id.replace("'", "''")
    settings = get_settings()
    skip_touch = bool(getattr(settings, "bulk_skip_features_touch_trigger", True))

    with engine.begin() as conn:
        if skip_touch:
            conn.execute(text("SET LOCAL geofast.bulk_skip_features_touch = 'on'"))
        conn.execute(
            text("DELETE FROM features_default WHERE collection_id = :cid"),
            {"cid": collection_id},
        )
        old_part = resolve_features_partition_relname_sync(engine, collection_id)
        if old_part and old_part != staging_table:
            conn.execute(text(f'ALTER TABLE features DETACH PARTITION "{old_part}"'))
            conn.execute(text(f'DROP TABLE "{old_part}"'))
        elif old_part == staging_table:
            return
        conn.execute(text(f'ALTER TABLE "{staging_table}" SET LOGGED'))
        conn.execute(
            text(
                f"ALTER TABLE features ATTACH PARTITION \"{staging_table}\" "
                f"FOR VALUES IN ('{cid_escaped}')"
            )
        )
        if skip_touch:
            conn.execute(text("RESET geofast.bulk_skip_features_touch"))


async def ensure_features_partition(db: AsyncSession, collection_id: str) -> None:
    """
    Ensure a dedicated partition exists for this collection_id.
    If not: create it and move any rows from features_default into it.
    Idempotent. Call from create_collection so new collections get a named partition.
    """
    exists = await _partition_exists_for(db, collection_id)
    if not exists:
        name = _safe_partition_name(collection_id)
        # PostgreSQL does not allow bound parameters in DDL; escape collection_id for literal.
        cid_escaped = collection_id.replace("'", "''")
        await db.execute(
            text(f'CREATE TABLE "{name}" PARTITION OF features FOR VALUES IN (\'{cid_escaped}\')'),
        )

    await db.execute(
        text("""
            INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id)
            SELECT id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id
            FROM features_default
            WHERE collection_id = :cid
        """),
        {"cid": collection_id},
    )
    await db.execute(
        text("DELETE FROM features_default WHERE collection_id = :cid"),
        {"cid": collection_id},
    )
    await db.commit()
