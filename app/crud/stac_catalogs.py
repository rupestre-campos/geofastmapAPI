"""CRUD for registered STAC API catalogs."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.models.stac_catalog import StacCatalog


async def list_catalogs(
    db: AsyncSession,
    *,
    enabled_only: bool = True,
) -> list[StacCatalog]:
    q = select(StacCatalog).order_by(StacCatalog.title.asc())
    if enabled_only:
        q = q.where(StacCatalog.enabled.is_(True))
    r = await db.execute(q)
    return list(r.scalars().all())


async def get_catalog(db: AsyncSession, catalog_id: str) -> StacCatalog | None:
    r = await db.execute(select(StacCatalog).where(StacCatalog.id == catalog_id))
    return r.scalar_one_or_none()


async def create_catalog(
    db: AsyncSession,
    *,
    title: str,
    stac_api_root_url: str,
    enabled: bool = True,
    notes: str | None = None,
    default_collections: list | dict | None = None,
    catalog_id: str | None = None,
) -> StacCatalog:
    cid = catalog_id or str(uuid7())
    row = StacCatalog(
        id=cid,
        title=title,
        stac_api_root_url=stac_api_root_url.rstrip("/"),
        enabled=enabled,
        notes=notes,
        default_collections=default_collections,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_catalog(
    db: AsyncSession,
    catalog_id: str,
    *,
    title: str | None = None,
    stac_api_root_url: str | None = None,
    enabled: bool | None = None,
    notes: str | None = None,
    default_collections: list | dict | None = None,
) -> StacCatalog | None:
    row = await get_catalog(db, catalog_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if stac_api_root_url is not None:
        row.stac_api_root_url = stac_api_root_url.rstrip("/")
    if enabled is not None:
        row.enabled = enabled
    if notes is not None:
        row.notes = notes
    if default_collections is not None:
        row.default_collections = default_collections
    await db.commit()
    await db.refresh(row)
    return row


async def delete_catalog(db: AsyncSession, catalog_id: str) -> bool:
    row = await get_catalog(db, catalog_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
