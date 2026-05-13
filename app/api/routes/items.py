import asyncio
import json
from pathlib import Path as PathLib
from urllib.parse import urlencode

import orjson
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Path, Query, Request, Response, status, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import collection_tiles as tiles_crud
from app.utils.geo import mvt_layer_name
from app.api.deps import get_current_user_optional
from app.core.permissions import can_edit_collection, can_see_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.crud import styles as styles_crud
from app.db.session import get_db, AsyncSessionLocal
from app.models.feature import Feature
from app.services.coverages import CogPathOutsideStorageError
from app.services.bulk_import import list_shp_in_zip
from app.services.bulk_queue import BulkJobPayload, enqueue, register_bulk_import_job
from app.services.bulk_storage import get_bulk_storage
from app.services.bulk_upload_sessions import (
    add_uploaded_part,
    create_upload_session,
    delete_upload_session,
    get_upload_session,
)
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
from app.utils.geo import bbox_from_geometries, geometry_to_geojson
from app.utils.datetime_parse import parse_datetime_param
from app.services.collection_type_guard import ensure_vector_collection
from app.utils.property_filters import parse_filter_param

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# Reserved query params for items list (not attribute filters). Include "f" for ?f=html (HTML view).
ITEMS_RESERVED_PARAMS = {"limit", "offset", "bbox", "datetime", "sortby", "sortdesc", "properties", "filter", "q", "f", "bbox_only"}


def _feature_to_read(
    feature: Feature,
    properties_include: set[str] | None = None,
) -> FeatureRead:
    """Build FeatureRead from ORM Feature. properties_include: if set, only these keys in properties.
    Uses feature.geometry_geojson when set (list path) to avoid WKT→GeoJSON conversion per feature.
    When feature.bbox is set (fast list path), geometry is omitted."""
    geom_dict = None
    if getattr(feature, "bbox", None) is None:
        geom_dict = getattr(feature, "geometry_geojson", None) or geometry_to_geojson(feature.geometry)
    props = feature.properties
    if properties_include is not None and props:
        props = {k: v for k, v in props.items() if k in properties_include}
    bbox = getattr(feature, "bbox", None)
    return FeatureRead(
        id=feature.id,
        collection_id=feature.collection_id,
        type="Feature",
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=props,
        created_at=feature.created_at,
        updated_at=feature.updated_at,
        bbox=bbox,
    )


