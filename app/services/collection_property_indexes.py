"""Per-collection expression indexes on features.properties for filter/delete performance."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.db.features_partitions import (
    ensure_features_partition_sync,
    resolve_features_partition_relname_sync,
)

_FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_RELNAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_INDEX_PREFIX = "idx_fp_"
_DEFAULT_PARTITION = "features_default"


def validate_property_index_field(field: str) -> str:
    """Return a safe JSON property key name for indexing."""
    name = (field or "").strip()
    if not name or not _FIELD_RE.match(name):
        raise ValueError(
            f"Invalid property field {field!r}: use letters, digits, underscore; max 64 chars."
        )
    return name


def normalize_property_index_fields(raw: Any) -> list[str]:
    """Parse and dedupe property index field list from API/DB JSON."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        field = validate_property_index_field(item)
        if field not in seen:
            seen.add(field)
            out.append(field)
    return out


def property_index_name(collection_id: str, field: str) -> str:
    """Deterministic Postgres index name (≤ 63 chars) for collection + property field."""
    digest = hashlib.sha256(f"{collection_id}\0{field}".encode()).hexdigest()[:16]
    return f"{_INDEX_PREFIX}{digest}"


def _validate_relname(relname: str) -> str:
    name = (relname or "").strip()
    if not name or not _RELNAME_RE.match(name):
        raise ValueError(f"Unsafe partition relname: {relname!r}")
    return name


def resolve_property_index_target(
    engine: Engine, collection_id: str
) -> tuple[str, bool]:
    """
    Return (leaf_relname, include_collection_id_predicate).

    Postgres forbids CREATE INDEX CONCURRENTLY on partitioned parents; we always
    target the leaf (dedicated LIST partition or features_default).
    """
    part = resolve_features_partition_relname_sync(engine, collection_id)
    if not part:
        try:
            part = ensure_features_partition_sync(engine, collection_id)
        except Exception:
            part = _DEFAULT_PARTITION
    rel = _validate_relname(part)
    # Default (and any multi-tenant leaf) still needs collection_id in the predicate.
    include_cid = rel == _DEFAULT_PARTITION
    return rel, include_cid


def _create_index_sql(
    partition_relname: str,
    collection_id: str,
    field: str,
    *,
    include_collection_predicate: bool,
) -> tuple[Any, dict[str, str]]:
    idx = property_index_name(collection_id, field)
    rel = _validate_relname(partition_relname)
    # CONCURRENTLY on the leaf partition (not parent `features`).
    if include_collection_predicate:
        where = "WHERE collection_id = :cid AND properties ? :field_key"
        params: dict[str, str] = {"cid": collection_id, "field_key": field}
    else:
        where = "WHERE properties ? :field_key"
        params = {"field_key": field}
    sql = text(
        f"""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx}"
        ON "{rel}" ((properties->>:field_key))
        {where}
        """
    )
    return sql, params


def _drop_invalid_index_if_any(conn, index_name: str) -> bool:
    """Drop leftover INVALID indexes from a failed CONCURRENTLY build. Returns True if dropped."""
    row = conn.execute(
        text(
            """
            SELECT NOT i.indisvalid AS invalid
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE c.relname = :name
            LIMIT 1
            """
        ),
        {"name": index_name},
    ).first()
    if row and row.invalid:
        conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"'))
        return True
    return False


def _autocommit_engine(engine: Engine | None) -> tuple[Engine, bool]:
    """
    CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction.
    Always use AUTOCOMMIT; dispose only engines we create.
    """
    if engine is None:
        settings = get_settings()
        return create_engine(settings.database_sync_url, isolation_level="AUTOCOMMIT"), True
    # Caller may pass a pooled engine still in READ COMMITTED; open a dedicated
    # AUTOCOMMIT engine against the same URL so CONCURRENTLY never fails / locks.
    url = engine.url
    return create_engine(url, isolation_level="AUTOCOMMIT"), True


def sync_collection_property_indexes_sync(
    collection_id: str,
    old_fields: list[str],
    new_fields: list[str],
    *,
    engine: Engine | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, list[str]]:
    """
    Ensure indexes for new_fields exist and drop indexes for fields removed since old_fields.

    Creates use IF NOT EXISTS on every field in new_fields (not only the delta vs old_fields),
    so re-saving the same config repairs missing indexes after a failed job.

    Uses CREATE/DROP INDEX CONCURRENTLY on the collection's leaf partition so
    SELECT/INSERT/UPDATE/DELETE (and property filter searches) are not blocked.

    Returns {"created": [...], "dropped": [...]} field names (ensured / dropped).
    """
    old_norm = normalize_property_index_fields(old_fields)
    new_norm = normalize_property_index_fields(new_fields)
    new_set = set(new_norm)
    # Always ensure indexes for every desired field (IF NOT EXISTS). Do not key creates
    # off old_fields alone — after a failed job the DB already stores the same fields,
    # so a re-save would otherwise create an empty to_create and never build indexes.
    to_create = list(new_norm)
    to_drop = [f for f in old_norm if f not in new_set]

    def _progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    owned, close = _autocommit_engine(engine)
    try:
        partition: str | None = None
        include_cid = False
        if to_create:
            # Leaf only: PG rejects CONCURRENTLY on partitioned parent `features`.
            partition, include_cid = resolve_property_index_target(owned, collection_id)
        with owned.connect() as conn:
            for i, field in enumerate(to_create, start=1):
                assert partition is not None
                idx = property_index_name(collection_id, field)
                _progress(
                    f"Creating index CONCURRENTLY {i}/{len(to_create)} "
                    f"on {partition}.{field} ({idx})…"
                )
                if _drop_invalid_index_if_any(conn, idx):
                    _progress(
                        f"Dropped INVALID prior index {idx}; rebuilding {collection_id}.{field}…"
                    )
                sql, params = _create_index_sql(
                    partition,
                    collection_id,
                    field,
                    include_collection_predicate=include_cid,
                )
                conn.execute(sql, params)
            for i, field in enumerate(to_drop, start=1):
                _progress(
                    f"Dropping index CONCURRENTLY {i}/{len(to_drop)} "
                    f"on {collection_id}.{field}…"
                )
                idx = property_index_name(collection_id, field)
                conn.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{idx}"'))
    finally:
        if close and owned is not None:
            owned.dispose()

    return {"created": to_create, "dropped": to_drop}


def drop_all_collection_property_indexes_sync(
    collection_id: str,
    fields: list[str],
    *,
    engine: Engine | None = None,
) -> None:
    """Drop all managed property indexes for a collection (e.g. on delete)."""
    sync_collection_property_indexes_sync(
        collection_id,
        normalize_property_index_fields(fields),
        [],
        engine=engine,
    )
