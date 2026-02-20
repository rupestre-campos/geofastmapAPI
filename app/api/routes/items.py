from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.models.feature import Feature
from app.schemas.feature import (
    FeatureCollection,
    FeatureCreate,
    FeatureGeoJSON,
    FeaturePatch,
    FeatureRead,
    FeatureReplace,
    Geometry,
)
from app.api.responses import GeoJSONResponse
from app.schemas.ogc import Link
from app.utils.geo import geometry_to_geojson

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _feature_to_read(feature: Feature) -> FeatureRead:
    """Build FeatureRead from ORM Feature, converting PostGIS geometry to GeoJSON."""
    geom_dict = geometry_to_geojson(feature.geometry)
    return FeatureRead(
        id=feature.id,
        collection_id=feature.collection_id,
        type="Feature",
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        created_at=feature.created_at,
        updated_at=feature.updated_at,
    )


@router.get(
    "/{collection_id}/items",
    response_model=FeatureCollection,
    response_class=GeoJSONResponse,
    summary="List items (features) for a collection",
)
async def list_items(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> FeatureCollection:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    features = await features_crud.list_features_for_collection(db, collection_id)
    return FeatureCollection(
        features=[_feature_to_read(f) for f in features],
        numberMatched=len(features),
        numberReturned=len(features),
        links=[
            Link(href=f"{base}/collections/{collection_id}/items", rel="self", type="application/geo+json"),
        ],
    )


@router.get(
    "/{collection_id}/items/{feature_id}",
    response_model=FeatureGeoJSON,
    response_class=GeoJSONResponse,
    summary="Get a feature by id within a collection (GeoJSON Feature)",
)
async def get_item(
    request: Request,
    collection_id: str,
    feature_id: str = Path(..., description="Identifier of the feature."),
    db: AsyncSession = Depends(get_db),
) -> FeatureGeoJSON:
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    geom_dict = geometry_to_geojson(feature.geometry)
    base = _base_url(request)
    return FeatureGeoJSON(
        type="Feature",
        id=feature.id,
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature_id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}", rel="collection", type="application/json"),
        ],
    )


@router.put(
    "/{collection_id}/items/{feature_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Replace a feature (OGC Part 4)",
    description="Full replace with GeoJSON Feature. Body id must match path. Returns 204 No Content.",
)
async def replace_item(
    collection_id: str,
    feature_id: str = Path(..., description="Identifier of the feature."),
    payload: FeatureReplace = ...,
    db: AsyncSession = Depends(get_db),
) -> Response:
    if payload.id != feature_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feature id in body must match path",
        )
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    updated = await features_crud.replace_feature(db, collection_id, feature_id, payload)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{collection_id}/items/{feature_id}",
    response_model=FeatureGeoJSON,
    response_class=GeoJSONResponse,
    summary="Partially update a feature (OGC Part 4)",
    description="Merge-patch: send only geometry and/or properties to update. Content-Type: application/merge-patch+json. Returns 200 with full Feature.",
)
async def patch_item(
    request: Request,
    collection_id: str,
    feature_id: str = Path(..., description="Identifier of the feature."),
    payload: FeaturePatch = ...,
    db: AsyncSession = Depends(get_db),
) -> FeatureGeoJSON:
    feature = await features_crud.update_feature(db, collection_id, feature_id, payload)
    if not feature:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    geom_dict = geometry_to_geojson(feature.geometry)
    base = _base_url(request)
    return FeatureGeoJSON(
        type="Feature",
        id=feature.id,
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature_id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}", rel="collection", type="application/json"),
        ],
    )


@router.post(
    "/{collection_id}/items",
    response_model=FeatureRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a feature (non-OGC helper endpoint)",
)
async def create_item(
    collection_id: str,
    payload: FeatureCreate,
    db: AsyncSession = Depends(get_db),
) -> FeatureRead:
    # Ensure path and body collection_id match
    if payload.collection_id != collection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="collection_id in path and body must match",
        )

    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )

    feature = await features_crud.create_feature(db, payload)
    return _feature_to_read(feature)


@router.delete(
    "/{collection_id}/items/{feature_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a feature from a collection",
)
async def delete_item(
    collection_id: str,
    feature_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await features_crud.delete_feature(db, collection_id, feature_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

