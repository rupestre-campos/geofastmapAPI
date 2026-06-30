"""CRUD for collections."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Tuple

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collection_tiles as tiles_crud
from app.crud import styles as styles_crud
from app.db.features_partitions import ensure_features_partition
from app.models.collection import (
    Collection,
    COLLECTION_TYPE_COMPOSITE,
    COLLECTION_TYPE_RASTER,
    COLLECTION_TYPE_VECTOR,
    VISIBILITY_LOGGED,
    VISIBILITY_PUBLIC,
)
from app.models.collection_tiles import CollectionTiles
from app.models.resource_share import ResourceShare
from app.services.collection_property_indexes import (
    drop_all_collection_property_indexes_sync,
    normalize_property_index_fields,
    sync_collection_property_indexes_sync,
)
from app.schemas.collection import CollectionCreate, Extent, CollectionPatch, CollectionReplace

if TYPE_CHECKING:
    from app.models.user import User


def _like_escape(value: str) -> str:
    """Escape % and _ for use in LIKE/ILIKE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _order_by_collection(sortby: str | None, sortdesc: bool):
    """Return order_by clause for Collection (id, title, description, created_at)."""
    if not sortby or sortby == "id":
        return Collection.id.desc() if sortdesc else Collection.id.asc()
    if sortby == "title":
        return Collection.title.desc() if sortdesc else Collection.title.asc()
    if sortby == "description":
        return Collection.description.desc() if sortdesc else Collection.description.asc()
    if sortby == "created_at":
        return Collection.created_at.desc() if sortdesc else Collection.created_at.asc()
    return Collection.id.asc()


async def list_collections(
    db: AsyncSession,
    *,
    q: str | None = None,
    bbox_tuple: tuple[float, float, float, float] | None = None,
    sortby: str | None = None,
    sortdesc: bool = False,
    limit: int | None = None,
    offset: int = 0,
    has_static_tiles: bool = False,
    current_user: "User | None" = None,
    only_public: bool = False,
    collection_type: str | None = None,
) -> Tuple[Sequence[Collection], int]:
    """
    List collections with optional full-text search (id, title, description),
    bbox filter (collections whose extent intersects bbox), sort, and pagination.
    When has_static_tiles=True, only collections that have static tiles built are returned.
    When only_public=True, only collections with visibility=public are returned (e.g. for processing).
    Otherwise: admin sees all; anon sees public; logged sees public+logged+owned+shared.
    Returns (collections, total_count).
    """
    base = select(Collection)
    count_base = select(func.count()).select_from(Collection)

    if has_static_tiles:
        static_ids = select(CollectionTiles.collection_id).where(
            CollectionTiles.pmtiles_path.isnot(None)
        )
        base = base.where(Collection.id.in_(static_ids))
        count_base = count_base.where(Collection.id.in_(static_ids))
    if collection_type in (COLLECTION_TYPE_VECTOR, COLLECTION_TYPE_RASTER, COLLECTION_TYPE_COMPOSITE):
        base = base.where(Collection.collection_type == collection_type)
        count_base = count_base.where(Collection.collection_type == collection_type)

    # Visibility: only_public forces public; else admin sees all; anon sees public; logged sees public+logged+owned+shared
    if only_public:
        base = base.where(Collection.visibility == VISIBILITY_PUBLIC)
        count_base = count_base.where(Collection.visibility == VISIBILITY_PUBLIC)
    elif current_user is not None and current_user.is_admin:
        pass
    elif current_user is None:
        base = base.where(Collection.visibility == VISIBILITY_PUBLIC)
        count_base = count_base.where(Collection.visibility == VISIBILITY_PUBLIC)
    else:
        from app.models.resource_share import RESOURCE_TYPE_COLLECTION
        share_exists = (
            select(1)
            .where(ResourceShare.resource_type == RESOURCE_TYPE_COLLECTION)
            .where(ResourceShare.resource_id == Collection.id)
            .where(ResourceShare.username == current_user.username)
        )
        visible = or_(
            Collection.visibility.in_([VISIBILITY_PUBLIC, VISIBILITY_LOGGED]),
            Collection.owner_id == current_user.id,
            exists(share_exists),
        )
        base = base.where(visible)
        count_base = count_base.where(visible)

    if q and q.strip():
        pattern = f"%{_like_escape(q.strip())}%"
        q_clause = or_(
            Collection.id.ilike(pattern, escape="\\"),
            Collection.title.ilike(pattern, escape="\\"),
            Collection.description.ilike(pattern, escape="\\"),
        )
        base = base.where(q_clause)
        count_base = count_base.where(q_clause)

    bbox_params: dict[str, float] = {}
    if bbox_tuple is not None:
        minx, miny, maxx, maxy = bbox_tuple
        bbox_cond = text(
            "collections.extent IS NOT NULL AND (collections.extent->'bbox'->0) IS NOT NULL "
            "AND (collections.extent->'bbox'->0->>0)::float <= :req_maxx "
            "AND (collections.extent->'bbox'->0->>2)::float >= :req_minx "
            "AND (collections.extent->'bbox'->0->>1)::float <= :req_maxy "
            "AND (collections.extent->'bbox'->0->>3)::float >= :req_miny"
        )
        bbox_params = {"req_minx": minx, "req_miny": miny, "req_maxx": maxx, "req_maxy": maxy}
        base = base.where(bbox_cond)
        count_base = count_base.where(bbox_cond)

    total = (await db.execute(count_base, bbox_params)).scalar() or 0
    base = base.order_by(_order_by_collection(sortby, sortdesc))
    if limit is not None:
        base = base.limit(limit)
    if offset > 0:
        base = base.offset(offset)
    result = await db.execute(base, bbox_params)
    rows = result.scalars().all()
    return (rows, int(total))


