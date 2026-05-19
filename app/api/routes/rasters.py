"""Raster collection APIs: upload TIFF(s) and render via Titiler (item or mosaic)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Path as PathParam, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_edit_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.crud import raster_styles as raster_styles_crud
from app.db.session import get_db
from app.services.coverages import CogPathOutsideStorageError, cog_path_for, resolve_stored_cog_path
from app.services.bulk_storage import get_bulk_storage
from app.services.collection_type_guard import ensure_raster_collection
from app.services.job_store import create_job
from app.services.raster_collection_mosaic import get_or_build_collection_mosaic, internal_cog_http_url
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.bulk_upload_sessions import (
    add_uploaded_part,
    create_upload_session,
    delete_upload_session,
    get_upload_session,
)
from app.services.raster_style_spec import titiler_nodata_param
from app.services.raster_titiler_forward import prepare_raster_collection_titiler
from app.services.titiler_point import enrich_point_response, fetch_mosaic_point_with_fallback, fetch_titiler_point_json
from app.services.raster_batch import (
    RasterBatchUploadTooLargeError,
    enqueue_raster_batch_job,
    queue_raster_from_staged_file,
    write_raster_batch_archive,
)
from app.services.raster_mosaic_version import (
    MOSAIC_TILE_CACHE_CONTROL,
    MOSAIC_TILE_CACHE_CONTROL_LEGACY,
    compute_mosaic_version_id,
    mosaic_mv_matches_request,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_DEM_ENCODINGS = frozenset({"terrainrgb", "terrarium"})


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _raster_collection_item_ids(db: AsyncSession, collection_id: str) -> list[str]:
    q = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    return [r.id for r in q.fetchall()]


def _normalize_dem_encoding(value: str | None) -> str:
    enc = (value or "terrainrgb").strip().lower()
    return enc if enc in _DEM_ENCODINGS else "terrainrgb"


def _maplibre_dem_encoding(algorithm: str) -> str:
    # MapLibre uses "mapbox" name for TerrainRGB tiles.
    return "mapbox" if algorithm == "terrainrgb" else "terrarium"


def _style_version_token(style) -> str:
    if not style:
        return "none"
    ts = getattr(style, "updated_at", None)
    sid = str(getattr(style, "id", "") or "")
    if ts is None:
        return sid or "default"
    try:
        # Second-level granularity is enough for cache-busting URL params.
        return f"{sid}:{int(ts.timestamp())}" if sid else str(int(ts.timestamp()))
    except Exception:
        return sid or "default"


def _collection_dem_settings(collection) -> tuple[bool, str]:
    rs = getattr(collection, "raster_settings", None)
    if not isinstance(rs, dict):
        return (False, "terrainrgb")
    return (bool(rs.get("is_dem", False)), _normalize_dem_encoding(rs.get("dem_encoding")))


async def _queue_raster_upload_job(
    *,
    request: Request,
    collection_id: str,
    files: list[UploadFile],
    current_user: User,
    is_dem: bool,
    dem_encoding: str | None,
    source_crs: str | None,
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files uploaded")
    storage = get_bulk_storage()
    job = create_job(collection_id, owner_id=current_user.id, job_label="raster_batch")
    storage_key = f"{job.job_id}.raster_batch.zip"
    dest_path = storage.get_write_path(storage_key)
    try:
        n, _ = await write_raster_batch_archive(
            files=files,
            dest_path=dest_path,
            is_dem=is_dem,
            dem_encoding=dem_encoding,
            source_crs=source_crs,
        )
    except RasterBatchUploadTooLargeError as e:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(e)) from e
    except ValueError as e:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception:
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise
    return enqueue_raster_batch_job(
        request=request,
        collection_id=collection_id,
        current_user=current_user,
        job_id=job.job_id,
        storage_key=storage_key,
        entry_count=n,
    )


def _feature_dem_settings(feature) -> tuple[bool, str]:
    props = getattr(feature, "properties", None) or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if not isinstance(raster, dict):
        return (False, "terrainrgb")
    return (bool(raster.get("is_dem", False)), _normalize_dem_encoding(raster.get("dem_encoding")))


@router.get(
    "/{collection_id}/rasters",
    summary="List raster items and mosaic version metadata for a raster collection",
)
async def list_raster_items(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    base = _base_url(request)
    item_ids = await _raster_collection_item_ids(db, collection_id)
    mosaic_vid = compute_mosaic_version_id(collection_id, item_ids)
    collection_is_dem, collection_dem_encoding = _collection_dem_settings(collection)
    default_style = await raster_styles_crud.get_default_raster_style(db, collection_id)
    style_version = _style_version_token(default_style)
    items = []
    for fid in item_ids:
        f = await features_crud.get_feature(db, collection_id, fid)
        if not f:
            continue
        props = f.properties or {}
        title = props.get("title") if isinstance(props, dict) else None
        is_dem, dem_encoding = _feature_dem_settings(f)
        terrain_on = bool(is_dem or collection_is_dem)
        map_layer: dict = {
            "collection_id": collection_id,
            "raster_tiles": True,
            "raster_collection_mode": "item",
            "raster_feature_id": fid,
            "tiles_url": (
                f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
                f"?mode=item&feature_id={fid}&sv={quote(style_version, safe='')}"
            ),
            "terrain_enabled": terrain_on,
            "terrain_encoding": _maplibre_dem_encoding(dem_encoding if is_dem else collection_dem_encoding),
        }
        if terrain_on:
            map_layer["terrain_raster_overlay"] = True
        items.append(
            {
                "id": fid,
                "title": title or fid,
                "is_dem": is_dem,
                "dem_encoding": dem_encoding,
                "tiles_url": (
                    f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
                    f"?mode=item&feature_id={fid}&sv={quote(style_version, safe='')}"
                ),
                "point_url": (
                    f"{base}/collections/{collection_id}/rasters/point"
                    f"?mode=item&feature_id={fid}&sv={quote(style_version, safe='')}"
                ),
                "delete_url": f"{base}/collections/{collection_id}/items/{fid}",
                "map_layer": map_layer,
            }
        )
    mosaic_url = None
    mosaic_point_url = None
    if item_ids and mosaic_vid:
        mosaic_url = (
            f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
            f"?mode=mosaic&mv={mosaic_vid}&sv={quote(style_version, safe='')}"
        )
        mosaic_point_url = (
            f"{base}/collections/{collection_id}/rasters/point"
            f"?mode=mosaic&mv={mosaic_vid}&sv={quote(style_version, safe='')}"
        )
    extent_fe = await collections_crud.get_collection_bbox_from_features(db, collection_id)
    features_bbox: list[float] | None = None
    if extent_fe and extent_fe.bbox and extent_fe.bbox[0] and len(extent_fe.bbox[0]) >= 4:
        fb = extent_fe.bbox[0]
        features_bbox = [float(fb[0]), float(fb[1]), float(fb[2]), float(fb[3])]
    return JSONResponse(
        content={
            "collection_id": collection_id,
            "item_count": len(item_ids),
            "mosaic_version_id": mosaic_vid,
            "default_mode": "mosaic" if item_ids else None,
            "mosaic_tiles_url": mosaic_url,
            "mosaic_point_url": mosaic_point_url,
            "terrain_tilejson_url": f"{base}/collections/{collection_id}/rasters/terrain/tilejson.json",
            "collection_is_dem": collection_is_dem,
            "collection_dem_encoding": collection_dem_encoding,
            "features_bbox": features_bbox,
            "items": items,
        },
        headers={
            # Maps poll this after ingest; avoid stale JSON behind proxies or the browser cache.
            "Cache-Control": "no-store, max-age=0",
        },
    )


@router.post(
    "/{collection_id}/rasters/upload/sessions",
    status_code=status.HTTP_201_CREATED,
    summary="Create resumable raster upload session (chunked parts, max 100 MiB each)",
)
async def create_raster_upload_session(
    collection_id: str,
    request: Request,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    filename = str(body.get("filename") or "upload.tif")
    settings = get_settings()
    source_crs = body.get("source_crs")
    if isinstance(source_crs, str):
        source_crs = source_crs.strip() or None
    else:
        source_crs = None
    s = create_upload_session(
        collection_id=collection_id,
        owner_id=current_user.id,
        filename=filename,
        mode="append",
        batch_size=1000,
        queue_compute_tiles=False,
        upload_kind="raster_batch",
        extra={
            "is_dem": bool(body.get("is_dem", False)),
            "dem_encoding": body.get("dem_encoding"),
            "source_crs": source_crs,
        },
    )
    return {
        "upload_id": s["upload_id"],
        "status": s["status"],
        "chunk_size_bytes": settings.raster_upload_chunk_size_bytes,
        "expires_in_seconds": settings.bulk_upload_session_ttl_seconds,
        "parts_uploaded": [],
        "complete_url": f"{_base_url(request)}/collections/{collection_id}/rasters/upload/sessions/{s['upload_id']}/complete",
    }


@router.put(
    "/{collection_id}/rasters/upload/sessions/{upload_id}/parts/{part_no}",
    summary="Upload one resumable raster chunk (max size from session chunk_size_bytes)",
)
async def upload_raster_session_part(
    collection_id: str,
    upload_id: str,
    part_no: int = PathParam(..., ge=1),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id or s.get("upload_kind") != "raster_batch":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if s.get("owner_id") is not None and int(s.get("owner_id")) != int(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload session owner mismatch")
    settings = get_settings()
    max_part = int(settings.raster_upload_chunk_size_bytes)
    storage = get_bulk_storage()
    part_path = storage.get_chunk_part_path(upload_id, part_no)
    try:
        total = 0
        with open(part_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_part:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Part exceeds maximum {max_part} bytes ({max_part // (1024 * 1024)} MiB per chunk)",
                    )
                f.write(chunk)
    except HTTPException:
        try:
            if os.path.isfile(part_path):
                os.unlink(part_path)
        except OSError:
            pass
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed writing upload part: {e}") from e
    s2 = add_uploaded_part(upload_id, part_no)
    return {"upload_id": upload_id, "part_no": part_no, "parts_uploaded": sorted(s2.get("parts") if s2 else [part_no])}


@router.post(
    "/{collection_id}/rasters/upload/sessions/{upload_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Complete resumable raster upload and queue COG ingest",
)
async def complete_raster_upload_session(
    request: Request,
    collection_id: str,
    upload_id: str,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id or s.get("upload_kind") != "raster_batch":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    parts = [int(p) for p in (body.get("parts") or s.get("parts") or [])]
    if not parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No uploaded parts")

    filename = str(s.get("filename") or "upload.tif")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".tif", ".tiff", ".geotiff", ".zip"):
        suffix = ".tif"
    staging_key = f"{upload_id}.upload{suffix}"
    storage = get_bulk_storage()
    try:
        staged_path = storage.assemble_chunk_parts(upload_id, parts, staging_key)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed assembling upload: {e}") from e
    finally:
        storage.delete_upload_parts(upload_id)
        delete_upload_session(upload_id)

    source_crs = s.get("source_crs")
    if isinstance(source_crs, str):
        source_crs = source_crs.strip() or None
    else:
        source_crs = None
    try:
        return await queue_raster_from_staged_file(
            request=request,
            collection_id=collection_id,
            staged_path=Path(staged_path),
            original_filename=filename,
            current_user=current_user,
            is_dem=bool(s.get("is_dem", False)),
            dem_encoding=s.get("dem_encoding"),
            source_crs=source_crs,
        )
    except ValueError as e:
        try:
            storage.delete(staging_key)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception:
        try:
            storage.delete(staging_key)
        except Exception:
            pass
        raise
    finally:
        try:
            storage.delete(staging_key)
        except Exception:
            pass


@router.delete(
    "/{collection_id}/rasters/upload/sessions/{upload_id}",
    summary="Abort resumable raster upload session",
)
async def abort_raster_upload_session(
    collection_id: str,
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    s = get_upload_session(upload_id)
    if not s or s.get("collection_id") != collection_id or s.get("upload_kind") != "raster_batch":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    storage = get_bulk_storage()
    storage.delete_upload_parts(upload_id)
    delete_upload_session(upload_id)
    return {"status": "aborted", "upload_id": upload_id}


@router.post(
    "/{collection_id}/rasters",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload one GeoTIFF and queue background ingest",
    description=(
        "Stores the upload first, then creates a background job to convert to COG in EPSG:4326 and create an item. "
        "If the file has no CRS, georeferencing is assumed to be WGS84 unless source_crs is provided."
    ),
)
async def upload_raster(
    request: Request,
    collection_id: str,
    file: UploadFile = File(..., description="GeoTIFF (EPSG:4326 or source_crs)"),
    is_dem: bool = Form(False, description="Mark uploaded raster as DEM for terrain rendering."),
    dem_encoding: str | None = Form("terrainrgb", description="DEM encoding for terrain tiles: terrainrgb or terrarium."),
    source_crs: str | None = Form(
        None,
        description="Optional CRS of the source file (EPSG:xxxx, proj4, WKT). Overrides embedded CRS when set.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    return await _queue_raster_upload_job(
        request=request,
        collection_id=collection_id,
        files=[file],
        current_user=current_user,
        is_dem=is_dem,
        dem_encoding=dem_encoding,
        source_crs=source_crs.strip() if source_crs and source_crs.strip() else None,
    )


@router.post(
    "/{collection_id}/rasters/batch",
    summary="Upload many GeoTIFF files to a raster collection",
)
async def upload_raster_batch(
    request: Request,
    collection_id: str,
    files: list[UploadFile] = File(...),
    is_dem: bool = Form(False, description="Mark all uploaded rasters as DEM."),
    dem_encoding: str | None = Form("terrainrgb", description="DEM encoding for all uploaded rasters."),
    source_crs: str | None = Form(
        None,
        description="Optional CRS for all files in this request (EPSG:xxxx, proj4, WKT).",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    return await _queue_raster_upload_job(
        request=request,
        collection_id=collection_id,
        files=files,
        current_user=current_user,
        is_dem=is_dem,
        dem_encoding=dem_encoding,
        source_crs=source_crs.strip() if source_crs and source_crs.strip() else None,
    )


@router.patch(
    "/{collection_id}/rasters/{feature_id}/dem",
    summary="Set DEM metadata for a raster item",
)
async def set_raster_item_dem(
    collection_id: str,
    feature_id: str,
    is_dem: bool = Query(...),
    dem_encoding: str | None = Query("terrainrgb"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=403, detail="You do not have permission to edit this collection")
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Raster item not found")
    props = dict(feature.properties or {})
    raster = dict(props.get("raster") or {})
    raster["is_dem"] = bool(is_dem)
    raster["dem_encoding"] = _normalize_dem_encoding(dem_encoding)
    props["raster"] = raster
    # Reuse patch API path used elsewhere for lightweight metadata updates.
    from app.schemas.feature import FeaturePatch
    await features_crud.update_feature(db, collection_id, feature_id, FeaturePatch(properties=props))
    return {"collection_id": collection_id, "feature_id": feature_id, "is_dem": raster["is_dem"], "dem_encoding": raster["dem_encoding"]}


@router.get(
    "/{collection_id}/rasters/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Raster collection tiles via Titiler (single item or mosaic)",
)
async def get_raster_collection_tile(
    request: Request,
    collection_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    mode: str | None = Query(None, description="mosaic or item (auto: item when 1 item, mosaic when >1)"),
    feature_id: str | None = Query(None, description="Required when mode=item"),
    style_id: str | None = Query(None, description="Optional raster style preset id"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    secret = settings.titiler_internal_secret
    titiler = settings.titiler_internal_url.rstrip("/")
    fwd = await prepare_raster_collection_titiler(
        db,
        request,
        collection_id,
        kind="tiles",
        tile_matrix_set_id=tile_matrix_set_id,
        z=z,
        x=x,
        y=y,
        ext=ext,
        mode=mode,
        feature_id=feature_id,
        style_id=style_id,
    )
    upstream = f"{titiler}{fwd.forward_path}"
    params = fwd.params
    response_headers = fwd.response_headers
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp: httpx.Response | None = None
        for attempt in range(2):
            resp = await client.get(upstream, params=params)
            # Transient Titiler/GDAL hiccups and occasional bug-masked 500s; one retry often succeeds.
            if resp.status_code not in (500, 502, 503, 504) or attempt == 1:
                break
            await asyncio.sleep(0.08)
    assert resp is not None
    if resp.status_code >= 400:
        logger.warning(
            "Titiler tile error status=%s upstream=%s collection=%s mode=%s feature_id=%s body=%s",
            resp.status_code,
            upstream,
            collection_id,
            mode,
            feature_id,
            (resp.text or "")[:1000],
        )
        raise HTTPException(
            status_code=resp.status_code,
            detail=sanitize_titiler_upstream_error_text(
                resp.text,
                shared_secret=secret,
                max_len=1000,
            ),
        )
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/png"),
        headers=response_headers,
    )


@router.get(
    "/{collection_id}/rasters/point",
    summary="Sample raster pixel values at lon/lat (Titiler point)",
)
async def get_raster_collection_point(
    request: Request,
    collection_id: str,
    lon: float = Query(..., description="Longitude WGS84"),
    lat: float = Query(..., description="Latitude WGS84"),
    mode: str | None = Query(None, description="mosaic or item"),
    feature_id: str | None = Query(None, description="Required when mode=item"),
    style_id: str | None = Query(None, description="Optional raster style preset id"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    titiler = settings.titiler_internal_url.rstrip("/")
    fwd = await prepare_raster_collection_titiler(
        db,
        request,
        collection_id,
        kind="point",
        lon=lon,
        lat=lat,
        mode=mode,
        feature_id=feature_id,
        style_id=style_id,
    )
    extra_cog_urls: list[str] = []
    if fwd.forward_path.startswith("/mosaicjson/point/"):
        hit_id = await features_crud.find_raster_feature_id_at_point(db, collection_id, lon, lat)
        if hit_id:
            http_u = internal_cog_http_url(settings, collection_id, hit_id)
            if http_u:
                extra_cog_urls.append(http_u)
            det = cog_path_for(settings.raster_storage_path, collection_id, hit_id)
            if det.exists():
                extra_cog_urls.append(os.fspath(det))

    async with httpx.AsyncClient(timeout=30.0) as client:
        if fwd.forward_path.startswith("/mosaicjson/point/"):
            raw = await fetch_mosaic_point_with_fallback(
                client,
                titiler,
                fwd.forward_path,
                fwd.params,
                shared_secret=settings.titiler_internal_secret,
                extra_cog_urls=extra_cog_urls or None,
            )
        else:
            raw = await fetch_titiler_point_json(
                client,
                titiler,
                fwd.forward_path,
                fwd.params,
                shared_secret=settings.titiler_internal_secret,
            )
    return JSONResponse(content=enrich_point_response(raw, fwd.style_spec))


@router.get(
    "/{collection_id}/rasters/terrain/tilejson.json",
    summary="MapLibre terrain TileJSON for raster collection DEM",
)
async def get_raster_collection_terrain_tilejson(
    request: Request,
    collection_id: str,
    mode: str | None = Query(None, description="mosaic or item"),
    feature_id: str | None = Query(None),
    dem_encoding: str | None = Query(None, description="terrainrgb or terrarium"),
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(collection)
    base = _base_url(request)
    item_ids = await _raster_collection_item_ids(db, collection_id)
    collection_is_dem, collection_dem_encoding = _collection_dem_settings(collection)
    if not item_ids:
        raise HTTPException(status_code=404, detail="No raster items found for this collection")
    selected_mode = (mode or "mosaic").lower()
    selected_feature_id = feature_id
    algorithm = _normalize_dem_encoding(dem_encoding) if dem_encoding is not None else collection_dem_encoding
    if selected_mode == "item":
        if not selected_feature_id:
            if len(item_ids) == 1:
                selected_feature_id = item_ids[0]
            else:
                raise HTTPException(status_code=400, detail="feature_id is required for item mode")
        f = await features_crud.get_feature(db, collection_id, selected_feature_id)
        if not f:
            raise HTTPException(status_code=404, detail="Raster item not found")
        is_dem, item_alg = _feature_dem_settings(f)
        if not is_dem and dem_encoding is None and not collection_is_dem:
            raise HTTPException(status_code=400, detail="Selected item is not marked as DEM")
        if dem_encoding is None and is_dem:
            algorithm = item_alg
        bounds = (f.properties or {}).get("raster", {}).get("meta", {}).get("bounds")
    else:
        # For mosaic, require at least one DEM-marked item and merge item bounds.
        bounds = None
        found_dem = False
        merged = [180.0, 90.0, -180.0, -90.0]
        for fid in item_ids:
            f = await features_crud.get_feature(db, collection_id, fid)
            if not f:
                continue
            is_dem, item_alg = _feature_dem_settings(f)
            if is_dem:
                found_dem = True
                if dem_encoding is None:
                    algorithm = item_alg
            b = (f.properties or {}).get("raster", {}).get("meta", {}).get("bounds")
            if isinstance(b, list) and len(b) >= 4:
                merged[0] = min(merged[0], float(b[0]))
                merged[1] = min(merged[1], float(b[1]))
                merged[2] = max(merged[2], float(b[2]))
                merged[3] = max(merged[3], float(b[3]))
        if not found_dem and dem_encoding is None and not collection_is_dem:
            raise HTTPException(status_code=400, detail="No DEM-marked raster items found in collection")
        if merged[0] <= merged[2] and merged[1] <= merged[3]:
            bounds = merged
    params = [("mode", selected_mode), ("dem_encoding", algorithm), ("demv", "2")]
    if selected_feature_id:
        params.append(("feature_id", selected_feature_id))
    tile_url = (
        f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?"
        + "&".join(f"{k}={v}" for k, v in params)
    )
    payload = {
        "tilejson": "2.2.0",
        "name": f"{collection_id}-terrain",
        "scheme": "xyz",
        "tiles": [tile_url],
        "minzoom": 0,
        "maxzoom": 14,
        "encoding": _maplibre_dem_encoding(algorithm),
    }
    if isinstance(bounds, list) and len(bounds) >= 4:
        payload["bounds"] = [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]
    return JSONResponse(content=payload)


@router.get(
    "/{collection_id}/rasters/mosaic.json",
    summary="Build collection MosaicJSON from raster items",
)
async def get_raster_collection_mosaic_json(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    item_ids = await _raster_collection_item_ids(db, collection_id)
    if not item_ids:
        raise HTTPException(status_code=404, detail="No raster items found for this collection")
    settings = get_settings()
    try:
        mosaic, mv = await get_or_build_collection_mosaic(
            db, collection_id, settings, item_ids=item_ids
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="No valid raster COG items found")
    return JSONResponse(content=mosaic, headers={"ETag": mv})
