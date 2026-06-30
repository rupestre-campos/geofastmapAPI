"""Fetch GeoJSON for a single tile (bbox from z/x/y + item query params). Used by the dynamic tiler worker."""

from __future__ import annotations

import json

from shapely.geometry import shape
from shapely.geometry import box as shapely_box
from geoalchemy2.shape import to_shape
from shapely.geometry import box
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.shadow_import import active_shadow_exclude_job_ids
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.services.composite_collections import is_composite_collection
from app.services.composite_items import (
    composite_feature_to_geojson,
    composite_member_ids,
    list_composite_features_paginated,
)
from app.utils.datetime_parse import parse_datetime_param
from app.utils.property_filters import parse_filter_param
from app.utils.geo import geometry_to_geojson
from app.utils.tile_bbox import tile_bbox_wgs84


def _feature_intersects_bbox(feature, minx: float, miny: float, maxx: float, maxy: float) -> bool:
    """Return True if feature geometry intersects the given WGS84 bbox.
    Uses geometry_geojson when set (list path), else feature.geometry (WKTElement)."""
    geom_geojson = getattr(feature, "geometry_geojson", None)
    if geom_geojson is not None:
        try:
            shp = shape(geom_geojson)
            return shp.intersects(box(minx, miny, maxx, maxy))
        except Exception:
            return False
    if feature.geometry is None:
        return False
    try:
        shp = to_shape(feature.geometry)
        return shp.intersects(box(minx, miny, maxx, maxy))
    except Exception:
        return False


def _feature_to_geojson_feature(feature) -> dict:
    """Convert ORM Feature to GeoJSON Feature dict.
    Uses geometry_geojson when set (list path) to avoid WKT→GeoJSON conversion."""
    geom = getattr(feature, "geometry_geojson", None) or geometry_to_geojson(feature.geometry)
    props = dict(feature.properties) if feature.properties else {}
    if feature.id is not None and "id" not in props:
        props["id"] = feature.id
    return {
        "type": "Feature",
        "id": feature.id,
        "geometry": geom,
        "properties": props,
    }


