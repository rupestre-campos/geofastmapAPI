"""CRUD for collection_tiles (PMTiles build tracking)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection_tiles import CollectionTiles


async def get_collection_tiles(db: AsyncSession, collection_id: str) -> CollectionTiles | None:
    result = await db.execute(
        text("SELECT collection_id, pmtiles_path, built_at, features_updated_at FROM collection_tiles WHERE collection_id = :cid"),
        {"cid": collection_id},
    )
    row = result.first()
    if not row:
        return None
    return CollectionTiles(
        collection_id=row.collection_id,
        pmtiles_path=row.pmtiles_path,
        built_at=row.built_at,
        features_updated_at=row.features_updated_at,
    )


async def get_max_feature_updated_at(db: AsyncSession, collection_id: str) -> datetime | None:
    """Max updated_at of features in this collection. None if no features."""
    result = await db.execute(
        text("SELECT MAX(updated_at) AS m FROM features WHERE collection_id = :cid"),
        {"cid": collection_id},
    )
    row = result.first()
    return row.m if row and row.m else None


async def clear_static_tiles(db: AsyncSession, collection_id: str) -> bool:
    """Clear static tiles record (set pmtiles_path, built_at, features_updated_at to NULL). Returns True if a row was updated."""
    result = await db.execute(
        text("""
            UPDATE collection_tiles
            SET pmtiles_path = NULL, built_at = NULL, features_updated_at = NULL
            WHERE collection_id = :cid
        """),
        {"cid": collection_id},
    )
    await db.commit()
    return result.rowcount > 0


async def upsert_collection_tiles(
    db: AsyncSession,
    collection_id: str,
    pmtiles_path: str,
    features_updated_at: datetime,
) -> None:
    await db.execute(
        text("""
            INSERT INTO collection_tiles (collection_id, pmtiles_path, built_at, features_updated_at)
            VALUES (:cid, :path, :now, :fua)
            ON CONFLICT (collection_id) DO UPDATE SET
                pmtiles_path = EXCLUDED.pmtiles_path,
                built_at = EXCLUDED.built_at,
                features_updated_at = EXCLUDED.features_updated_at
        """),
        {"cid": collection_id, "path": pmtiles_path, "now": datetime.now(timezone.utc), "fua": features_updated_at},
    )
    await db.commit()
