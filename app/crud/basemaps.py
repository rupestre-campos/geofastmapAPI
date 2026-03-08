"""CRUD for system basemaps."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.basemap import Basemap


async def list_basemaps(db: AsyncSession) -> Sequence[Basemap]:
    """List all basemaps ordered by sort_order then id."""
    result = await db.execute(
        select(Basemap).order_by(Basemap.sort_order, Basemap.id)
    )
    return result.scalars().all()


async def get_basemap(db: AsyncSession, basemap_id: str) -> Basemap | None:
    """Get a basemap by id."""
    result = await db.execute(select(Basemap).where(Basemap.id == basemap_id))
    return result.scalar_one_or_none()


async def create_basemap(
    db: AsyncSession,
    *,
    id: str,
    name: str,
    tiles: list[str],
    copyright: str | None = None,
    min_zoom: int = 0,
    max_zoom: int = 22,
    labels: str | None = None,
    sort_order: int = 0,
) -> Basemap:
    """Create a basemap."""
    b = Basemap(
        id=id,
        name=name,
        copyright=copyright,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tiles=tiles,
        labels=labels,
        sort_order=sort_order,
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b


async def update_basemap(
    db: AsyncSession,
    basemap_id: str,
    *,
    name: str | None = None,
    copyright: str | None = None,
    min_zoom: int | None = None,
    max_zoom: int | None = None,
    tiles: list[str] | None = None,
    labels: str | None = None,
    sort_order: int | None = None,
) -> Basemap | None:
    """Update a basemap. Returns None if not found."""
    b = await get_basemap(db, basemap_id)
    if b is None:
        return None
    if name is not None:
        b.name = name
    if copyright is not None:
        b.copyright = copyright
    if min_zoom is not None:
        b.min_zoom = min_zoom
    if max_zoom is not None:
        b.max_zoom = max_zoom
    if tiles is not None:
        b.tiles = tiles
    if labels is not None:
        b.labels = labels
    if sort_order is not None:
        b.sort_order = sort_order
    await db.commit()
    await db.refresh(b)
    return b


async def delete_basemap(db: AsyncSession, basemap_id: str) -> bool:
    """Delete a basemap. Returns True if deleted."""
    b = await get_basemap(db, basemap_id)
    if b is None:
        return False
    await db.delete(b)
    await db.commit()
    return True
