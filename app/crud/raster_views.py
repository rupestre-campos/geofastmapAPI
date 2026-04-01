"""CRUD for saved raster views (MosaicJSON paths)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.models.raster_view import RasterView


async def get_view(db: AsyncSession, view_id: str) -> RasterView | None:
    r = await db.execute(select(RasterView).where(RasterView.id == view_id))
    return r.scalar_one_or_none()


async def create_view(
    db: AsyncSession,
    *,
    title: str,
    json_relative_path: str,
    owner_id: int | None,
    visibility: str,
    view_id: str | None = None,
) -> RasterView:
    vid = view_id or str(uuid7())
    row = RasterView(
        id=vid,
        title=title,
        json_relative_path=json_relative_path,
        owner_id=owner_id,
        visibility=visibility,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_view(db: AsyncSession, view_id: str) -> bool:
    row = await get_view(db, view_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
