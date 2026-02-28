import json
from pathlib import Path as PathLib
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, Response, status, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.models.feature import Feature
from app.services.bulk_queue import BulkJobPayload, enqueue
from app.services.bulk_storage import get_bulk_storage
from app.services.job_store import create_job
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
from app.utils.datetime_parse import parse_datetime_param
from app.utils.property_filters import parse_filter_param

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# Reserved query params for items list (not attribute filters)
ITEMS_RESERVED_PARAMS = {"limit", "offset", "bbox", "datetime", "sortby", "sortdesc", "properties", "filter", "q"}


def _feature_to_read(
    feature: Feature,
    properties_include: set[str] | None = None,
) -> FeatureRead:
    """Build FeatureRead from ORM Feature. properties_include: if set, only these keys in properties."""
    geom_dict = geometry_to_geojson(feature.geometry)
    props = feature.properties
    if properties_include is not None and props:
        props = {k: v for k, v in props.items() if k in properties_include}
    return FeatureRead(
        id=feature.id,
        collection_id=feature.collection_id,
        type="Feature",
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=props,
        created_at=feature.created_at,
        updated_at=feature.updated_at,
    )


@router.get(
    "/{collection_id}/items",
    response_model=FeatureCollection,
    response_class=GeoJSONResponse,
    summary="List items (features) for a collection",
    description="OGC API Features: limit, offset, bbox, datetime, sortby, sortdesc; filter=key:op:value (op: eq, ne, gt, gte, lt, lte, like, ilike); q=full-text search; legacy name=value; properties (attribute selection).",
)
async def list_items(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int | None = Query(None, ge=1, le=1000, description="Max features per page (OGC limit)."),
    offset: int = Query(0, ge=0, description="Number of features to skip (OGC offset)."),
    bbox: str | None = Query(None, description="Bounding box: minx,miny,maxx,maxy (WGS84)."),
    datetime_param: str | None = Query(None, alias="datetime", description="Instant or range (e.g. 2024-01-01 or 2024-01-01/2024-12-31). Filters by feature created_at."),
    sortby: str | None = Query(None, description="Sort by attribute: id, created_at, or a property name."),
    sortdesc: bool = Query(False, description="Sort descending."),
    properties_include: str | None = Query(None, alias="properties", description="Comma-separated property names to return (attribute selection)."),
    filter_param: list[str] | None = Query(None, alias="filter", description="Structured filters: key:op:value (op: eq, ne, gt, gte, lt, lte, like, ilike). Repeat for AND."),
    q: str | None = Query(None, description="Full-text search across all property values."),
) -> FeatureCollection:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    settings = get_settings()
    limit = limit if limit is not None else settings.items_default_limit
    limit = min(limit, settings.items_max_limit)
    bbox_tuple: tuple[float, float, float, float] | None = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                pass
    dt_start, dt_end = None, None
    if datetime_param:
        dt_start, dt_end = parse_datetime_param(datetime_param)
    # Structured filters (filter=key:op:value) and full-text (q=)
    structured_filters = parse_filter_param(filter_param) if filter_param else []
    fulltext_q = q.strip() if q and q.strip() else None

    # Legacy attribute filters: any query param not reserved (name=value, * for partial)
    property_filters: dict[str, str] = {}
    if request.query_params:
        for key, value in request.query_params.items():
            if key.lower() not in ITEMS_RESERVED_PARAMS and value is not None:
                property_filters[key] = value

    props_include_set: set[str] | None = None
    if properties_include:
        props_include_set = {p.strip() for p in properties_include.split(",") if p.strip()}
    features, number_matched = await features_crud.list_features_paginated(
        db,
        collection_id,
        limit=limit,
        offset=offset,
        bbox=bbox_tuple,
        datetime_start=dt_start,
        datetime_end=dt_end,
        sortby=sortby,
        sortdesc=sortdesc,
        property_filters=property_filters or None,
        structured_filters=structured_filters or None,
        fulltext_q=fulltext_q,
    )
    base = _base_url(request)
    base_path = f"{base}/collections/{collection_id}/items"
    links: list[Link] = [
        Link(href=base_path, rel="self", type="application/geo+json"),
    ]
    # Next/prev: preserve current query params, only change offset (OGC / QGIS style)
    def _page_href(new_offset: int) -> str:
        q = dict(request.query_params)
        q["offset"] = str(new_offset)
        q["limit"] = str(limit)
        return f"{base_path}?{urlencode(sorted(q.items()))}"
    if offset + len(features) < number_matched:
        links.append(Link(href=_page_href(offset + limit), rel="next", type="application/geo+json"))
    if offset > 0:
        links.append(Link(href=_page_href(max(0, offset - limit)), rel="prev", type="application/geo+json"))
    return FeatureCollection(
        features=[_feature_to_read(f, props_include_set) for f in features],
        numberMatched=number_matched,
        numberReturned=len(features),
        links=links,
    )


@router.post(
    "/{collection_id}/items/bulk",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Bulk import from geospatial file",
    description="Upload a file (KML, GPKG, GeoJSON, GeoJSONSeq/.geojsonl/.geojsonseq, .zip or .shp.zip containing a shapefile). Import runs asynchronously in the background. Use mode=append or replace. Returns job_id and status_url.",
)
async def bulk_import_items(
    request: Request,
    collection_id: str,
    file: UploadFile = File(
        ...,
        description="Geospatial file: .kml, .gpkg, .geojson, .geojsonl, .geojsonseq, .json, .zip or .shp.zip (shapefile inside)",
    ),
    mode: str = Form("append", description="append = add to collection; replace = delete all then import"),
    batch_size: int | None = Form(None, ge=1, le=100_000, description="Features per DB batch (default from config)."),
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    if mode not in ("append", "replace"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be 'append' or 'replace'")

    settings = get_settings()
    batch = batch_size if batch_size is not None else settings.bulk_import_batch_size

    job = create_job(collection_id)
    suffix = PathLib(file.filename or "upload").suffix.lower()
    if suffix not in (".kml", ".gpkg", ".geojson", ".json", ".geojsonl", ".geojsonseq", ".jsonl", ".zip"):
        suffix = ".geojson"
    storage_key = f"{job.job_id}{suffix}"

    storage = get_bulk_storage()
    write_path = storage.get_write_path(storage_key)
    try:
        with open(write_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save upload")

    enqueue(BulkJobPayload(
        job_id=job.job_id,
        collection_id=collection_id,
        storage_key=storage_key,
        mode=mode,
        batch_size=batch,
    ))

    base = _base_url(request)
    return Response(
        status_code=status.HTTP_202_ACCEPTED,
        content=json.dumps({
            "job_id": job.job_id,
            "message": "Bulk import queued.",
            "status_url": f"{base}/jobs/{job.job_id}",
        }),
        media_type="application/json",
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
    summary="Create a feature (OGC Part 4)",
    description="Add a new feature to the collection. Content-Type: application/geo+json. Returns 201 with Location and created Feature.",
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

