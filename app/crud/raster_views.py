"""CRUD for saved raster views (MosaicJSON paths)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.models.raster_view import RasterView
from app.models.resource_share import RESOURCE_TYPE_RASTER_VIEW, ResourceShare

if TYPE_CHECKING:
    from app.models.user import User


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
    bbox: list[float] | None = None,
    definition: dict | None = None,
    allow_public_maps: bool = False,
) -> RasterView:
    vid = view_id or str(uuid7())
    row = RasterView(
        id=vid,
        title=title,
        json_relative_path=json_relative_path,
        owner_id=owner_id,
        visibility=visibility,
        bbox=bbox,
        definition=definition,
        allow_public_maps=allow_public_maps,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


_MISSING = object()


async def update_view(
    db: AsyncSession,
    view_id: str,
    *,
    title: str | None = None,
    visibility: str | None = None,
    json_relative_path: str | None = None,
    bbox: list[float] | None | object = _MISSING,
    definition: dict | None | object = _MISSING,
    allow_public_maps: bool | None = None,
) -> RasterView | None:
    row = await get_view(db, view_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if visibility is not None:
        row.visibility = visibility
    if json_relative_path is not None:
        row.json_relative_path = json_relative_path
    if bbox is not _MISSING:
        row.bbox = bbox  # type: ignore[assignment]
    if definition is not _MISSING:
        row.definition = definition  # type: ignore[assignment]
    if allow_public_maps is not None:
        row.allow_public_maps = allow_public_maps
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def list_views_visible_to_user(
    db: AsyncSession,
    *,
    current_user: "User | None",
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    bbox_intersects: tuple[float, float, float, float] | None = None,
    mine_only: bool = False,
) -> tuple[list[RasterView], int]:
    """List raster views the user may see; admin sees all. Returns (rows, total_count)."""
    base = select(RasterView)
    count_base = select(func.count()).select_from(RasterView)

    if mine_only and current_user is not None:
        cond = RasterView.owner_id == current_user.id
        base = base.where(cond)
        count_base = count_base.where(cond)
    elif current_user is not None and current_user.is_admin:
        pass
    elif current_user is None:
        cond = RasterView.visibility == "public"
        base = base.where(cond)
        count_base = count_base.where(cond)
    else:
        share_exists = (
            select(1)
            .where(ResourceShare.resource_type == RESOURCE_TYPE_RASTER_VIEW)
            .where(ResourceShare.resource_id == RasterView.id)
            .where(ResourceShare.username == current_user.username)
        )
        cond = or_(
            RasterView.visibility.in_(["public", "logged"]),
            RasterView.owner_id == current_user.id,
            exists(share_exists),
        )
        base = base.where(cond)
        count_base = count_base.where(cond)

    if q and q.strip():
        pat = f"%{q.strip()}%"
        base = base.where(RasterView.title.ilike(pat))
        count_base = count_base.where(RasterView.title.ilike(pat))

    if bbox_intersects is not None:
        minx, miny, maxx, maxy = bbox_intersects
        bbox_clause = text(
            "raster_views.bbox IS NOT NULL AND "
            "(raster_views.bbox->>0)::float <= :fb_maxx AND "
            "(raster_views.bbox->>2)::float >= :fb_minx AND "
            "(raster_views.bbox->>1)::float <= :fb_maxy AND "
            "(raster_views.bbox->>3)::float >= :fb_miny"
        ).bindparams(fb_minx=minx, fb_miny=miny, fb_maxx=maxx, fb_maxy=maxy)
        base = base.where(bbox_clause)
        count_base = count_base.where(bbox_clause)

    total_r = await db.execute(count_base)
    total = int(total_r.scalar_one() or 0)

    result = await db.execute(
        base.order_by(RasterView.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), total


async def delete_view(db: AsyncSession, view_id: str) -> bool:
    row = await get_view(db, view_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