async def get_geojson_for_tile(
    db: AsyncSession,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    *,
    limit: int | None = None,
    offset: int = 0,
    sortby: str | None = None,
    sortdesc: bool = False,
    bbox_user: tuple[float, float, float, float] | None = None,
    datetime_param: str | None = None,
    filter_param: list[str] | None = None,
    q: str | None = None,
    ids: list[str] | None = None,
    property_filters: dict[str, str] | None = None,
) -> bytes:
    """
    When limit/offset are set (items list pagination): fetch the same page as the table
    (same filters, sort, limit, offset; no tile bbox), then keep only features that
    intersect the tile bbox so the map matches the search results.
    When limit/offset are not set (e.g. single item ids=): fetch features that intersect
    the tile bbox (and match filters/ids). Returns GeoJSON FeatureCollection as UTF-8.
    """
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise ValueError(f"Collection not found: {collection_id}")

    settings = get_settings()
    tile_bbox = tile_bbox_wgs84(z, x, y)
    tile_minx, tile_miny, tile_maxx, tile_maxy = tile_bbox

    dt_start, dt_end = None, None
    if datetime_param:
        dt_start, dt_end = parse_datetime_param(datetime_param)

    filter_list = []
    if filter_param:
        filter_list = [x for s in filter_param for x in s.strip().split("\n") if x.strip()]
    structured_filters = parse_filter_param(filter_list) if filter_list else []
    fulltext_q = q.strip() if q and q.strip() else None
    exclude_bulk_job_ids = active_shadow_exclude_job_ids(collection_id)
    use_page_mode = limit is not None or offset != 0

    if is_composite_collection(collection):
        member_ids = await composite_member_ids(db, collection)
        if use_page_mode:
            if limit is None:
                limit = min(settings.items_default_limit, settings.items_max_limit)
            limit = min(limit, settings.items_max_limit)
            rows, _ = await list_composite_features_paginated(
                db,
                member_ids,
                limit=limit,
                offset=offset,
                bbox=bbox_user,
                datetime_start=dt_start,
                datetime_end=dt_end,
                sortby=sortby,
                sortdesc=sortdesc,
                property_filters=property_filters or None,
                structured_filters=structured_filters or None,
                fulltext_q=fulltext_q,
                feature_ids=ids,
                include_geometry=True,
                skip_count=True,
            )
            geojson_features = [
                composite_feature_to_geojson(mid, f, composite_collection_id=collection_id)
                for mid, f in rows
                if _feature_intersects_bbox(f, tile_minx, tile_miny, tile_maxx, tile_maxy)
            ]
        else:
            if bbox_user is not None:
                minx = max(tile_bbox[0], bbox_user[0])
                miny = max(tile_bbox[1], bbox_user[1])
                maxx = min(tile_bbox[2], bbox_user[2])
                maxy = min(tile_bbox[3], bbox_user[3])
                if minx >= maxx or miny >= maxy:
                    fc = {"type": "FeatureCollection", "features": []}
                    return json.dumps(fc).encode("utf-8")
                bbox = (minx, miny, maxx, maxy)
            else:
                bbox = tile_bbox
            limit = min(settings.items_max_limit, getattr(settings, "tiles_mvt_max_features", 10_000))
            rows, _ = await list_composite_features_paginated(
                db,
                member_ids,
                limit=limit,
                offset=0,
                bbox=bbox,
                datetime_start=dt_start,
                datetime_end=dt_end,
                sortby=sortby,
                sortdesc=sortdesc,
                property_filters=property_filters or None,
                structured_filters=structured_filters or None,
                fulltext_q=fulltext_q,
                feature_ids=ids,
                include_geometry=True,
                skip_count=True,
            )
            geojson_features = [
                composite_feature_to_geojson(mid, f, composite_collection_id=collection_id)
                for mid, f in rows
            ]
        fc = {"type": "FeatureCollection", "features": geojson_features}
        return json.dumps(fc).encode("utf-8")

    if use_page_mode:
        # Fetch the exact same page as GET items: user bbox only, no tile bbox
        if limit is None:
            limit = min(settings.items_default_limit, settings.items_max_limit)
        limit = min(limit, settings.items_max_limit)
        features, _ = await features_crud.list_features_paginated(
            db,
            collection_id,
            limit=limit,
            offset=offset,
            bbox=bbox_user,
            datetime_start=dt_start,
            datetime_end=dt_end,
            sortby=sortby,
            sortdesc=sortdesc,
            property_filters=property_filters or None,
            structured_filters=structured_filters or None,
            fulltext_q=fulltext_q,
            feature_ids=ids,
            collection_feature_count=collection.feature_count,
            exclude_bulk_job_ids=exclude_bulk_job_ids or None,
        )
        # Keep only features that intersect this tile so the map shows the same set as the table
        features = [f for f in features if _feature_intersects_bbox(f, tile_minx, tile_miny, tile_maxx, tile_maxy)]
    else:
        # No pagination: fetch features that intersect the tile (e.g. single item ids= or full collection)
        if bbox_user is not None:
            minx = max(tile_bbox[0], bbox_user[0])
            miny = max(tile_bbox[1], bbox_user[1])
            maxx = min(tile_bbox[2], bbox_user[2])
            maxy = min(tile_bbox[3], bbox_user[3])
            if minx >= maxx or miny >= maxy:
                fc = {"type": "FeatureCollection", "features": []}
                return json.dumps(fc).encode("utf-8")
            bbox = (minx, miny, maxx, maxy)
        else:
            bbox = tile_bbox

        limit = min(settings.items_max_limit, getattr(settings, "tiles_mvt_max_features", 10_000))
        features, _ = await features_crud.list_features_paginated(
            db,
            collection_id,
            limit=limit,
            offset=0,
            bbox=bbox,
            datetime_start=dt_start,
            datetime_end=dt_end,
            sortby=sortby,
            sortdesc=sortdesc,
            property_filters=property_filters or None,
            structured_filters=structured_filters or None,
            fulltext_q=fulltext_q,
            feature_ids=ids,
            collection_feature_count=collection.feature_count,
            exclude_bulk_job_ids=exclude_bulk_job_ids or None,
        )

    geojson_features = [_feature_to_geojson_feature(f) for f in features]
    fc = {"type": "FeatureCollection", "features": geojson_features}
    return json.dumps(fc).encode("utf-8")


