from __future__ import annotations

import asyncio
from collections.abc import Sequence, AsyncGenerator
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Tuple

from geoalchemy2.elements import WKTElement
from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope, ST_MakePoint, ST_SetSRID
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from sqlalchemy import Float, and_, cast, func, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import get_settings
from app.models.collection import Collection
from app.models.feature import Feature
from app.schemas.feature import FeatureCreate, FeaturePatch, FeatureReplace
from app.utils.geo import geojson_to_wkt_element
from app.utils.geometry_limits import check_geometry_size_limit
from app.utils.feature_subdivide import (
    MAX_COORDS_FOR_DB_SUBDIVIDE,
    _coord_count,
    insert_feature_parts_batched,
    insert_feature_subdivided_sql,
    subdivide_geometry_by_vertices,
)
from app.db.feature_property_filters import property_filter_clause, structured_filter_clause
from app.utils.property_filters import PropertyFilter, safe_json_key
from app.services.coverages import CogPathOutsideStorageError, resolve_stored_cog_path
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.shadow_import import active_shadow_exclude_job_ids, shadow_distinct_on_order, shadow_read_where_sql

# Properties key that must not be writable via API (feature id is from the resource)
PROPERTIES_READONLY_KEYS = frozenset({"id"})


def _bbox_spatial_predicate(envelope):
    """GiST-friendly bbox filter: && uses index bounds, then ST_Intersects refines (see process_worker)."""
    return (
        Feature.geometry.isnot(None)
        & Feature.geometry.op("&&")(envelope)
        & ST_Intersects(Feature.geometry, envelope)
    )


def _properties_without_readonly(props: dict | None) -> dict | None:
    """Return a copy of properties with readonly keys (e.g. id) removed. None in -> None out."""
    if props is None:
        return None
    return {k: v for k, v in props.items() if k not in PROPERTIES_READONLY_KEYS}


def _validate_raster_cog_path_in_properties(props: dict | None, storage_root: str) -> None:
    """Reject ``raster.cog_path`` values that escape ``raster_storage_path``."""
    if not props:
        return
    raster = props.get("raster")
    if not isinstance(raster, dict):
        return
    cp = raster.get("cog_path")
    if isinstance(cp, str) and cp.strip():
        resolve_stored_cog_path(cp, storage_root)


def _parts_to_logical_feature(parts: list[Any]) -> Feature:
    """Aggregate part-rows (id, collection_id, part_index, geometry_geojson, properties, created_at, updated_at)
    into one logical Feature. Geometry = unary_union of parts in Python (no DB union)."""
    if not parts:
        raise ValueError("parts must be non-empty")
    sorted_parts = sorted(parts, key=lambda p: getattr(p, "part_index", 0))
    first = sorted_parts[0]
    geoms = []
    for p in sorted_parts:
        g = getattr(p, "geometry_geojson", None)
        if g:
            try:
                # Depending on DB driver/cast, ST_AsGeoJSON may come as a string or a dict.
                # Parse strings to GeoJSON dict before handing to shapely.
                if isinstance(g, str):
                    g = json.loads(g)
                geoms.append(shape(g))
            except Exception:
                pass
    if geoms:
        try:
            union_geom = unary_union(geoms)
        except Exception:
            # Fallback: try to make each geom valid individually, then union again
            fixed = []
            for gg in geoms:
                try:
                    if not gg.is_valid:
                        from shapely.validation import make_valid
                        gg = make_valid(gg)
                    if not gg.is_empty:
                        fixed.append(gg)
                except Exception:
                    continue
            union_geom = unary_union(fixed) if fixed else None
        geometry_geojson = mapping(union_geom) if union_geom and not union_geom.is_empty else None
    else:
        geometry_geojson = None
    created_at = min(getattr(p, "created_at", None) for p in sorted_parts)
    updated_at = max(getattr(p, "updated_at", None) for p in sorted_parts)
    f = Feature(
        id=first.id,
        collection_id=first.collection_id,
        part_index=0,
        geometry=None,
        properties=first.properties,
        created_at=created_at,
        updated_at=updated_at,
    )
    f.geometry_geojson = geometry_geojson  # type: ignore[attr-defined]
    return f


