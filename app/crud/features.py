from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Tuple

from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope
from sqlalchemy import Float, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection
from app.models.feature import Feature
from app.schemas.feature import FeatureCreate, FeaturePatch, FeatureReplace
from app.utils.geo import geojson_to_wkt_element
from app.utils.property_filter import property_value_to_like_pattern
from app.utils.property_filters import PropertyFilter, PropertyOp

# Properties key that must not be writable via API (feature id is from the resource)
PROPERTIES_READONLY_KEYS = frozenset({"id"})


def _properties_without_readonly(props: dict | None) -> dict | None:
    """Return a copy of properties with readonly keys (e.g. id) removed. None in -> None out."""
    if props is None:
        return None
    return {k: v for k, v in props.items() if k not in PROPERTIES_READONLY_KEYS}


async def list_features_for_collection(
    db: AsyncSession, collection_id: str
) -> Sequence[Feature]:
    result = await db.execute(
        select(Feature).where(Feature.collection_id == collection_id)
    )
    return result.scalars().all()


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
    """Build WHERE clause for one legacy attribute filter (exact or LIKE with *)."""
    prop_col = Feature.properties[key].astext
    pattern, use_like = property_value_to_like_pattern(value)
    if use_like and pattern is not None:
        return prop_col.isnot(None) & prop_col.like(pattern, escape="\\")
    return prop_col == value


