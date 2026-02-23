"""Create named LIST partitions for the features table (partition key: collection_id)."""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _safe_partition_name(collection_id: str) -> str:
    """Return a valid, unique table name for a features partition (max 63 chars)."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)[:45].strip("_") or "default"
    h = hashlib.sha256(collection_id.encode()).hexdigest()[:8]
    return f"features_{safe}_{h}"


async def _partition_exists_for(db: AsyncSession, collection_id: str) -> bool:
    """Return True if a partition of features already exists for this collection_id."""
    r = await db.execute(
        text("""
            SELECT pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'features'
              AND c.relname != 'features_default'
        """)
    )
    target = "FOR VALUES IN ('" + collection_id.replace("'", "''") + "')"
    target_norm = target.replace(" ", "")
    for row in r.fetchall():
        if row.bound and row.bound.replace(" ", "") == target_norm:
            return True
    return False


async def ensure_features_partition(db: AsyncSession, collection_id: str) -> None:
    """
    Ensure a dedicated partition exists for this collection_id.
    If not: create it and move any rows from features_default into it.
    Idempotent. Call from create_collection so new collections get a named partition.
    """
    exists = await _partition_exists_for(db, collection_id)
    if not exists:
        name = _safe_partition_name(collection_id)
        await db.execute(
            text(f'CREATE TABLE "{name}" PARTITION OF features FOR VALUES IN (:cid)'),
            {"cid": collection_id},
        )

    await db.execute(
        text("""
            INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at)
            SELECT id, collection_id, geometry, properties, created_at, updated_at
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
