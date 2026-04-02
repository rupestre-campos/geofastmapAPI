"""Public tile grants for STAC items (anonymous viewers on public maps)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stac_public_tile_grant import StacPublicTileGrant


async def has_grant(
    db: AsyncSession,
    catalog_id: str,
    stac_collection_id: str,
    stac_item_id: str,
) -> bool:
    r = await db.execute(
        select(StacPublicTileGrant.id).where(
            StacPublicTileGrant.catalog_id == catalog_id,
            StacPublicTileGrant.stac_collection_id == stac_collection_id,
            StacPublicTileGrant.stac_item_id == stac_item_id,
        ).limit(1)
    )
    return r.scalar_one_or_none() is not None


async def get_grant_row(
    db: AsyncSession,
    catalog_id: str,
    stac_collection_id: str,
    stac_item_id: str,
) -> StacPublicTileGrant | None:
    r = await db.execute(
        select(StacPublicTileGrant).where(
            StacPublicTileGrant.catalog_id == catalog_id,
            StacPublicTileGrant.stac_collection_id == stac_collection_id,
            StacPublicTileGrant.stac_item_id == stac_item_id,
        ).limit(1)
    )
    return r.scalar_one_or_none()


async def ensure_grant(
    db: AsyncSession,
    *,
    catalog_id: str,
    stac_collection_id: str,
    stac_item_id: str,
    granted_by_user_id: int,
) -> StacPublicTileGrant:
    existing = await get_grant_row(db, catalog_id, stac_collection_id, stac_item_id)
    if existing is not None:
        return existing
    row = StacPublicTileGrant(
        catalog_id=catalog_id,
        stac_collection_id=stac_collection_id,
        stac_item_id=stac_item_id,
        granted_by_user_id=granted_by_user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
        return row
    except IntegrityError:
        await db.rollback()
        existing = await get_grant_row(db, catalog_id, stac_collection_id, stac_item_id)
        if existing is not None:
            return existing
        raise
