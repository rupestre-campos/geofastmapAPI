"""Create named LIST partitions for the features table (partition key: collection_id)."""
from __future__ import annotations

import hashlib
import re
import time

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
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

# Serialize all partition swap DDL across collections (prevents cross-collection deadlocks).
_FEATURES_SWAP_ADVISORY_LOCK_KEY = 0xFEA7_0001


def _partition_bound_literal(collection_id: str) -> str:
    return ("FOR VALUES IN ('" + collection_id.replace("'", "''") + "')").replace(" ", "")


def _collection_id_from_bound(bound: str) -> str | None:
    """Parse collection_id from a LIST partition bound expression."""
    import re

    m = re.search(r"IN\s*\('((?:[^']|'')*)'\)", bound or "", re.IGNORECASE)
    if not m:
        return None
    return m.group(1).replace("''", "'")


def _resolve_features_partition_relname_conn(conn, collection_id: str) -> str | None:
    """Return attached features child relname for collection_id (any name, including bulk_staging_*)."""
    target_norm = _partition_bound_literal(collection_id)
    rows = conn.execute(text(_PARTITION_LIST_SQL)).fetchall()
    for row in rows:
        bound = (row.bound or "").replace(" ", "")
        if bound == target_norm:
            name = str(row.relname or "")
            return name if name else None
    return None


def partition_swap_already_complete_sync(
    engine: Engine, collection_id: str, staging_table: str
) -> bool:
    """True when staging is already the attached live partition for this collection."""
    with engine.connect() as conn:
        if _partition_is_attached_conn(conn, staging_table):
            return True
        live = _resolve_features_partition_relname_conn(conn, collection_id)
        return live == staging_table


def _partition_is_attached_conn(conn, relname: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'features' AND c.relname = :name
            """
        ),
        {"name": relname},
    ).first()
    return row is not None


def _table_exists_conn(conn, relname: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = :name
              AND c.relkind = 'r'
            """
        ),
        {"name": relname},
    ).first()
    return row is not None


def _drop_orphan_detached_partition_conn(
    conn,
    collection_id: str,
    staging_table: str,
    canonical_old: str,
) -> None:
    """Drop detached features_* leftovers from failed swaps (not attached to features)."""
    for name in (canonical_old,):
        if not name or name == staging_table:
            continue
        if not _table_exists_conn(conn, name):
            continue
        if _partition_is_attached_conn(conn, name):
            continue
        conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))


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
    """Return attached features partition relname for collection_id, or None (rows may be in features_default)."""
    with engine.connect() as conn:
        return _resolve_features_partition_relname_conn(conn, collection_id)


def list_attached_feature_partitions_sync(engine: Engine) -> list[tuple[str, str]]:
    """Return (relname, collection_id) for each non-default features partition."""
    out: list[tuple[str, str]] = []
    with engine.connect() as conn:
        rows = conn.execute(text(_PARTITION_LIST_SQL)).fetchall()
        for row in rows:
            relname = str(row.relname or "")
            cid = _collection_id_from_bound(row.bound or "")
            if relname and cid:
                out.append((relname, cid))
    return out


def cleanup_detached_orphan_feature_partitions_sync(engine: Engine) -> list[str]:
    """
    Drop detached features_* tables when another partition is already attached for the same collection.
    Fixes overlap errors from failed replace swaps (orphan features_* + attached bulk_staging_*).
    """
    dropped: list[str] = []
    with engine.begin() as conn:
        rows = conn.execute(text(_PARTITION_LIST_SQL)).fetchall()
        for row in rows:
            collection_id = _collection_id_from_bound(row.bound or "")
            attached = str(row.relname or "")
            if not collection_id or not attached:
                continue
            canonical = _safe_partition_name(collection_id)
            if canonical == attached:
                continue
            if not _table_exists_conn(conn, canonical):
                continue
            if _partition_is_attached_conn(conn, canonical):
                continue
            conn.execute(text(f'DROP TABLE IF EXISTS "{canonical}"'))
            dropped.append(canonical)
    return dropped


async def _partition_exists_for(db: AsyncSession, collection_id: str) -> bool:
    """Return True if a partition of features already exists for this collection_id."""
    r = await db.execute(text(_PARTITION_LIST_SQL))
    target_norm = _partition_bound_literal(collection_id)
    for row in r.fetchall():
        if row.bound and row.bound.replace(" ", "") == target_norm:
            return True
    return False


def _is_lock_timeout_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        msg = f"{msg} {orig}".lower()
    return "lock timeout" in msg or "locknotavailable" in msg or "55p03" in msg


def _staging_is_unlogged_conn(conn, relname: str) -> bool:
    row = conn.execute(
        text("SELECT relpersistence FROM pg_class WHERE relname = :name LIMIT 1"),
        {"name": relname},
    ).first()
    return bool(row and row[0] == "u")