async def get_collection(db: AsyncSession, collection_id: str) -> Collection | None:
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id)
    )
    return result.scalar_one_or_none()


async def get_collection_titles_by_ids(db: AsyncSession, collection_ids: list[str]) -> dict[str, str]:
    """Return id -> human-readable label: non-empty title when set, else collection id."""
    if not collection_ids:
        return {}
    uniq = list(dict.fromkeys(collection_ids))
    result = await db.execute(
        select(Collection.id, Collection.title).where(Collection.id.in_(uniq))
    )
    out: dict[str, str] = {}
    for row in result.all():
        t = (row.title or "").strip()
        out[row.id] = t if t else row.id
    return out


def _row_to_extent(row: Any) -> Extent:
    return Extent(
        bbox=[[float(row.minx), float(row.miny), float(row.maxx), float(row.maxy)]],
        crs="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
    )


async def get_collection_bbox_from_features(
    db: AsyncSession, collection_id: str
) -> Extent | None:
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


async def recompute_and_update_collection_extent(
    db: AsyncSession, collection_id: str
) -> Extent | None:
    """
    Compute extent from feature geometries, update the collection's stored extent, and return it.
    Returns None if the collection has no features with geometry (stored extent is set to None).
    """
    collection = await get_collection(db, collection_id)
    if collection is None:
        return None
    extent = await get_collection_bbox_from_features(db, collection_id)
    collection.extent = extent.model_dump() if extent else None
    await db.commit()
    await db.refresh(collection)
    return extent


def recompute_and_update_collection_extent_sync(engine: Engine, collection_id: str) -> None:
    """
    Sync variant: compute extent from feature geometries and update the collection's stored extent.
    Same database effect as POST /collections/{id}/extent/recompute.

    Use from workers after bulk import or process jobs finish writing features.
    No-op if the collection has no features with geometry (stored extent becomes null).

    Uses a single transaction (engine.begin()) so the UPDATE is committed reliably.
    """
    log = logging.getLogger(__name__)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("""
                    SELECT ST_XMin(e) AS minx, ST_YMin(e) AS miny, ST_XMax(e) AS maxx, ST_YMax(e) AS maxy
                    FROM (SELECT ST_Extent(geometry) AS e FROM features WHERE collection_id = :cid AND geometry IS NOT NULL) t
                """),
                {"cid": collection_id},
            ).first()
            if row is None or row.minx is None:
                conn.execute(
                    text("UPDATE collections SET extent = NULL WHERE id = :cid"),
                    {"cid": collection_id},
                )
            else:
                extent_json = json.dumps({
                    "bbox": [[float(row.minx), float(row.miny), float(row.maxx), float(row.maxy)]],
                    "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
                })
                conn.execute(
                    text("UPDATE collections SET extent = CAST(:extent AS jsonb) WHERE id = :cid"),
                    {"cid": collection_id, "extent": extent_json},
                )
    except Exception:
        log.warning("recompute_and_update_collection_extent_sync failed for %s", collection_id, exc_info=True)


async def get_collections_bboxes(db: AsyncSession) -> dict[str, Extent]:
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


def _composite_members_json(members: list | None) -> list[dict[str, str]] | None:
    if not members:
        return None
    out: list[dict[str, str]] = []
    for m in members:
        cid = m.collection_id if hasattr(m, "collection_id") else m.get("collection_id")
        if cid:
            out.append({"collection_id": str(cid).strip()})
    return out or None


def _normalize_collection_type(value: str | None) -> str:
    if value in (COLLECTION_TYPE_VECTOR, COLLECTION_TYPE_RASTER, COLLECTION_TYPE_COMPOSITE):
        return value
    return COLLECTION_TYPE_VECTOR