def _structured_filter_clause(f: PropertyFilter):
    """Build WHERE clause for one structured filter (key:op:value)."""
    prop_col = Feature.properties[f.key].astext
    value = f.value
    if f.op == PropertyOp.EQ:
        return prop_col == value
    if f.op == PropertyOp.NE:
        return prop_col != value
    if f.op == PropertyOp.LIKE:
        return prop_col.isnot(None) & prop_col.like(value, escape="\\")
    if f.op == PropertyOp.ILIKE:
        return prop_col.isnot(None) & prop_col.ilike(value, escape="\\")
    # Numeric comparison: cast both sides; non-numeric compare as text
    try:
        num_val = float(value)
    except ValueError:
        num_val = None
    if num_val is not None and f.op in (PropertyOp.GT, PropertyOp.GTE, PropertyOp.LT, PropertyOp.LTE):
        num_col = cast(prop_col, Float)
        if f.op == PropertyOp.GT:
            return num_col > num_val
        if f.op == PropertyOp.GTE:
            return num_col >= num_val
        if f.op == PropertyOp.LT:
            return num_col < num_val
        if f.op == PropertyOp.LTE:
            return num_col <= num_val
    # Fallback: compare as text for gt/gte/lt/lte when value is not numeric
    if f.op == PropertyOp.GT:
        return prop_col > value
    if f.op == PropertyOp.GTE:
        return prop_col >= value
    if f.op == PropertyOp.LT:
        return prop_col < value
    if f.op == PropertyOp.LTE:
        return prop_col <= value
    return prop_col == value


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
    collection_feature_count: int | None = None,
) -> Tuple[Sequence[Feature], int]:
    """
    List features with OGC query params. Returns (features, numberMatched).
    property_filters: legacy name=value (* partial). structured_filters: key:op:value (eq, ne, gt, gte, lt, lte, like, ilike).
    fulltext_q: search term across all properties (uses properties_flat trigram index).
    When no filters are applied and collection_feature_count is provided, use it as total (no COUNT query).
    When filters are applied, count matching rows.
    """
    has_filters = (
        bbox is not None
        or datetime_start is not None
        or datetime_end is not None
        or bool(property_filters)
        or bool(structured_filters)
        or bool(fulltext_q and fulltext_q.strip())
    )

    base = select(Feature).where(Feature.collection_id == collection_id)
    count_base = select(func.count()).select_from(Feature).where(Feature.collection_id == collection_id)

    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        envelope = ST_MakeEnvelope(minx, miny, maxx, maxy, 4326)
        spatial_filter = Feature.geometry.isnot(None) & ST_Intersects(Feature.geometry, envelope)
        base = base.where(spatial_filter)
        count_base = count_base.where(spatial_filter)

    if datetime_start is not None:
        base = base.where(Feature.created_at >= datetime_start)
        count_base = count_base.where(Feature.created_at >= datetime_start)
    if datetime_end is not None:
        base = base.where(Feature.created_at <= datetime_end)
        count_base = count_base.where(Feature.created_at <= datetime_end)

    if property_filters:
        for key, value in property_filters.items():
            clause = _property_filter_clause(key, value)
            base = base.where(clause)
            count_base = count_base.where(clause)

    if structured_filters:
        for pf in structured_filters:
            clause = _structured_filter_clause(pf)
            base = base.where(clause)
            count_base = count_base.where(clause)

    if fulltext_q and fulltext_q.strip():
        q = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{q}%"
        # ILIKE on properties_flat uses the trigram index
        base = base.where(Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\"))
        count_base = count_base.where(Feature.properties_flat.isnot(None) & Feature.properties_flat.ilike(pattern, escape="\\"))

    # Count matching rows only when filters are applied; otherwise use collection's cached feature_count.
    if has_filters:
        total = (await db.execute(count_base)).scalar() or 0
    elif collection_feature_count is not None:
        total = collection_feature_count
    else:
        total = (await db.execute(count_base)).scalar() or 0

    base = base.order_by(_order_by_clause(sortby, sortdesc))
    base = base.limit(limit).offset(offset)
    result = await db.execute(base)
    features = result.scalars().all()
    return (features, int(total))


async def get_feature(
    db: AsyncSession, collection_id: str, feature_id: str
) -> Feature | None:
    result = await db.execute(
        select(Feature).where(
            Feature.collection_id == collection_id,
            Feature.id == feature_id,
        )
    )
    return result.scalar_one_or_none()


async def create_feature(db: AsyncSession, data: FeatureCreate) -> Feature:
    geometry_wkt = geojson_to_wkt_element(
        data.geometry.model_dump() if data.geometry else None
    )
    feature = Feature(
        collection_id=data.collection_id,
        geometry=geometry_wkt,
        properties=_properties_without_readonly(data.properties),
    )
    db.add(feature)
    await db.execute(
        update(Collection)
        .where(Collection.id == data.collection_id)
        .values(feature_count=Collection.feature_count + 1)
    )
    await db.commit()
    await db.refresh(feature)
    return feature


async def replace_feature(
    db: AsyncSession, collection_id: str, feature_id: str, data: FeatureReplace
) -> bool:
    """OGC Part 4: Replace feature with full representation. Returns True if updated."""
    feature = await get_feature(db, collection_id, feature_id)
    if feature is None:
        return False
    geometry_wkt = geojson_to_wkt_element(
        data.geometry.model_dump() if data.geometry else None
    )
    feature.geometry = geometry_wkt
    feature.properties = _properties_without_readonly(data.properties)
    await db.commit()
    await db.refresh(feature)
    return True


async def update_feature(
    db: AsyncSession, collection_id: str, feature_id: str, data: FeaturePatch
) -> Feature | None:
    """OGC Part 4: Partial update (merge-patch). Only updates provided fields. Returns updated feature or None."""
    feature = await get_feature(db, collection_id, feature_id)
    if feature is None:
        return None
    if "geometry" in data.model_fields_set:
        feature.geometry = (
            geojson_to_wkt_element(data.geometry.model_dump())
            if data.geometry is not None
            else None
        )
    if "properties" in data.model_fields_set:
        existing = feature.properties or {}
        incoming = _properties_without_readonly(data.properties) or {}
        feature.properties = {**existing, **incoming}
    await db.commit()
    await db.refresh(feature)
    return feature


async def delete_feature(
    db: AsyncSession, collection_id: str, feature_id: str
) -> bool:
    """Delete a feature by id within a collection. Returns True if deleted."""
    feature = await get_feature(db, collection_id, feature_id)
    if feature is None:
        return False

    await db.delete(feature)
    await db.execute(
        update(Collection)
        .where(Collection.id == collection_id)
        .values(feature_count=func.greatest(0, Collection.feature_count - 1))
    )
    await db.commit()
    return True