def _prepare_staging_for_attach_sync(engine: Engine, collection_id: str, staging_table: str) -> None:
    """
    Heavy prep BEFORE any lock on parent `features`:
    - SET LOGGED (full table rewrite — minutes for large layers; staging is not attached,
      so nothing else reads it and no parent lock is needed).
    - Add a bound CHECK constraint so ATTACH PARTITION skips its validation scan
      (collection_id is NOT NULL in staging DDL, so the planner can prove the bound).
    Idempotent.
    """
    cid_escaped = collection_id.replace("'", "''")
    check_name = f"chk_bound_{staging_table}"[:63]
    with engine.begin() as conn:
        if _staging_is_unlogged_conn(conn, staging_table):
            conn.execute(text(f'ALTER TABLE "{staging_table}" SET LOGGED'))
        has_check = conn.execute(
            text(
                """
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = :rel AND c.conname = :con
                """
            ),
            {"rel": staging_table, "con": check_name},
        ).first()
        if not has_check:
            conn.execute(
                text(
                    f'ALTER TABLE "{staging_table}" ADD CONSTRAINT "{check_name}" '
                    f"CHECK (collection_id = '{cid_escaped}')"
                )
            )


def swap_staging_into_collection_partition_sync(
    engine: Engine,
    collection_id: str,
    staging_table: str,
) -> None:
    """
    Replace a collection's dedicated partition by detaching the old child and attaching staging.
    Staging must include the same columns as features (including generated properties_flat).

    The parent-locking window (DETACH/ATTACH need ACCESS EXCLUSIVE / strong locks on `features`,
    blocking ALL collections) is kept to catalog-only statements and guarded by a short
    lock_timeout with retries: a waiting DDL otherwise queues every new query behind it.
    """
    cid_escaped = collection_id.replace("'", "''")
    settings = get_settings()
    skip_touch = bool(getattr(settings, "bulk_skip_features_touch_trigger", True))
    canonical_old = _safe_partition_name(collection_id)

    # Phase 1 (no parent locks): rewrite staging to LOGGED + add bound CHECK so the
    # locked window below is catalog-only (no table rewrite / validation scan).
    _prepare_staging_for_attach_sync(engine, collection_id, staging_table)

    lock_timeout_s = max(1.0, float(getattr(settings, "bulk_swap_lock_timeout_seconds", 5.0) or 5.0))
    max_wait_s = max(lock_timeout_s, float(getattr(settings, "bulk_swap_lock_max_wait_seconds", 600.0) or 600.0))
    deadline = time.monotonic() + max_wait_s
    attempt = 0

    while True:
        attempt += 1
        try:
            with engine.begin() as conn:
                # Global + per-collection locks: serialize swap DDL and prevent deadlocks between workers.
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": _FEATURES_SWAP_ADVISORY_LOCK_KEY},
                )
                _advisory_lock_partition(conn, collection_id)
                # Fail fast instead of queueing behind long reads while blocking all new queries.
                conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout_s * 1000)}"))
                if skip_touch:
                    conn.execute(text("SET LOCAL geofast.bulk_skip_features_touch = 'on'"))
                conn.execute(
                    text("DELETE FROM features_default WHERE collection_id = :cid"),
                    {"cid": collection_id},
                )

                if _partition_is_attached_conn(conn, staging_table):
                    _drop_orphan_detached_partition_conn(conn, collection_id, staging_table, canonical_old)
                    return

                old_part = _resolve_features_partition_relname_conn(conn, collection_id)
                if old_part == staging_table:
                    _drop_orphan_detached_partition_conn(conn, collection_id, staging_table, canonical_old)
                    return

                if old_part and old_part != staging_table:
                    conn.execute(text(f'ALTER TABLE features DETACH PARTITION "{old_part}"'))
                    conn.execute(text(f'DROP TABLE "{old_part}"'))
                _drop_orphan_detached_partition_conn(conn, collection_id, staging_table, canonical_old)

                conn.execute(
                    text(
                        f"ALTER TABLE features ATTACH PARTITION \"{staging_table}\" "
                        f"FOR VALUES IN ('{cid_escaped}')"
                    )
                )
                return
        except (OperationalError, DBAPIError) as exc:
            if not _is_lock_timeout_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Partition swap for {collection_id!r} could not acquire locks on `features` "
                    f"within {max_wait_s:.0f}s ({attempt} attempts); busy readers kept the parent locked."
                ) from exc
            print(
                f"[partition-swap] lock timeout (attempt {attempt}) collection={collection_id}; retrying…",
                flush=True,
            )
            time.sleep(min(5.0, 0.5 * attempt))


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
