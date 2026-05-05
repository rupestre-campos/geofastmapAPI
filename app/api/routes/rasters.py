"""Raster collection APIs: upload TIFF(s) and render via Titiler (item or mosaic)."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
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
from app.services.coverages import cog_path_for
from app.services.bulk_queue import BulkJobPayload, enqueue, register_bulk_import_job
from app.services.bulk_storage import get_bulk_storage
from app.services.collection_type_guard import ensure_raster_collection
from app.services.job_store import create_job, update_job
from app.services.raster_collection_mosaic import get_or_build_collection_mosaic
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.raster_batch import (
    RasterBatchUploadTooLargeError,
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
    register_bulk_import_job(job.job_id, storage_key)
    update_job(
        job.job_id,
        message="Raster upload received; queued for COG conversion.",
        items_in=n,
    )
    enqueue(
        BulkJobPayload(
            job_id=job.job_id,
            collection_id=collection_id,
            storage_key=storage_key,
            mode="append",
            batch_size=1000,
            owner_id=current_user.id,
            queue_compute_tiles=False,
            job_kind="raster_batch",
        )
    )
    base = _base_url(request)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "collection_id": collection_id,
            "job_id": job.job_id,
            "status": "pending",
            "queued_files": n,
            "message": "Upload stored; raster processing runs in the background.",
            "job_url": f"{base}/jobs/{job.job_id}",
        },
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
                "delete_url": f"{base}/collections/{collection_id}/items/{fid}",
                "map_layer": {
                    "collection_id": collection_id,
                    "raster_tiles": True,
                    "raster_collection_mode": "item",
                    "raster_feature_id": fid,
                    "tiles_url": (
                        f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
                        f"?mode=item&feature_id={fid}&sv={quote(style_version, safe='')}"
                    ),
                    "terrain_enabled": bool(is_dem or collection_is_dem),
                    "terrain_encoding": _maplibre_dem_encoding(dem_encoding if is_dem else collection_dem_encoding),
                },
            }
        )
    mosaic_url = None
    if item_ids and mosaic_vid:
        mosaic_url = (
            f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
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
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    collection_is_dem, collection_dem_encoding = _collection_dem_settings(collection)
    titiler = settings.titiler_internal_url.rstrip("/")
    if not titiler:
        raise HTTPException(status_code=503, detail="Titiler not configured")
    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if not secret or not fetch_base:
        raise HTTPException(status_code=503, detail="Titiler internal fetch is not configured")

    item_ids = await _raster_collection_item_ids(db, collection_id)
    response_headers: dict[str, str] = {}
    # Prefer mosaic for collection previews (single- or multi-item); explicit mode=item still supported.
    mode = (mode or ("mosaic" if item_ids else "item")).lower()
    params: list[tuple[str, str]] = []
    dem_algorithm: str | None = None
    if mode == "item":
        if not feature_id:
            if len(item_ids) == 1:
                feature_id = item_ids[0]
            else:
                raise HTTPException(status_code=400, detail="feature_id is required when mode=item")
        cog_url: str | None = None
        upstream = f"{titiler}/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        if feature_id:
            f = await features_crud.get_feature(db, collection_id, feature_id)
            if f:
                props = f.properties or {}
                raster = props.get("raster") if isinstance(props, dict) else None
                cog_val = raster.get("cog_path") if isinstance(raster, dict) else None
                if isinstance(cog_val, str) and cog_val:
                    cog_url = cog_val
                else:
                    cog_url = str(cog_path_for(settings.raster_storage_path, collection_id, feature_id))
                is_dem, dem_enc = _feature_dem_settings(f)
                if is_dem or collection_is_dem:
                    dem_algorithm = dem_enc if is_dem else collection_dem_encoding
        if not cog_url:
            cog_url = str(cog_path_for(settings.raster_storage_path, collection_id, feature_id))
        # Fail fast with a clear client error instead of Titiler/GDAL 500 when the COG is missing
        # (orphan row, different storage root on worker, or manual delete).
        _cu = str(cog_url).strip()
        if _cu and not _cu.startswith(("http://", "https://", "/vsicurl/", "/vsi")):
            if not Path(_cu).is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "Raster GeoTIFF file for this item is not present on server storage. "
                        "The catalog row may be orphaned—delete the item and upload again, "
                        "or fix shared volume alignment for the raster worker and API."
                    ),
                )
        params.append(("url", cog_url))
    else:
        if not item_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No raster items in this collection for mosaic mode.",
            )
        expected_mv = compute_mosaic_version_id(collection_id, item_ids)
        mv_client_raw = request.query_params.get("mv")
        mv_client = (mv_client_raw or "").strip() or None
        if mv_client is not None and not mosaic_mv_matches_request(mv_client, expected_mv):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Mosaic version mismatch; reload collection metadata or open a fresh map layer.",
            )
        mosaic_url = (
            f"{fetch_base}/internal/collections/{collection_id}/rasters/mosaic.json"
            f"?token={quote(secret, safe='')}"
        )
        upstream = f"{titiler}/mosaicjson/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        params.append(("url", mosaic_url))
        params.append(("mv", expected_mv))
        if mv_client and mosaic_mv_matches_request(mv_client, expected_mv):
            response_headers["Cache-Control"] = MOSAIC_TILE_CACHE_CONTROL
        else:
            response_headers["Cache-Control"] = MOSAIC_TILE_CACHE_CONTROL_LEGACY
    request_keys = frozenset(request.query_params.keys())
    # Avoid forwarding duplicate keys Titiler may reject (mv is set above for mosaic).
    for k, v in request.query_params.multi_items():
        if k in ("mode", "feature_id", "style_id", "dem_encoding", "mv", "sv"):
            continue
        params.append((k, v))
    dem_encoding_q = request.query_params.get("dem_encoding")
    dem_request = bool(dem_encoding_q)
    style = None
    if style_id and not dem_request:
        style = await raster_styles_crud.get_raster_style(db, collection_id, style_id)
        if style is None:
            style = await raster_styles_crud.get_public_raster_style(db, style_id)
    elif not dem_request:
        # Mosaic always had default; item (single-feature collections) did not — tiles stayed raw RGB.
        style = await raster_styles_crud.get_default_raster_style(db, collection_id)
    style_spec: dict = (style.style_spec if style and isinstance(style.style_spec, dict) else {}) or {}
    # DEM terrain requests must bypass visualization style params (bidx/expression/colormap),
    # otherwise raster-dem decoding receives rendered imagery instead of elevation encoding.
    if style_spec and not dem_request:
        # Query params from the style editor / clients win over preset keys to avoid duplicate Titiler args.
        for key in ("asset", "assets", "bidx", "rescale", "colormap_name", "expression", "color_formula"):
            if key in request_keys:
                continue
            value = style_spec.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                params.append((key, ",".join(str(x) for x in value)))
            else:
                params.append((key, str(value)))
    # Allow explicit DEM encoding override for terrain clients.
    if dem_request:
        dem_algorithm = _normalize_dem_encoding(dem_encoding_q)
    # DEM terrain RGB conflicts with analytic viz (single band / colormap / expr). Multi-bidx RGB keeps terrain.
    bidx_vals = request.query_params.getlist("bidx")
    assets_vals = request.query_params.getlist("assets")

    def _style_supplies(key: str) -> bool:
        if dem_request:
            return False
        v = style_spec.get(key)
        if v is None or v == "" or v == []:
            return False
        return key not in request_keys

    force_analytic = bool(
        request.query_params.get("expression")
        or request.query_params.get("colormap_name")
        or request.query_params.get("color_formula")
        or len(assets_vals) >= 2
        or _style_supplies("expression")
        or _style_supplies("colormap_name")
        or _style_supplies("color_formula")
    )
    if len(bidx_vals) == 1:
        force_analytic = True
    if _style_supplies("bidx"):
        bx = style_spec.get("bidx")
        if isinstance(bx, list) and len(bx) == 1:
            force_analytic = True
        elif isinstance(bx, str) and bx.strip() and "," not in bx.strip():
            force_analytic = True
    if force_analytic and not dem_request:
        dem_algorithm = None
    # Mosaic: apply terrain RGB only when no explicit bands/colormap/expression (those conflict with algorithm=).
    _mosaic_viz_keys = frozenset({"bidx", "colormap_name", "expression", "color_formula", "assets", "rescale"})
    has_mosaic_viz = mode == "mosaic" and any(k in _mosaic_viz_keys for k, _ in params)
    if (
        mode == "mosaic"
        and dem_algorithm is None
        and not force_analytic
        and collection_is_dem
        and not has_mosaic_viz
    ):
        dem_algorithm = collection_dem_encoding
    if dem_algorithm:
        params.append(("algorithm", dem_algorithm))
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(upstream, params=params)
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
    params = [("mode", selected_mode), ("dem_encoding", algorithm)]
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
