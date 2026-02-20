from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature import Feature
from app.schemas.feature import FeatureCreate, FeaturePatch, FeatureReplace
from app.utils.geo import geojson_to_wkt_element


async def list_features_for_collection(
    db: AsyncSession, collection_id: str
) -> Sequence[Feature]:
    result = await db.execute(
        select(Feature).where(Feature.collection_id == collection_id)
    )
    return result.scalars().all()


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
        properties=data.properties,
    )
    db.add(feature)
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
    feature.properties = data.properties
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
        feature.properties = {**existing, **(data.properties or {})}
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
    await db.commit()
    return True

