"""Per-collection expression indexes on features.properties for filter/delete performance."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings

_FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_INDEX_PREFIX = "idx_fp_"


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


def _create_index_sql(collection_id: str, field: str) -> tuple[Any, dict[str, str]]:
    idx = property_index_name(collection_id, field)
    # CONCURRENTLY: does not take AccessExclusiveLock; must run outside a transaction (AUTOCOMMIT).
    sql = text(
        f"""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx}"
        ON features ((properties->>:field_key))
        WHERE collection_id = :cid AND properties ? :field_key
        """
    )
    return sql, {"cid": collection_id, "field_key": field}


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
    Create indexes for new_fields and drop indexes for fields removed since old_fields.

    Uses CREATE/DROP INDEX CONCURRENTLY so SELECT/INSERT/UPDATE/DELETE (and
    property filter searches) are not blocked by AccessExclusiveLock.

    Returns {"created": [...], "dropped": [...]} field names.
    """
    old_norm = normalize_property_index_fields(old_fields)
    new_norm = normalize_property_index_fields(new_fields)
    old_set = set(old_norm)
    new_set = set(new_norm)
    to_create = [f for f in new_norm if f not in old_set]
    to_drop = [f for f in old_norm if f not in new_set]

    def _progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    owned, close = _autocommit_engine(engine)
    try:
        with owned.connect() as conn:
            for i, field in enumerate(to_create, start=1):
                idx = property_index_name(collection_id, field)
                _progress(
                    f"Creating index CONCURRENTLY {i}/{len(to_create)} "
                    f"on {collection_id}.{field} ({idx})…"
                )
                if _drop_invalid_index_if_any(conn, idx):
                    _progress(
                        f"Dropped INVALID prior index {idx}; rebuilding {collection_id}.{field}…"
                    )
                sql, params = _create_index_sql(collection_id, field)
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