async def create_collection(
    db: AsyncSession,
    data: CollectionCreate,
    *,
    owner_id: int | None = None,
    visibility: str = "private",
) -> Collection:
    ctype = _normalize_collection_type(data.collection_type)
    members_json = _composite_members_json(data.composite_members) if ctype == COLLECTION_TYPE_COMPOSITE else None
    index_fields = None
    if ctype == COLLECTION_TYPE_VECTOR and data.property_index_fields:
        index_fields = normalize_property_index_fields(data.property_index_fields) or None
    collection = Collection(
        id=data.id,
        title=data.title,
        description=data.description,
        extent=data.extent.model_dump() if data.extent else None,
        stac_source=data.stac_source,
        raster_settings=data.raster_settings,
        owner_id=owner_id,
        visibility=visibility,
        collection_type=ctype,
        composite_members=members_json,
        property_index_fields=index_fields,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    if ctype != COLLECTION_TYPE_COMPOSITE:
        await ensure_features_partition(db, data.id)
    if index_fields:
        await asyncio.to_thread(
            sync_collection_property_indexes_sync,
            data.id,
            [],
            index_fields,
        )
    return collection


async def replace_collection(
    db: AsyncSession, collection_id: str, data: CollectionReplace
) -> Collection | None:
    collection = await get_collection(db, collection_id)
    if collection is None:
        return None
    old_index_fields = normalize_property_index_fields(collection.property_index_fields)
    collection.title = data.title
    collection.description = data.description
    collection.extent = data.extent.model_dump() if data.extent else None
    collection.stac_source = data.stac_source
    collection.raster_settings = data.raster_settings
    collection.collection_type = _normalize_collection_type(data.collection_type)
    if collection.collection_type == COLLECTION_TYPE_COMPOSITE:
        collection.composite_members = _composite_members_json(data.composite_members)
    else:
        collection.composite_members = None
    if collection.collection_type == COLLECTION_TYPE_VECTOR:
        collection.property_index_fields = (
            normalize_property_index_fields(data.property_index_fields) or None
        )
    else:
        collection.property_index_fields = None
    await db.commit()
    await db.refresh(collection)
    if collection.collection_type == COLLECTION_TYPE_VECTOR:
        await asyncio.to_thread(
            sync_collection_property_indexes_sync,
            collection_id,
            old_index_fields,
            normalize_property_index_fields(collection.property_index_fields),
        )
    elif old_index_fields:
        await asyncio.to_thread(
            drop_all_collection_property_indexes_sync,
            collection_id,
            old_index_fields,
        )
    return collection


async def patch_collection(
    db: AsyncSession, collection_id: str, data: CollectionPatch
) -> Collection | None:
    collection = await get_collection(db, collection_id)
    if collection is None:
        return None
    old_index_fields = normalize_property_index_fields(collection.property_index_fields)
    sync_indexes = False
    if "title" in data.model_fields_set:
        collection.title = data.title
    if "description" in data.model_fields_set:
        collection.description = data.description
    if "extent" in data.model_fields_set:
        collection.extent = data.extent.model_dump() if data.extent else None
    if "visibility" in data.model_fields_set and data.visibility is not None:
        from app.models.collection import VISIBILITY_LOGGED, VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
        if data.visibility in (VISIBILITY_PRIVATE, VISIBILITY_LOGGED, VISIBILITY_PUBLIC):
            collection.visibility = data.visibility
    if "viewer_can_edit" in data.model_fields_set and data.viewer_can_edit is not None:
        collection.viewer_can_edit = data.viewer_can_edit
    if "stac_source" in data.model_fields_set:
        collection.stac_source = data.stac_source
    if "raster_settings" in data.model_fields_set:
        collection.raster_settings = data.raster_settings
    if "collection_type" in data.model_fields_set and data.collection_type:
        collection.collection_type = _normalize_collection_type(data.collection_type)
    if "composite_members" in data.model_fields_set:
        collection.composite_members = _composite_members_json(data.composite_members)
    if collection.collection_type != COLLECTION_TYPE_COMPOSITE:
        collection.composite_members = None
    if "property_index_fields" in data.model_fields_set:
        sync_indexes = True
        if collection.collection_type == COLLECTION_TYPE_VECTOR:
            collection.property_index_fields = (
                normalize_property_index_fields(data.property_index_fields) or None
            )
        else:
            collection.property_index_fields = None
    await db.commit()
    await db.refresh(collection)
    if sync_indexes:
        if collection.collection_type == COLLECTION_TYPE_VECTOR:
            await asyncio.to_thread(
                sync_collection_property_indexes_sync,
                collection_id,
                old_index_fields,
                normalize_property_index_fields(collection.property_index_fields),
            )
        elif old_index_fields:
            await asyncio.to_thread(
                drop_all_collection_property_indexes_sync,
                collection_id,
                old_index_fields,
            )
    return collection


async def delete_collection(db: AsyncSession, collection_id: str) -> bool:
    collection = await get_collection(db, collection_id)
    if collection is None:
        return False
    index_fields = normalize_property_index_fields(collection.property_index_fields)
    if index_fields:
        await asyncio.to_thread(
            drop_all_collection_property_indexes_sync,
            collection_id,
            index_fields,
        )
    # Delete static MBTiles file if present (before collection_tiles row is CASCADE-deleted)
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if rec and rec.pmtiles_path:
        try:
            if os.path.isfile(rec.pmtiles_path):
                os.unlink(rec.pmtiles_path)
        except OSError:
            pass
    await styles_crud.delete_collection_styles(db, collection_id)
    await db.delete(collection)
    await db.commit()
    return True