@router.get(
    "/{collection_id}/items",
    summary="List items (features) for a collection",
    description="OGC API Features: limit, offset, bbox, datetime, sortby, sortdesc; filter=key:op:value; q=full-text; properties. Use ?f=html for HTML (search form, map).",
)
async def list_items(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    limit: int | None = Query(None, ge=1, le=get_settings().items_max_limit, description="Max features per page (OGC limit)."),
    offset: int = Query(0, ge=0, description="Number of features to skip (OGC offset)."),
    bbox: str | None = Query(None, description="Bounding box: minx,miny,maxx,maxy (WGS84)."),
    datetime_param: str | None = Query(None, alias="datetime", description="Instant or range (e.g. 2024-01-01 or 2024-01-01/2024-12-31). Filters by feature created_at."),
    sortby: str | None = Query(None, description="Sort by attribute: id, created_at, or a property name."),
    sortdesc: bool = Query(False, description="Sort descending."),
    properties_include: str | None = Query(None, alias="properties", description="Comma-separated property names to return (attribute selection)."),
    filter_param: list[str] | None = Query(None, alias="filter", description="Structured filters: key:op:value (op: eq, ne, gt, gte, lt, lte, like, ilike). Repeat for AND."),
    q: str | None = Query(None, description="Full-text search across all property values."),
    bbox_only: bool = Query(False, description="If true, return only { bbox, numberMatched } for the same query (no features)."),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    # Server-side cache for repeated identical GeoJSON queries (bbox, q, filters, etc.).
    # Only applies to JSON (not HTML) and non-bbox_only responses.
    settings = get_settings()
    # HTML view: default to 10 items when limit not specified (single-line search)
    if wants_html(request) and "limit" not in request.query_params:
        limit = 10
    else:
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
    # Structured filters (filter=key:op:value). Allow newline-separated from HTML form.
    if filter_param:
        filter_param = [x for s in filter_param for x in s.strip().split("\n") if x.strip()]
    structured_filters = parse_filter_param(filter_param) if filter_param else []
    # Full-text search (q) requires at least 4 characters to avoid slow queries on trigram index.
    FULLTEXT_MIN_LENGTH = 4
    if q and q.strip():
        if len(q.strip()) < FULLTEXT_MIN_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Full-text search (q) requires at least {FULLTEXT_MIN_LENGTH} characters.",
            )
    fulltext_q = q.strip() if q and q.strip() else None

    # Cache lookup (after permission check, before DB query).
    if not wants_html(request) and not bbox_only:
        try:
            from app.services.dynamic_tile_cache import _params_key_from_query, get_items_list

            params_key = _params_key_from_query(
                limit=limit,
                offset=offset,
                sortby=sortby,
                sortdesc=sortdesc,
                bbox=bbox,
                datetime_param=datetime_param,
                filter_param=filter_param,
                q=q,
                ids=None,
                properties=properties_include,
            )
            cached = get_items_list(collection_id, params_key)
            if cached:
                return Response(content=cached, media_type="application/geo+json")
        except Exception:
            pass

    # Legacy attribute filters: any query param not reserved (name=value, * for partial)
    property_filters: dict[str, str] = {}
    if request.query_params:
        for key, value in request.query_params.items():
            if key.lower() not in ITEMS_RESERVED_PARAMS and value is not None:
                property_filters[key] = value

    props_include_set: set[str] | None = None
    if properties_include:
        props_include_set = {p.strip() for p in properties_include.split(",") if p.strip()}
    # HTML view: skip full geometry for vector (bbox only); raster needs footprints on the map.
    collection_type = getattr(collection, "collection_type", "vector") or "vector"
    is_raster = collection_type == "raster"
    include_geometry = (wants_html(request) and is_raster) or (not wants_html(request))
    skip_count = wants_html(request)
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
        collection_feature_count=collection.feature_count,
        include_geometry=include_geometry,
        skip_count=skip_count,
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
    read_list = [_feature_to_read(f, props_include_set) for f in features]
    # Build GeoJSON features once (used for extent, cache, HTML, and JSON response)
    features_geojson = []
    bboxes_only: list[list[float]] = []
    for r in read_list:
        props = dict(r.properties) if r.properties else {}
        if r.id is not None and "id" not in props:
            props["id"] = r.id
        feat = {
            "type": "Feature",
            "id": r.id,
            "geometry": r.geometry.model_dump() if r.geometry else None,
            "properties": props,
        }
        if getattr(r, "bbox", None) is not None:
            feat["bbox"] = r.bbox
            bboxes_only.append(r.bbox)
        features_geojson.append(feat)
    if bboxes_only:
        extent_bbox = [
            min(b[0] for b in bboxes_only),
            min(b[1] for b in bboxes_only),
            max(b[2] for b in bboxes_only),
            max(b[3] for b in bboxes_only),
        ]
    else:
        extent_bbox = bbox_from_geometries([f["geometry"] for f in features_geojson])
    if bbox_only:
        return Response(
            content=json.dumps({"bbox": extent_bbox, "numberMatched": number_matched}),
            media_type="application/json",
        )
    # Warm search result cache for dynamic tiler (queue mode); only when we have geometry (not bbox-only)
    if get_settings().tiles_dynamic_use_queue and include_geometry:
        from app.services.dynamic_tile_cache import _params_key_from_query, set_search_result
        params_key = _params_key_from_query(
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox=bbox,
            datetime_param=datetime_param,
            filter_param=filter_param,
            q=q,
            ids=None,
            properties=properties_include,
        )
        set_search_result(
            collection_id,
            params_key,
            json.dumps({"type": "FeatureCollection", "features": features_geojson}).encode("utf-8"),
        )
    if wants_html(request):
        property_keys = sorted(
            set().union(*(set((r.properties or {}).keys()) for r in read_list))
        )
        items_url_json = base_path + ("?" + request.url.query if request.url.query else "")
        if "f=html" in (request.url.query or ""):
            from urllib.parse import parse_qs
            qs = parse_qs(request.url.query or "")
            qs.pop("f", None)
            items_url_json = base_path + ("?" + urlencode(qs, doseq=True) if qs else "")
        # Pagination URLs for HTML (preserve all query params, change offset)
        query_params = dict(request.query_params)
        query_params["f"] = "html"
        query_params["limit"] = str(limit)
        prev_page_url = None
        next_page_url = None
        if offset > 0:
            q_prev = {**query_params, "offset": str(max(0, offset - limit))}
            prev_page_url = base_path + "?" + urlencode(sorted(q_prev.items()))
        if offset + len(features) < number_matched:
            q_next = {**query_params, "offset": str(offset + limit)}
            next_page_url = base_path + "?" + urlencode(sorted(q_next.items()))
        # Fetch style and tiles in parallel (no dependency on features)
        default_style, rec = await asyncio.gather(
            styles_crud.get_default_style(db, collection_id),
            tiles_crud.get_collection_tiles(db, collection_id),
        )
        default_style_dict = (
            {"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec}
            if default_style
            else None
        )
        has_static_tiles = bool(rec and rec.pmtiles_path and PathLib(rec.pmtiles_path).exists())
        can_edit = await can_edit_collection(db, collection, current_user)
        return html_response(
            "items.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            collection_id=collection_id,
            can_edit_collection=can_edit,
            features=read_list,
            features_geojson=features_geojson,
            extent_bbox=extent_bbox,
            property_keys=property_keys,
            number_matched=number_matched,
            number_returned=len(features),
            limit=limit,
            limit_max_value=get_settings().items_max_limit,
            offset=offset,
            bbox=bbox,
            datetime_param=datetime_param,
            sortby=sortby,
            sortdesc=sortdesc,
            filter_param="\n".join(filter_param) if filter_param else "",
            q=q or "",
            properties=properties_include or "",
            items_url_json=items_url_json,
            prev_page_url=prev_page_url,
            next_page_url=next_page_url,
            default_style=default_style_dict,
            has_static_tiles=has_static_tiles,
            tile_layer_id=mvt_layer_name(collection_id),
            google_maps_api_key=get_settings().google_maps_api_key or "",
            collection_type=collection_type,
        )
    fc = FeatureCollection(
        features=read_list,
        bbox=extent_bbox,
        numberMatched=number_matched,
        numberReturned=len(features),
        links=links + [Link(href=f"{base_path}?f=html", rel="alternate", type="text/html")],
    )
    payload = fc.model_dump(mode="json")
    # Cache store best-effort (JSON only).
    if not wants_html(request):
        try:
            from app.services.dynamic_tile_cache import _params_key_from_query, set_items_list

            params_key = _params_key_from_query(
                limit=limit,
                offset=offset,
                sortby=sortby,
                sortdesc=sortdesc,
                bbox=bbox,
                datetime_param=datetime_param,
                filter_param=filter_param,
                q=q,
                ids=None,
                properties=properties_include,
            )
            set_items_list(collection_id, params_key, orjson.dumps(payload))
        except Exception:
            pass
    return GeoJSONResponse(content=payload)


@router.get(
    "/{collection_id}/queryables",
    summary="List queryable property keys for a collection",
    description="Returns property names that can be used in filter=key:op:value. Used by the filter builder UI.",
)
async def get_collection_queryables(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    """Return distinct property keys for the collection (for building filter lines)."""
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    keys = await features_crud.get_collection_property_keys(db, collection_id)
    return {"properties": keys}


@router.post(
    "/{collection_id}/items/bulk/sessions",
    status_code=status.HTTP_201_CREATED,
    summary="Create resumable bulk upload session",
)
async def create_bulk_upload_session(
    collection_id: str,
    request: Request,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    mode = str(body.get("mode") or "append")
    if mode not in ("append", "replace"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be 'append' or 'replace'")
    settings = get_settings()
    batch_size = int(body.get("batch_size") or settings.bulk_import_batch_size)
    if batch_size < 1 or batch_size > 100_000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="batch_size out of range")
    filename = str(body.get("filename") or "upload.geojson")
    qt = bool(body.get("queue_compute_tiles", True))
    s = create_upload_session(
        collection_id=collection_id,
        owner_id=current_user.id if current_user else None,
        filename=filename,
        mode=mode,
        batch_size=batch_size,
        queue_compute_tiles=qt,
    )
    return {
        "upload_id": s["upload_id"],
        "status": s["status"],
        "chunk_size_bytes": settings.bulk_upload_chunk_size_bytes,
        "expires_in_seconds": settings.bulk_upload_session_ttl_seconds,
        "parts_uploaded": [],
        "complete_url": f"{_base_url(request)}/collections/{collection_id}/items/bulk/sessions/{s['upload_id']}/complete",
    }


@router.put(
    "/{collection_id}/items/bulk/sessions/{upload_id}/parts/{part_no}",
    summary="Upload one resumable chunk part",
)
async def upload_bulk_session_part(
    collection_id: str,
    upload_id: str,
    part_no: int = Path(..., ge=1),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if s.get("owner_id") is not None and current_user and int(s.get("owner_id")) != int(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session owner mismatch")
    storage = get_bulk_storage()
    part_path = storage.get_chunk_part_path(upload_id, part_no)
    try:
        with open(part_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed writing upload part: {e}") from e
    s2 = add_uploaded_part(upload_id, part_no)
    return {"upload_id": upload_id, "part_no": part_no, "parts_uploaded": sorted(s2.get("parts") if s2 else [part_no])}


@router.post(
    "/{collection_id}/items/bulk/sessions/{upload_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Complete resumable upload and enqueue ingest",
)
async def complete_bulk_upload_session(
    request: Request,
    collection_id: str,
    upload_id: str,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    parts = [int(p) for p in (body.get("parts") or s.get("parts") or [])]
    if not parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No uploaded parts")

    settings = get_settings()
    filename = str(s.get("filename") or "upload.geojson")
    suffix = PathLib(filename).suffix.lower()
    if suffix not in (".kml", ".gpkg", ".geojson", ".json", ".geojsonl", ".geojsonseq", ".jsonl", ".zip"):
        suffix = ".geojson"
    job = create_job(collection_id, owner_id=current_user.id if current_user else None)
    storage_key = f"{job.job_id}{suffix}"
    storage = get_bulk_storage()
    try:
        write_path = storage.assemble_chunk_parts(upload_id, parts, storage_key)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed assembling upload: {e}") from e
    finally:
        storage.delete_upload_parts(upload_id)
        delete_upload_session(upload_id)

    zip_inner_shp_paths: list[str] | None = None
    if suffix == ".zip":
        try:
            shp_list = list_shp_in_zip(write_path)
            if shp_list:
                zip_inner_shp_paths = shp_list
        except Exception:
            pass

    payload = BulkJobPayload(
        job_id=job.job_id,
        collection_id=collection_id,
        owner_id=current_user.id if current_user else None,
        storage_key=storage_key,
        mode=str(s.get("mode") or "append"),
        batch_size=int(s.get("batch_size") or settings.bulk_import_batch_size),
        queue_compute_tiles=bool(s.get("queue_compute_tiles", True)),
        zip_inner_shp_paths=zip_inner_shp_paths,
    )
    if bool(settings.bulk_sharded_ingest_enabled):
        payload.job_kind = "parent"
        payload.finalize_collection = True
    try:
        register_bulk_import_job(job.job_id, storage_key)
        enqueue(payload)
    except Exception as e:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Bulk queue unavailable: {e}") from e

    base = _base_url(request)
    return {"job_id": job.job_id, "message": "Bulk import queued.", "status_url": f"{base}/jobs/{job.job_id}"}


@router.delete(
    "/{collection_id}/items/bulk/sessions/{upload_id}",
    summary="Abort resumable bulk upload session",
)
async def abort_bulk_upload_session(
    collection_id: str,
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    storage = get_bulk_storage()
    storage.delete_upload_parts(upload_id)
    delete_upload_session(upload_id)
    return {"status": "aborted", "upload_id": upload_id}


@router.post(
    "/{collection_id}/items/bulk",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Bulk import from geospatial file",
    description="Upload a file (KML, GPKG, GeoJSON, GeoJSONSeq/.geojsonl/.geojsonseq, or .zip). A .zip may contain one or more shapefiles (.shp and sidecars); all .shp found inside the zip (including in subfolders) are imported into the collection. Import runs asynchronously. Use mode=append or replace. Returns job_id and status_url.",
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
    queue_compute_tiles: str | None = Form(
        "true",
        description="If true (default), queue a static tile build for this collection after the bulk import completes.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")

    if mode not in ("append", "replace"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be 'append' or 'replace'")

    settings = get_settings()
    batch = batch_size if batch_size is not None else settings.bulk_import_batch_size

    job = create_job(collection_id, owner_id=current_user.id if current_user else None)
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

    zip_inner_shp_paths: list[str] | None = None
    if suffix == ".zip":
        try:
            shp_list = list_shp_in_zip(write_path)
            if shp_list:
                zip_inner_shp_paths = shp_list
        except Exception:
            pass

    qt = queue_compute_tiles is None or str(queue_compute_tiles).lower() not in ("false", "0", "no", "")
    try:
        register_bulk_import_job(job.job_id, storage_key)
        payload = BulkJobPayload(
            job_id=job.job_id,
            collection_id=collection_id,
            owner_id=current_user.id if current_user else None,
            storage_key=storage_key,
            mode=mode,
            batch_size=batch,
            queue_compute_tiles=qt,
            zip_inner_shp_paths=zip_inner_shp_paths,
        )
        if bool(settings.bulk_sharded_ingest_enabled):
            payload.job_kind = "parent"
            payload.finalize_collection = True
        enqueue(payload)
    except Exception as e:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Bulk queue unavailable after retries: {e}",
        ) from e

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
    "/{collection_id}/items/new",
    summary="Add feature form (HTML only)",
    description="Use ?f=html to get a form to create a new feature (geometry + properties).",
)
async def new_item_form(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html for the add-feature form.")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    base = _base_url(request)
    default_style = await styles_crud.get_default_style(db, collection_id)
    default_style_dict = (
        {"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec}
        if default_style
        else None
    )
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    has_static_tiles = bool(rec and rec.pmtiles_path and PathLib(rec.pmtiles_path).exists())
    return html_response(
        "add_feature.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        collection_id=collection_id,
        default_style=default_style_dict,
        has_static_tiles=has_static_tiles,
        tile_layer_id=mvt_layer_name(collection_id),
        google_maps_api_key=get_settings().google_maps_api_key or "",
    )


@router.get(
    "/{collection_id}/items/data",
    summary="Download items as GeoJSONL",
    description="Stream matching collection features as line-delimited GeoJSON. Supports bbox, datetime, filter, q, properties, and legacy attribute filters.",
)
async def download_items_data(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    bbox: str | None = Query(None, description="Bounding box: minx,miny,maxx,maxy (WGS84)."),
    datetime_param: str | None = Query(None, alias="datetime", description="Instant or range (e.g. 2024-01-01 or 2024-12-31/2025-01-31). Filters by feature created_at."),
    properties_include: str | None = Query(None, alias="properties", description="Comma-separated property names to return."),
    filter_param: list[str] | None = Query(None, alias="filter", description="Structured filters: key:op:value (repeat for AND)."),
    q: str | None = Query(None, description="Full-text search across all property values."),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    # Restrict GeoJSONL downloads to logged-in users to avoid anonymous bulk export.
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
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
    if filter_param:
        filter_param = [x for s in filter_param for x in s.strip().split("\n") if x.strip()]
    structured_filters = parse_filter_param(filter_param) if filter_param else []
    if q and q.strip() and len(q.strip()) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Full-text search (q) requires at least 4 characters.",
        )
    fulltext_q = q.strip() if q and q.strip() else None
    property_filters: dict[str, str] = {}
    if request.query_params:
        for key, value in request.query_params.items():
            if key.lower() not in ITEMS_RESERVED_PARAMS and value is not None:
                property_filters[key] = value
    props_include_set: set[str] | None = None
    if properties_include:
        props_include_set = {p.strip() for p in properties_include.split(",") if p.strip()}

    # Larger batches = fewer DB round-trips; buffer output for bigger TCP chunks = higher throughput.
    _GEOJSONL_BATCH_SIZE = 2000
    _GEOJSONL_CHUNK_TARGET = 256 * 1024  # 256 KB per yield for good throughput, still low RAM

    async def _iter_geojsonl():
        # Use a dedicated session so we don't hold the request-scoped connection for the
        # whole download; close it explicitly so the pool never leaks (fixes GC warning).
        session = AsyncSessionLocal()
        try:
            gen = features_crud.stream_features_geojsonl(
                session,
                collection_id,
                bbox=bbox_tuple,
                datetime_start=dt_start,
                datetime_end=dt_end,
                property_filters=property_filters or None,
                structured_filters=structured_filters or None,
                fulltext_q=fulltext_q,
                batch_size=_GEOJSONL_BATCH_SIZE,
            )
            try:
                buf: list[bytes] = []
                buf_size = 0
                async for row in gen:
                    props = dict(row.properties) if row.properties else {}
                    if props_include_set is not None:
                        props = {k: v for k, v in props.items() if k in props_include_set}
                    if row.id is not None and "id" not in props:
                        props["id"] = row.id
                    line = orjson.dumps({
                        "type": "Feature",
                        "id": row.id,
                        "geometry": row.geometry_geojson,
                        "properties": props,
                    }) + b"\n"
                    buf.append(line)
                    buf_size += len(line)
                    if buf_size >= _GEOJSONL_CHUNK_TARGET:
                        yield b"".join(buf)
                        buf.clear()
                        buf_size = 0
                if buf:
                    yield b"".join(buf)
            finally:
                await gen.aclose()
        finally:
            await session.close()

    safe_filename = "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in collection_id) or "collection"
    return StreamingResponse(
        _iter_geojsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}.geojsonl"'},
    )


@router.get(
    "/{collection_id}/items/{feature_id}",
    summary="Get a feature by id (GeoJSON). Use ?f=html for HTML (map, edit, delete).",
)
async def get_item(
    request: Request,
    collection_id: str,
    feature_id: str = Path(..., description="Identifier of the feature."),
    bbox_only: bool = Query(False, description="If true, return only { bbox } for this feature's geometry."),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feature not found")
    geom_dict = geometry_to_geojson(feature.geometry)
    if bbox_only:
        bbox = bbox_from_geometries([geom_dict])
        return Response(
            content=json.dumps({"bbox": bbox}),
            media_type="application/json",
        )
    base = _base_url(request)
    feat_geojson = FeatureGeoJSON(
        type="Feature",
        id=feature.id,
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature_id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}", rel="collection", type="application/json"),
        ],
    )
    if wants_html(request):
        default_style = await styles_crud.get_default_style(db, collection_id)
        default_style_dict = (
            {"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec}
            if default_style
            else None
        )
        rec = await tiles_crud.get_collection_tiles(db, collection_id)
        has_static_tiles = bool(rec and rec.pmtiles_path and PathLib(rec.pmtiles_path).exists())
        can_edit = await can_edit_collection(db, collection, current_user)
        collection_type_item = getattr(collection, "collection_type", "vector") or "vector"
        return html_response(
            "item.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            collection_id=collection_id,
            can_edit_collection=can_edit,
            feature=feat_geojson,
            feature_geojson=feat_geojson.model_dump(),
            properties_json=json.dumps(feat_geojson.properties or {}, indent=2),
            default_style=default_style_dict,
            has_static_tiles=has_static_tiles,
            tile_layer_id=mvt_layer_name(collection_id),
            google_maps_api_key=get_settings().google_maps_api_key or "",
            collection_type=collection_type_item,
        )
    return GeoJSONResponse(content=feat_geojson.model_dump(mode="json"))


@router.get(
    "/{collection_id}/items/{feature_id}/edit",
    summary="Edit feature (HTML only)",
    description="Use ?f=html to open the feature edit page (map editor, properties, save).",
)
async def get_item_edit(
    request: Request,
    collection_id: str,
    feature_id: str = Path(..., description="Identifier of the feature."),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html for the edit page.")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    geom_dict = geometry_to_geojson(feature.geometry)
    base = _base_url(request)
    feat_geojson = FeatureGeoJSON(
        type="Feature",
        id=feature.id,
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature_id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}", rel="collection", type="application/json"),
        ],
    )
    default_style = await styles_crud.get_default_style(db, collection_id)
    default_style_dict = (
        {"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec}
        if default_style
        else None
    )
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    has_static_tiles = bool(rec and rec.pmtiles_path and PathLib(rec.pmtiles_path).exists())
    return html_response(
        "item_edit.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        collection_id=collection_id,
        feature=feat_geojson,
        feature_geojson=feat_geojson.model_dump(),
        properties_json=json.dumps(feat_geojson.properties or {}, indent=2),
        default_style=default_style_dict,
        has_static_tiles=has_static_tiles,
        tile_layer_id=mvt_layer_name(collection_id),
        google_maps_api_key=get_settings().google_maps_api_key or "",
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
    current_user=Depends(get_current_user_optional),
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
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    try:
        updated = await features_crud.replace_feature(db, collection_id, feature_id, payload)
    except CogPathOutsideStorageError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
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
    current_user=Depends(get_current_user_optional),
) -> FeatureGeoJSON:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    try:
        feature = await features_crud.update_feature(db, collection_id, feature_id, payload)
    except CogPathOutsideStorageError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
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
    current_user=Depends(get_current_user_optional),
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
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")

    try:
        feature = await features_crud.create_feature(db, payload)
    except CogPathOutsideStorageError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
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
    current_user=Depends(get_current_user_optional),
) -> Response:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    deleted = await features_crud.delete_feature(db, collection_id, feature_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feature not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

