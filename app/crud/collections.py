from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection
from app.schemas.collection import CollectionCreate, Extent, CollectionPatch, CollectionReplace


async def list_collections(db: AsyncSession) -> Sequence[Collection]:
    result = await db.execute(select(Collection).order_by(Collection.id))
    return result.scalars().all()


async def get_collection(db: AsyncSession, collection_id: str) -> Collection | None:
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id)
    )
    return result.scalar_one_or_none()


def _row_to_extent(row: Any) -> Extent:
    return Extent(
        bbox=[[float(row.minx), float(row.miny), float(row.maxx), float(row.maxy)]],
        crs="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
    )


async def get_collection_bbox_from_features(
    db: AsyncSession, collection_id: str
) -> Extent | None:
    """Compute extent (bbox) from feature geometries in the collection. Returns None if no geometries."""
    result = await db.execute(
        text("""
            SELECT ST_XMin(e) AS minx, ST_YMin(e) AS miny, ST_XMax(e) AS maxx, ST_YMax(e) AS maxy
            FROM (SELECT ST_Extent(geometry) AS e FROM features WHERE collection_id = :cid AND geometry IS NOT NULL) t
        """),
        {"cid": collection_id},
    )
    row = result.first()
    if row is None or row.minx is None:
        return None
    return _row_to_extent(row)


async def get_collections_bboxes(db: AsyncSession) -> dict[str, Extent]:
    """Compute extent (bbox) from features for every collection that has geometries. One query for list endpoint."""
    result = await db.execute(
        text("""
            SELECT collection_id,
                   ST_XMin(ST_Extent(geometry)) AS minx,
                   ST_YMin(ST_Extent(geometry)) AS miny,
                   ST_XMax(ST_Extent(geometry)) AS maxx,
                   ST_YMax(ST_Extent(geometry)) AS maxy
            FROM features
            WHERE geometry IS NOT NULL
            GROUP BY collection_id
        """)
    )
    rows = result.all()
    return {row.collection_id: _row_to_extent(row) for row in rows}


async def create_collection(
    db: AsyncSession, data: CollectionCreate
) -> Collection:
    collection = Collection(
        id=data.id,
        title=data.title,
        description=data.description,
        extent=data.extent.model_dump() if data.extent else None,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    return collection


async def replace_collection(
    db: AsyncSession, collection_id: str, data: CollectionReplace
) -> Collection | None:
    """Replace collection metadata (PUT). Returns updated collection or None."""
    collection = await get_collection(db, collection_id)
    if collection is None:
        return None
    collection.title = data.title
    collection.description = data.description
    collection.extent = data.extent.model_dump() if data.extent else None
    await db.commit()
    await db.refresh(collection)
    return collection


async def patch_collection(
    db: AsyncSession, collection_id: str, data: CollectionPatch
) -> Collection | None:
    """Partial update (PATCH). Only updates provided fields. Returns updated collection or None."""
    collection = await get_collection(db, collection_id)
    if collection is None:
        return None
    if "title" in data.model_fields_set:
        collection.title = data.title
    if "description" in data.model_fields_set:
        collection.description = data.description
    if "extent" in data.model_fields_set:
        collection.extent = data.extent.model_dump() if data.extent else None
    await db.commit()
    await db.refresh(collection)
    return collection


async def delete_collection(db: AsyncSession, collection_id: str) -> bool:
    """Delete a collection by id. Returns True if deleted."""
    collection = await get_collection(db, collection_id)
    if collection is None:
        return False

    await db.delete(collection)
    await db.commit()
    return True

