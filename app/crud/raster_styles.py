from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raster_style import RasterStyle


async def list_raster_styles(db: AsyncSession, collection_id: str) -> list[RasterStyle]:
    r = await db.execute(
        select(RasterStyle).where(RasterStyle.collection_id == collection_id).order_by(RasterStyle.id.asc())
    )
    return list(r.scalars().all())


async def get_raster_style(db: AsyncSession, collection_id: str, style_id: str) -> RasterStyle | None:
    r = await db.execute(
        select(RasterStyle).where(
            RasterStyle.collection_id == collection_id,
            RasterStyle.id == style_id,
        )
    )
    return r.scalar_one_or_none()


async def get_default_raster_style(db: AsyncSession, collection_id: str) -> RasterStyle | None:
    r = await db.execute(
        select(RasterStyle).where(
            RasterStyle.collection_id == collection_id,
            RasterStyle.is_default.is_(True),
        )
    )
    return r.scalar_one_or_none()


async def upsert_raster_style(
    db: AsyncSession,
    *,
    collection_id: str,
    style_id: str,
    title: str | None,
    style_spec: dict,
    owner_id: int | None,
    set_default: bool = False,
) -> RasterStyle:
    s = await get_raster_style(db, collection_id, style_id)
    if s is None:
        s = RasterStyle(
            collection_id=collection_id,
            id=style_id,
            title=title,
            style_spec=style_spec,
            owner_id=owner_id,
            is_default=False,
        )
        db.add(s)
    else:
        s.title = title
        s.style_spec = style_spec
    if set_default:
        await db.execute(
            update(RasterStyle)
            .where(RasterStyle.collection_id == collection_id)
            .values(is_default=False)
        )
        s.is_default = True
    await db.commit()
    await db.refresh(s)
    return s

