"""Per-collection expression indexes on features.properties for filter/delete performance."""
from __future__ import annotations

import hashlib
import re
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


def _create_index_sql(collection_id: str, field: str) -> tuple[str, dict[str, str]]:
    idx = property_index_name(collection_id, field)
    # Partial btree on expression; scoped to one collection for partition pruning.
    sql = text(
        f"""
        CREATE INDEX IF NOT EXISTS "{idx}"
        ON features ((properties->>:field_key))
        WHERE collection_id = :cid AND properties ? :field_key
        """
    )
    return sql, {"cid": collection_id, "field_key": field}


def sync_collection_property_indexes_sync(
    collection_id: str,
    old_fields: list[str],
    new_fields: list[str],
    *,
    engine: Engine | None = None,
) -> dict[str, list[str]]:
    """
    Create indexes for new_fields and drop indexes for fields removed since old_fields.
    Returns {"created": [...], "dropped": [...]} field names.
    """
    old_norm = normalize_property_index_fields(old_fields)
    new_norm = normalize_property_index_fields(new_fields)
    old_set = set(old_norm)
    new_set = set(new_norm)
    to_create = [f for f in new_norm if f not in old_set]
    to_drop = [f for f in old_norm if f not in new_set]

    owned = engine
    close = False
    if owned is None:
        settings = get_settings()
        owned = create_engine(settings.database_sync_url, isolation_level="AUTOCOMMIT")
        close = True
    try:
        with owned.connect() as conn:
            for field in to_create:
                sql, params = _create_index_sql(collection_id, field)
                conn.execute(sql, params)
            for field in to_drop:
                idx = property_index_name(collection_id, field)
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx}"'))
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