def filter_geojson_to_tile_bbox(geojson_bytes: bytes, z: int, x: int, y: int) -> bytes:
    """
    Given a GeoJSON FeatureCollection (full search page), return a FeatureCollection
    containing only features whose geometry intersects the tile bbox (z, x, y).
    Used by queue workers that read cached search result and build one tile.
    """
    tile_bbox = tile_bbox_wgs84(z, x, y)
    minx, miny, maxx, maxy = tile_bbox
    box = shapely_box(minx, miny, maxx, maxy)
    data = json.loads(geojson_bytes.decode("utf-8"))
    features = data.get("features") or []
    out = []
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue
        try:
            shp = shape(geom)
            if shp.intersects(box):
                out.append(f)
        except Exception:
            continue
    return json.dumps({"type": "FeatureCollection", "features": out}).encode("utf-8")


async def get_search_result_geojson(
    db: AsyncSession,
    collection_id: str,
    *,
    limit: int,
    offset: int = 0,
    sortby: str | None = None,
    sortdesc: bool = False,
    bbox_user: tuple[float, float, float, float] | None = None,
    datetime_param: str | None = None,
    filter_param: list[str] | None = None,
    q: str | None = None,
    ids: list[str] | None = None,
    property_filters: dict[str, str] | None = None,
) -> bytes:
    """
    Fetch the same page of features as GET items (no tile filter). Returns GeoJSON
    FeatureCollection bytes for caching. Used so the tiler workers can read from
    Redis and never hit the DB.
    """
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise ValueError(f"Collection not found: {collection_id}")

    settings = get_settings()
    limit = min(limit, settings.items_max_limit)

    dt_start, dt_end = None, None
    if datetime_param:
        dt_start, dt_end = parse_datetime_param(datetime_param)
    filter_list = []
    if filter_param:
        filter_list = [x for s in filter_param for x in s.strip().split("\n") if x.strip()]
    structured_filters = parse_filter_param(filter_list) if filter_list else []
    fulltext_q = q.strip() if q and q.strip() else None
    exclude_bulk_job_ids = active_shadow_exclude_job_ids(collection_id)

    if is_composite_collection(collection):
        member_ids = await composite_member_ids(db, collection)
        rows, _ = await list_composite_features_paginated(
            db,
            member_ids,
            limit=limit,
            offset=offset,
            bbox=bbox_user,
            datetime_start=dt_start,
            datetime_end=dt_end,
            sortby=sortby,
            sortdesc=sortdesc,
            property_filters=property_filters or None,
            structured_filters=structured_filters or None,
            fulltext_q=fulltext_q,
            feature_ids=ids,
            include_geometry=True,
            skip_count=True,
        )
        geojson_features = [
            composite_feature_to_geojson(mid, f, composite_collection_id=collection_id)
            for mid, f in rows
        ]
        fc = {"type": "FeatureCollection", "features": geojson_features}
        return json.dumps(fc).encode("utf-8")

    features, _ = await features_crud.list_features_paginated(
        db,
        collection_id,
        limit=limit,
        offset=offset,
        bbox=bbox_user,
        datetime_start=dt_start,
        datetime_end=dt_end,
        sortby=sortby,
        sortdesc=sortdesc,
        property_filters=property_filters or None,
        structured_filters=structured_filters or None,
        fulltext_q=fulltext_q,
        feature_ids=ids,
        collection_feature_count=collection.feature_count,
        exclude_bulk_job_ids=exclude_bulk_job_ids or None,
    )

    geojson_features = [_feature_to_geojson_feature(f) for f in features]
    fc = {"type": "FeatureCollection", "features": geojson_features}
    return json.dumps(fc).encode("utf-8")