def _row_to_logical_feature(row, geometry_geojson: bool = False) -> Feature:
    """Build a Feature (logical view, part_index=0) from a grouped query row.
    When geometry_geojson is True, row has geometry_geojson (dict) and we set it on the
    instance to avoid WKT→GeoJSON conversion in the route (list path)."""
    geom_wkt = getattr(row, "geometry_wkt", None)
    geom_geojson = getattr(row, "geometry_geojson", None)
    if geometry_geojson and geom_geojson is not None:
        geom = None  # Route will use feature.geometry_geojson
    else:
        geom = WKTElement(geom_wkt, srid=4326) if geom_wkt else None
    f = Feature(
        id=row.id,
        collection_id=row.collection_id,
        part_index=0,
        geometry=geom,
        properties=row.properties,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    if geometry_geojson and geom_geojson is not None:
        f.geometry_geojson = geom_geojson  # type: ignore[attr-defined]
    return f


# Max property keys to return for queryables (UI filter builder)
QUERYABLES_KEYS_LIMIT = 500
# Sample logical features instead of scanning the whole collection (car_area_imovel scale).
QUERYABLES_SAMPLE_FEATURES = 100


async def get_collection_property_keys(db: AsyncSession, collection_id: str) -> list[str]:
    """Return top-level property keys from a sample of features (for filter builder / queryables)."""
    r = await db.execute(
        text("""
            SELECT DISTINCT k.key
            FROM (
                SELECT DISTINCT ON (id) properties
                FROM features
                WHERE collection_id = :cid
                  AND properties IS NOT NULL
                ORDER BY id
                LIMIT :sample_limit
            ) AS sample
            CROSS JOIN LATERAL jsonb_object_keys(sample.properties) AS k(key)
            ORDER BY 1
            LIMIT :limit
        """),
        {
            "cid": collection_id,
            "sample_limit": QUERYABLES_SAMPLE_FEATURES,
            "limit": QUERYABLES_KEYS_LIMIT,
        },
    )
    return [row[0] for row in r.fetchall()]


async def list_features_for_collection(
    db: AsyncSession, collection_id: str
) -> Sequence[Feature]:
    """Return one logical feature per id (ST_Union of parts)."""
    r = await db.execute(
        text("""
            SELECT id, collection_id,
                   ST_AsText(ST_Union(geometry)) AS geometry_wkt,
                   (array_agg(properties ORDER BY part_index))[1] AS properties,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM features WHERE collection_id = :cid
            GROUP BY id, collection_id
        """),
        {"cid": collection_id},
    )
    return [_row_to_logical_feature(row) for row in r.fetchall()]


async def find_raster_feature_id_at_point(
    db: AsyncSession,
    collection_id: str,
    lon: float,
    lat: float,
) -> str | None:
    """Return one raster feature id whose geometry contains (lon, lat), or None."""
    point = ST_SetSRID(ST_MakePoint(lon, lat), 4326)
    stmt = (
        select(Feature.id)
        .where(
            Feature.collection_id == collection_id,
            Feature.geometry.isnot(None),
            ST_Intersects(Feature.geometry, point),
        )
        .distinct()
        .limit(1)
    )
    r = await db.execute(stmt)
    row = r.first()
    return str(row[0]) if row else None


async def list_raster_feature_rows_for_collection(
    db: AsyncSession, collection_id: str
) -> Sequence[SimpleNamespace]:
    """Return one logical row per raster id with lightweight geometry/properties for mosaic assembly.

    Uses grouped SQL with ST_Envelope instead of ST_Union to avoid expensive per-id union work
    when collections have many subdivided parts.
    """
    r = await db.execute(
        text("""
            SELECT id, collection_id,
                   (array_agg(properties ORDER BY part_index))[1] AS properties,
                   CAST(ST_AsGeoJSON(ST_Envelope(ST_Extent(geometry))) AS jsonb) AS geometry_geojson,
                   min(created_at) AS created_at,
                   max(updated_at) AS updated_at
            FROM features
            WHERE collection_id = :cid
            GROUP BY id, collection_id
            ORDER BY id
        """),
        {"cid": collection_id},
    )
    rows = []
    for row in r.fetchall():
        rows.append(
            SimpleNamespace(
                id=row.id,
                collection_id=row.collection_id,
                properties=row.properties,
                geometry_geojson=row.geometry_geojson,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return rows


def _geojsonl_export_base_stmt(
    collection_id: str,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    datetime_start: datetime | None = None,
    datetime_end: datetime | None = None,
    property_filters: dict[str, str] | None = None,
    structured_filters: Sequence[PropertyFilter] | None = None,
    fulltext_q: str | None = None,
):
    """Build base SELECT for GeoJSONL export (id, geometry_geojson, properties) with filters applied."""
    envelope = None
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        envelope = ST_MakeEnvelope(minx, miny, maxx, maxy, 4326)
    stmt = (
        select(
            Feature.id,
            Feature.collection_id,
            Feature.part_index,
            cast(func.ST_AsGeoJSON(Feature.geometry), JSONB).label("geometry_geojson"),
            Feature.properties,
        )
        .where(Feature.collection_id == collection_id)
    )
    if envelope is not None:
        stmt = stmt.where(_bbox_spatial_predicate(envelope))
    if datetime_start is not None:
        stmt = stmt.where(Feature.created_at >= datetime_start)
    if datetime_end is not None:
        stmt = stmt.where(Feature.created_at <= datetime_end)
    if property_filters:
        for key, value in property_filters.items():
            stmt = stmt.where(_property_filter_clause(key, value))
    if structured_filters:
        for pf in structured_filters:
            stmt = stmt.where(_structured_filter_clause(pf))
    if fulltext_q and fulltext_q.strip():
        q = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{q}%"
        stmt = stmt.where(Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\"))
    return stmt.order_by(Feature.id.asc(), Feature.part_index.asc())


async def stream_features_geojsonl(
    db: AsyncSession,
    collection_id: str,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    datetime_start: datetime | None = None,
    datetime_end: datetime | None = None,
    property_filters: dict[str, str] | None = None,
    structured_filters: Sequence[PropertyFilter] | None = None,
    fulltext_q: str | None = None,
    batch_size: int = 500,
) -> AsyncGenerator[Any, None]:
    """Stream raw feature parts for GeoJSONL export using keyset pagination.

    Fetches in small batches (default 500 rows) so the server never holds a large
    result set in memory. Connection is only used for short queries and can be
    closed between batches. Yields one row at a time for immediate streaming.
    """
    # For bbox exports, stream logical features (one per id) by:
    #   1) selecting matching unique ids in bbox
    #   2) fetching all parts for those ids
    #   3) unioning in Python
    # This avoids partial polygons when a logical feature is split into DB parts.
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        envelope = ST_MakeEnvelope(minx, miny, maxx, maxy, 4326)
        ids_base = select(Feature.id).where(
            Feature.collection_id == collection_id,
            _bbox_spatial_predicate(envelope),
        )
        if datetime_start is not None:
            ids_base = ids_base.where(Feature.created_at >= datetime_start)
        if datetime_end is not None:
            ids_base = ids_base.where(Feature.created_at <= datetime_end)
        if property_filters:
            for key, value in property_filters.items():
                ids_base = ids_base.where(_property_filter_clause(key, value))
        if structured_filters:
            for pf in structured_filters:
                ids_base = ids_base.where(_structured_filter_clause(pf))
        if fulltext_q and fulltext_q.strip():
            q = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{q}%"
            ids_base = ids_base.where(Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\"))
        ids_base = ids_base.distinct().order_by(Feature.id.asc())

        last_id: str | None = None
        batch_size = max(1, min(batch_size, 2000))
        while True:
            ids_stmt = ids_base.limit(batch_size)
            if last_id is not None:
                ids_stmt = ids_stmt.where(Feature.id > last_id)
            ids_result = await db.execute(ids_stmt)
            id_rows = ids_result.fetchall()
            if not id_rows:
                break
            batch_ids = [r.id for r in id_rows]
            parts_stmt = (
                select(
                    Feature.id,
                    Feature.collection_id,
                    Feature.part_index,
                    func.ST_AsGeoJSON(Feature.geometry).label("geometry_geojson"),
                    Feature.properties,
                    Feature.created_at,
                    Feature.updated_at,
                )
                .where(Feature.collection_id == collection_id, Feature.id.in_(batch_ids))
                .order_by(Feature.id.asc(), Feature.part_index.asc())
            )
            parts_result = await db.execute(parts_stmt)
            parts_rows = parts_result.fetchall()
            grouped: dict[str, list[Any]] = {}
            for row in parts_rows:
                grouped.setdefault(row.id, []).append(row)
            for fid in batch_ids:
                parts = grouped.get(fid)
                if not parts:
                    continue
                logical = _parts_to_logical_feature(parts)
                yield SimpleNamespace(
                    id=logical.id,
                    geometry_geojson=getattr(logical, "geometry_geojson", None),
                    properties=logical.properties,
                )
            last_id = batch_ids[-1]
        return

    base = _geojsonl_export_base_stmt(
        collection_id,
        bbox=bbox,
        datetime_start=datetime_start,
        datetime_end=datetime_end,
        property_filters=property_filters,
        structured_filters=structured_filters,
        fulltext_q=fulltext_q,
    )
    last_id: str | None = None
    last_part: int | None = None
    batch_size = max(1, min(batch_size, 2000))

    while True:
        stmt = base.limit(batch_size)
        if last_id is not None and last_part is not None:
            stmt = stmt.where(
                or_(
                    Feature.id > last_id,
                    and_(Feature.id == last_id, Feature.part_index > last_part),
                )
            )
        result = await db.execute(stmt)
        rows = result.fetchall()
        if not rows:
            break
        for row in rows:
            yield row
        last_row = rows[-1]
        last_id = last_row.id
        last_part = getattr(last_row, "part_index", 0)


def _order_by_clause(sortby: str | None, sortdesc: bool):
    """Build ORDER BY: id, created_at, or properties->>'sortby'. Uses column when possible for index."""
    if not sortby:
        return Feature.id.asc()
    if sortby == "id":
        return Feature.id.desc() if sortdesc else Feature.id.asc()
    if sortby == "created_at":
        return Feature.created_at.desc() if sortdesc else Feature.created_at.asc()
    # JSONB property: order by properties->>sortby (string comparison)
    col = Feature.properties[sortby].astext
    return col.desc() if sortdesc else col.asc()


def _property_filter_clause(key: str, value: str):
    return property_filter_clause(key, value)


def _structured_filter_clause(f: PropertyFilter):
    return structured_filter_clause(f)


async def list_features_paginated(
    db: AsyncSession,
    collection_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    bbox: tuple[float, float, float, float] | None = None,
    datetime_start: datetime | None = None,
    datetime_end: datetime | None = None,
    sortby: str | None = None,
    sortdesc: bool = False,
    property_filters: dict[str, str] | None = None,
    structured_filters: Sequence[PropertyFilter] | None = None,
    fulltext_q: str | None = None,
    feature_ids: Sequence[str] | None = None,
    collection_feature_count: int | None = None,
    include_geometry: bool = True,
    skip_count: bool = False,
    exclude_bulk_job_ids: Sequence[str] | None = None,
) -> Tuple[Sequence[Feature], int]:
    """
    List features with OGC query params. Returns (features, numberMatched).
    include_geometry=False: return features with bbox only (no geometry) for fast list/HTML view with large layers.
    skip_count=True: do not run COUNT query; use collection_feature_count when no filters, else 0. Speeds up HTML view.
    property_filters: legacy name=value (* partial). structured_filters: key:op:value (eq, ne, gt, gte, lt, lte, like, ilike).
    fulltext_q: search term across all properties (uses properties_flat trigram index).
    feature_ids: when set, only return features with id in this list (e.g. for single-item tile).
    When no filters are applied and collection_feature_count is provided, use it as total (no COUNT query).
    When filters are applied, count matching rows.
    """
    # Empty query params (e.g. sortby=) can arrive as ""; treat as None so we use the fast two-phase path
    sortby = (sortby.strip() if sortby else None) or None

    shadow_jobs = [j for j in (exclude_bulk_job_ids or []) if j]
    shadow_clause = ""
    shadow_params: dict[str, list[str]] = {}
    if shadow_jobs:
        shadow_clause, shadow_param = shadow_read_where_sql()
        shadow_params[shadow_param] = shadow_jobs

    has_filters = (
        bbox is not None
        or datetime_start is not None
        or datetime_end is not None
        or bool(property_filters)
        or bool(structured_filters)
        or bool(fulltext_q and fulltext_q.strip())
        or bool(feature_ids)
    )

    envelope = None
    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        envelope = ST_MakeEnvelope(minx, miny, maxx, maxy, 4326)
    if fulltext_q and fulltext_q.strip():
        q = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{q}%"

    # Count: logical features (COUNT(DISTINCT id)); uses idx_features_collection_id_id when no filters.
    count_distinct = select(func.count(func.distinct(Feature.id))).where(Feature.collection_id == collection_id)
    if shadow_jobs:
        count_distinct = count_distinct.where(
            (Feature.bulk_import_job_id.is_(None)) | (~Feature.bulk_import_job_id.in_(shadow_jobs))
        )
    if feature_ids:
        count_distinct = count_distinct.where(Feature.id.in_(list(feature_ids)))
    if bbox is not None:
        count_distinct = count_distinct.where(_bbox_spatial_predicate(envelope))
    if datetime_start is not None:
        count_distinct = count_distinct.where(Feature.created_at >= datetime_start)
    if datetime_end is not None:
        count_distinct = count_distinct.where(Feature.created_at <= datetime_end)
    if property_filters:
        for key, value in property_filters.items():
            count_distinct = count_distinct.where(_property_filter_clause(key, value))
    if structured_filters:
        for pf in structured_filters:
            count_distinct = count_distinct.where(_structured_filter_clause(pf))
    if fulltext_q and fulltext_q.strip():
        q = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{q}%"
        count_distinct = count_distinct.where(Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\"))

    if skip_count and not has_filters and collection_feature_count is not None:
        total = int(collection_feature_count)
    elif skip_count and has_filters:
        total = (await db.execute(count_distinct)).scalar() or 0
    elif skip_count:
        total = 0
    elif has_filters:
        total = (await db.execute(count_distinct)).scalar() or 0
    elif collection_feature_count is not None:
        total = collection_feature_count
    else:
        total = (await db.execute(count_distinct)).scalar() or 0

    # List: one logical feature per id. Always use two-phase: no ST_Union in DB.
    # Phase 1: get page of ids (order by id, created_at, or property — no geometry).
    # Phase 2: fetch parts for those ids, union/aggregate in Python.
    use_two_phase = True

    if use_two_phase:
        # Phase 1: get page of logical feature ids only (no ST_Union, no array_agg).
        # No filters + sortby id/None: use DISTINCT ON (id) so planner reads only (limit+offset) rows from index.
        # With filters or sortby created_at: use grouped/distinct query (filtered set is smaller).
        has_any_filter = (
            bool(feature_ids)
            or bbox is not None
            or datetime_start is not None
            or datetime_end is not None
            or bool(property_filters)
            or bool(structured_filters)
            or bool(fulltext_q and fulltext_q.strip())
        )
        if (
            not has_any_filter
            and sortby in (None, "id")
        ):
            # Fast path: DISTINCT ON (id) with LIMIT so planner reads only (limit+offset) rows from index,
            # not the whole table. Inner query emits one row per id in order; we take a slice for the page.
            order_dir = "DESC" if sortdesc else "ASC"
            fetch_count = limit + offset  # read this many "first row per id" from index
            distinct_order = shadow_distinct_on_order(sortdesc) if shadow_jobs else f"id {order_dir}, part_index {order_dir}"
            shadow_where = f"\n                        {shadow_clause}" if shadow_jobs else ""
            r1 = await db.execute(
                text(f"""
                    SELECT id, collection_id
                    FROM (
                        SELECT DISTINCT ON (id) id, collection_id
                        FROM features
                        WHERE collection_id = :cid{shadow_where}
                        ORDER BY {distinct_order}
                        LIMIT :fetch
                    ) sub
                    ORDER BY id {order_dir}
                    LIMIT :lim OFFSET :off
                """),
                {"cid": collection_id, "fetch": fetch_count, "lim": limit, "off": offset, **shadow_params},
            )
            page_rows = r1.fetchall()
            page_ids = [r.id for r in page_rows]
        elif not has_any_filter and sortby == "created_at":
            # Fast path: only id, collection_id, created_at — matches idx_features_collection_created_at_id
            # so planner can use index-only scan (no heap). Still full index scan for GROUP BY + ORDER BY.
            order_dir = "DESC" if sortdesc else "ASC"
            shadow_where = f" AND (bulk_import_job_id IS NULL OR bulk_import_job_id != ALL(:shadow_exclude_jobs))" if shadow_jobs else ""
            r1 = await db.execute(
                text(f"""
                    SELECT id, collection_id
                    FROM (
                        SELECT id, collection_id, min(created_at) AS created_at
                        FROM features
                        WHERE collection_id = :cid{shadow_where}
                        GROUP BY id, collection_id
                        ORDER BY created_at {order_dir}, id {order_dir}
                        LIMIT :lim OFFSET :off
                    ) sub
                """),
                {"cid": collection_id, "lim": limit, "off": offset, **shadow_params},
            )
            page_rows = r1.fetchall()
            page_ids = [r.id for r in page_rows]
        else:
            # Phase 1 with filters and/or sortby: get page_ids (no ST_Union).
            # IMPORTANT: page by unique *logical* IDs first, then fetch all parts for those IDs in Phase 2.
            # This prevents "partial"/split GeoJSON when features are stored as subdivided parts.
            if sortby == "created_at":
                page_ids_stmt = (
                    select(
                        Feature.id,
                        Feature.collection_id,
                        func.min(Feature.created_at).label("created_at"),
                    )
                    .where(Feature.collection_id == collection_id)
                    .group_by(Feature.id, Feature.collection_id)
                )
                order_page_ids = (
                    literal_column("created_at").asc() if not sortdesc else literal_column("created_at").desc()
                )
            elif sortby and sortby != "id":
                # Sort by a property key: order by props->>key (where props is the first properties JSON among parts).
                key = safe_json_key(sortby)
                if key:
                    page_ids_stmt = (
                        select(
                            Feature.id,
                            Feature.collection_id,
                            literal_column(
                                "(array_agg(features.properties ORDER BY features.part_index))[1]"
                            ).label("props"),
                        )
                        .where(Feature.collection_id == collection_id)
                        .group_by(Feature.id, Feature.collection_id)
                    )
                    order_prop = literal_column(f"props ->> '{key}'")
                    order_page_ids = order_prop.asc() if not sortdesc else order_prop.desc()
                else:
                    page_ids_stmt = (
                        select(Feature.id)
                        .where(Feature.collection_id == collection_id)
                        .distinct()
                    )
                    order_page_ids = Feature.id.asc() if not sortdesc else Feature.id.desc()
            else:
                page_ids_stmt = (
                    select(Feature.id)
                    .where(Feature.collection_id == collection_id)
                    .distinct()
                )
                order_page_ids = Feature.id.asc() if not sortdesc else Feature.id.desc()

            if feature_ids:
                page_ids_stmt = page_ids_stmt.where(Feature.id.in_(list(feature_ids)))
            if bbox is not None:
                page_ids_stmt = page_ids_stmt.where(_bbox_spatial_predicate(envelope))
            if datetime_start is not None:
                page_ids_stmt = page_ids_stmt.where(Feature.created_at >= datetime_start)
            if datetime_end is not None:
                page_ids_stmt = page_ids_stmt.where(Feature.created_at <= datetime_end)
            if property_filters:
                for key, value in property_filters.items():
                    page_ids_stmt = page_ids_stmt.where(_property_filter_clause(key, value))
            if structured_filters:
                for pf in structured_filters:
                    page_ids_stmt = page_ids_stmt.where(_structured_filter_clause(pf))
            if fulltext_q and fulltext_q.strip():
                page_ids_stmt = page_ids_stmt.where(
                    Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\")
                )
            if shadow_jobs:
                page_ids_stmt = page_ids_stmt.where(
                    (Feature.bulk_import_job_id.is_(None)) | (~Feature.bulk_import_job_id.in_(shadow_jobs))
                )

            page_ids_stmt = page_ids_stmt.order_by(order_page_ids).limit(limit).offset(offset)
            result1 = await db.execute(page_ids_stmt)
            page_rows = result1.fetchall()
            page_ids = [r.id for r in page_rows]

        if not page_ids:
            return ([], int(total))

        if not include_geometry:
            # Fast path: fetch per-part bbox only (no GROUP BY, no ST_Extent in DB — minimal work per row).
            # Aggregate bbox/properties in Python so DB does a simple index scan and releases quickly.
            phase2_shadow = (
                " AND (bulk_import_job_id IS NULL OR bulk_import_job_id != ALL(:shadow_exclude_jobs))"
                if shadow_jobs
                else ""
            )
            r = await db.execute(
                text(f"""
                    SELECT id, collection_id, part_index,
                           ST_XMin(geometry) AS xmin, ST_YMin(geometry) AS ymin,
                           ST_XMax(geometry) AS xmax, ST_YMax(geometry) AS ymax,
                           properties, created_at, updated_at
                    FROM features
                    WHERE collection_id = :cid AND id = ANY(:ids){phase2_shadow}
                    ORDER BY id, part_index
                """),
                {"cid": collection_id, "ids": page_ids, **shadow_params},
            )
            rows = r.fetchall()
            by_id: dict[str, list[Any]] = {}
            for row in rows:
                by_id.setdefault(row.id, []).append(row)
            features = []
            for pid in page_ids:
                parts = by_id.get(pid)
                if not parts:
                    continue
                sorted_parts = sorted(parts, key=lambda p: getattr(p, "part_index", 0))
                first = sorted_parts[0]
                xs = [float(p.xmin) for p in sorted_parts if p.xmin is not None]
                ys = [float(p.ymin) for p in sorted_parts if p.ymin is not None]
                xmaxs = [float(p.xmax) for p in sorted_parts if p.xmax is not None]
                ymaxs = [float(p.ymax) for p in sorted_parts if p.ymax is not None]
                bbox_list = None
                if xs and ys and xmaxs and ymaxs:
                    bbox_list = [min(xs), min(ys), max(xmaxs), max(ymaxs)]
                f = Feature(
                    id=first.id,
                    collection_id=first.collection_id,
                    part_index=0,
                    geometry=None,
                    properties=first.properties,
                    created_at=min(p.created_at for p in sorted_parts),
                    updated_at=max(p.updated_at for p in sorted_parts),
                )
                f.bbox = bbox_list  # type: ignore[attr-defined]
                features.append(f)
            return (features, int(total))

        # Phase 2: fetch parts only, union in Python (same as non-bbox path).
        # Avoids heavy ST_Union in the DB per page; correctness unchanged (full geometry for each logical id).
        # Aggregate to logical features in Python in parallel (union geometries per id).
        parts_stmt = (
            select(
                Feature.id,
                Feature.collection_id,
                Feature.part_index,
                # Fetch raw GeoJSON text; _parts_to_logical_feature() handles str vs dict.
                func.ST_AsGeoJSON(Feature.geometry).label("geometry_geojson"),
                Feature.properties,
                Feature.created_at,
                Feature.updated_at,
            )
            .where(Feature.collection_id == collection_id, Feature.id.in_(page_ids))
            .order_by(Feature.id, Feature.part_index)
        )
        if shadow_jobs:
            parts_stmt = parts_stmt.where(
                (Feature.bulk_import_job_id.is_(None)) | (~Feature.bulk_import_job_id.in_(shadow_jobs))
            )
        result2 = await db.execute(parts_stmt)
        rows = result2.fetchall()
        # Group by id (preserve part order)
        groups: dict[str, list[Any]] = {}
        for r in rows:
            groups.setdefault(r.id, []).append(r)
        # Aggregate each group in parallel (union in Python, no DB)
        def build_one(pid: str) -> Feature | None:
            if pid not in groups:
                return None
            return _parts_to_logical_feature(groups[pid])

        loop = asyncio.get_event_loop()
        results = await asyncio.gather(
            *[loop.run_in_executor(None, build_one, pid) for pid in page_ids]
        )
        features = [f for f in results if f is not None]
        return (features, int(total))


async def get_feature(
    db: AsyncSession, collection_id: str, feature_id: str
) -> Feature | None:
    """Return one logical feature (ST_Union of parts) or None."""
    shadow_jobs = active_shadow_exclude_job_ids(collection_id)
    shadow_where = ""
    params: dict[str, object] = {"cid": collection_id, "fid": feature_id}
    if shadow_jobs:
        shadow_where = " AND (bulk_import_job_id IS NULL OR bulk_import_job_id != ALL(:shadow_exclude_jobs))"
        params["shadow_exclude_jobs"] = shadow_jobs
    r = await db.execute(
        text(f"""
            SELECT id, collection_id, ST_AsText(ST_Union(geometry)) AS geometry_wkt,
                   (array_agg(properties ORDER BY part_index))[1] AS properties,
                   min(created_at) AS created_at, max(updated_at) AS updated_at
            FROM features WHERE collection_id = :cid AND id = :fid{shadow_where}
            GROUP BY id, collection_id
        """),
        params,
    )
    row = r.fetchone()
    return _row_to_logical_feature(row) if row else None


async def create_feature(db: AsyncSession, data: FeatureCreate) -> Feature:
    """Create one logical feature; geometry is subdivided at insert (ST_Subdivide, ≤256 vertices/row)."""
    from shapely.geometry import shape
    from shapely.validation import make_valid

    geom_dict = data.geometry.model_dump() if data.geometry else None
    geom = None
    if geom_dict:
        geom = shape(geom_dict)
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            geom = None
    if geom is not None:
        check_geometry_size_limit(geom)
    props = _properties_without_readonly(data.properties)
    _validate_raster_cog_path_in_properties(props, get_settings().raster_storage_path)
    fid = str(uuid7())
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    if geom is not None and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
        parts = subdivide_geometry_by_vertices(geom, max_vertices)
        wkt_list = [p.wkt for p in parts if p is not None and not p.is_empty]
        for sql, params in insert_feature_parts_batched(fid, data.collection_id, wkt_list, props, now):
            await db.execute(text(sql), params)
    else:
        wkt = geom.wkt if geom is not None else None
        sql, params = insert_feature_subdivided_sql(fid, data.collection_id, wkt, props, now, max_vertices)
        await db.execute(text(sql), params)
    await db.execute(
        update(Collection)
        .where(Collection.id == data.collection_id)
        .values(feature_count=Collection.feature_count + 1)
    )
    await db.commit()
    invalidate_collection_cache(data.collection_id)
    feature = await get_feature(db, data.collection_id, fid)
    assert feature is not None
    return feature


async def create_feature_with_id(db: AsyncSession, data: FeatureCreate, feature_id: str) -> Feature:
    """Like create_feature but uses a caller-supplied id (e.g. raster COG path alignment)."""
    from shapely.geometry import shape
    from shapely.validation import make_valid

    geom_dict = data.geometry.model_dump() if data.geometry else None
    geom = None
    if geom_dict:
        geom = shape(geom_dict)
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            geom = None
    if geom is not None:
        check_geometry_size_limit(geom)
    props = _properties_without_readonly(data.properties)
    _validate_raster_cog_path_in_properties(props, get_settings().raster_storage_path)
    fid = feature_id
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    if geom is not None and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
        parts = subdivide_geometry_by_vertices(geom, max_vertices)
        wkt_list = [p.wkt for p in parts if p is not None and not p.is_empty]
        for sql, params in insert_feature_parts_batched(fid, data.collection_id, wkt_list, props, now):
            await db.execute(text(sql), params)
    else:
        wkt = geom.wkt if geom is not None else None
        sql, params = insert_feature_subdivided_sql(fid, data.collection_id, wkt, props, now, max_vertices)
        await db.execute(text(sql), params)
    await db.execute(
        update(Collection)
        .where(Collection.id == data.collection_id)
        .values(feature_count=Collection.feature_count + 1)
    )
    await db.commit()
    invalidate_collection_cache(data.collection_id)
    feature = await get_feature(db, data.collection_id, fid)
    assert feature is not None
    return feature


async def replace_feature(
    db: AsyncSession, collection_id: str, feature_id: str, data: FeatureReplace
) -> bool:
    """OGC Part 4: Replace feature with full representation. Deletes all parts, inserts with ST_Subdivide."""
    feature = await get_feature(db, collection_id, feature_id)
    if feature is None:
        return False
    await db.execute(text("DELETE FROM features WHERE collection_id = :cid AND id = :fid"), {"cid": collection_id, "fid": feature_id})
    geom_dict = data.geometry.model_dump() if data.geometry else None
    from shapely.geometry import shape
    from shapely.validation import make_valid

    geom = None
    if geom_dict:
        geom = shape(geom_dict)
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            geom = None
    if geom is not None:
        check_geometry_size_limit(geom)
    props = _properties_without_readonly(data.properties)
    _validate_raster_cog_path_in_properties(props, get_settings().raster_storage_path)
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    if geom is not None and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
        parts = subdivide_geometry_by_vertices(geom, max_vertices)
        wkt_list = [p.wkt for p in parts if p is not None and not p.is_empty]
        for sql, params in insert_feature_parts_batched(feature_id, collection_id, wkt_list, props, now):
            await db.execute(text(sql), params)
    else:
        wkt = geom.wkt if geom is not None else None
        sql, params = insert_feature_subdivided_sql(feature_id, collection_id, wkt, props, now, max_vertices)
        await db.execute(text(sql), params)
    await db.commit()
    invalidate_collection_cache(collection_id)
    return True


async def update_feature(
    db: AsyncSession, collection_id: str, feature_id: str, data: FeaturePatch
) -> Feature | None:
    """OGC Part 4: Partial update (merge-patch). Replaces all parts with new geometry/properties and ST_Subdivide."""
    feature = await get_feature(db, collection_id, feature_id)
    if feature is None:
        return None
    # Use exclude_unset so geometry-only / properties-only PATCH is detected reliably (model_fields_set alone can miss nested updates).
    patch_keys = data.model_dump(exclude_unset=True).keys()
    geom_dict = None
    if "geometry" in patch_keys:
        geom_dict = data.geometry.model_dump() if data.geometry is not None else None
    else:
        # Keep current geometry (from logical feature)
        if feature.geometry is not None:
            from app.utils.geo import geometry_to_geojson
            geom_dict = geometry_to_geojson(feature.geometry)
    existing = feature.properties or {}
    incoming = _properties_without_readonly(data.properties) if "properties" in patch_keys else None
    props = {**existing, **(incoming or {})}
    _validate_raster_cog_path_in_properties(props, get_settings().raster_storage_path)
    from shapely.geometry import shape
    from shapely.validation import make_valid

    geom = None
    if geom_dict:
        geom = shape(geom_dict)
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            geom = None
    if geom is not None:
        check_geometry_size_limit(geom)
    await db.execute(text("DELETE FROM features WHERE collection_id = :cid AND id = :fid"), {"cid": collection_id, "fid": feature_id})
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    if geom is not None and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
        parts = subdivide_geometry_by_vertices(geom, max_vertices)
        wkt_list = [p.wkt for p in parts if p is not None and not p.is_empty]
        for sql, params in insert_feature_parts_batched(feature_id, collection_id, wkt_list, props, now):
            await db.execute(text(sql), params)
    else:
        wkt = geom.wkt if geom is not None else None
        sql, params = insert_feature_subdivided_sql(feature_id, collection_id, wkt, props, now, max_vertices)
        await db.execute(text(sql), params)
    await db.commit()
    invalidate_collection_cache(collection_id)
    return await get_feature(db, collection_id, feature_id)


async def delete_feature(
    db: AsyncSession, collection_id: str, feature_id: str
) -> bool:
    """Delete all parts of a logical feature by id. Returns True if deleted."""
    result = await db.execute(
        text("DELETE FROM features WHERE collection_id = :cid AND id = :fid"),
        {"cid": collection_id, "fid": feature_id},
    )
    if result.rowcount == 0:
        return False
    await db.execute(
        update(Collection)
        .where(Collection.id == collection_id)
        .values(feature_count=func.greatest(0, Collection.feature_count - 1))
    )
    await db.commit()
    invalidate_collection_cache(collection_id)
    return True

