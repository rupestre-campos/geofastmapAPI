"""Raster collection APIs: upload TIFF(s) and render via Titiler (item or mosaic)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid6 import uuid7

from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_edit_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.crud import raster_styles as raster_styles_crud
from app.db.session import get_db
from app.schemas.feature import FeatureCreate, FeatureGeoJSON, Geometry
from app.schemas.ogc import Link
from app.services.coverages import cog_path_for, convert_geotiff_to_cog_4326
from app.services.collection_type_guard import ensure_raster_collection
from app.services.mosaic_plan import build_mosaicjson_from_footprints

router = APIRouter()

_ALLOWED_SUFFIX = frozenset({".tif", ".tiff", ".geotiff"})
_DEM_ENCODINGS = frozenset({"terrainrgb", "terrarium"})


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _raster_collection_item_ids(db: AsyncSession, collection_id: str) -> list[str]:
    q = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    return [r.id for r in q.fetchall()]


def _mosaic_version_id(collection_id: str, item_ids: list[str]) -> str | None:
    if len(item_ids) <= 1:
        return None
    raw = f"{collection_id}:{','.join(item_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_dem_encoding(value: str | None) -> str:
    enc = (value or "terrainrgb").strip().lower()
    return enc if enc in _DEM_ENCODINGS else "terrainrgb"


def _maplibre_dem_encoding(algorithm: str) -> str:
    # MapLibre uses "mapbox" name for TerrainRGB tiles.
    return "mapbox" if algorithm == "terrainrgb" else "terrarium"


def _collection_dem_settings(collection) -> tuple[bool, str]:
    rs = getattr(collection, "raster_settings", None)
    if not isinstance(rs, dict):
        return (False, "terrainrgb")
    return (bool(rs.get("is_dem", False)), _normalize_dem_encoding(rs.get("dem_encoding")))


def _extract_raster_footprint_and_href(feature: object, base: str, secret: str) -> tuple[str, object] | None:
    from shapely.geometry import shape
    from app.utils.geo import geometry_to_geojson

    props = getattr(feature, "properties", None) or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    cog_path = raster.get("cog_path") if isinstance(raster, dict) else None
    if not isinstance(cog_path, str) or not cog_path:
        return None
    geom = getattr(feature, "geometry", None)
    gj = geometry_to_geojson(geom) if geom is not None else None
    if not gj:
        return None
    href = (
        f"{base}/internal/collections/{feature.collection_id}/coverages/{feature.id}/cog"
        f"?token={quote(secret, safe='')}"
    )
    return href, shape(gj)


async def _upload_one_raster(
    *,
    request: Request,
    collection_id: str,
    file: UploadFile,
    title: str | None,
    db: AsyncSession,
    is_dem: bool = False,
    dem_encoding: str | None = None,
    source_crs: str | None = None,
) -> FeatureGeoJSON:
    settings = get_settings()
    name = (file.filename or "upload.tif").lower()
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected .tif / .tiff file, got suffix {suffix!r}",
        )

    root = Path(settings.raster_storage_path)
    root.mkdir(parents=True, exist_ok=True)

    max_b = settings.raster_upload_max_bytes
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    tmp_path_p = Path(tmp_path)
    feature_id = str(uuid7())
    dst = cog_path_for(settings.raster_storage_path, collection_id, feature_id)

    try:
        with open(tmp_path, "wb") as out:
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_b:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File too large (max {max_b} bytes)",
                    )
                out.write(chunk)

        feature = await _create_raster_feature_from_source(
            collection_id=collection_id,
            source_path=tmp_path_p,
            feature_id=feature_id,
            title=title,
            db=db,
            is_dem=is_dem,
            dem_encoding=dem_encoding,
            source_crs=source_crs,
        )
    except HTTPException:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        try:
            tmp_path_p.unlink(missing_ok=True)
        except OSError:
            pass

    base = _base_url(request)
    from app.utils.geo import geometry_to_geojson

    geom_dict = geometry_to_geojson(feature.geometry) if feature.geometry is not None else None
    return FeatureGeoJSON(
        id=feature.id,
        type="Feature",
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature.id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}/coverages/{feature.id}", rel="related", type="image/tiff"),
        ],
    )


async def _create_raster_feature_from_source(
    *,
    collection_id: str,
    source_path: str | Path,
    feature_id: str,
    title: str | None,
    db: AsyncSession,
    is_dem: bool = False,
    dem_encoding: str | None = None,
    source_crs: str | None = None,
):
    settings = get_settings()
    dst = cog_path_for(settings.raster_storage_path, collection_id, feature_id)
    try:
        conv = convert_geotiff_to_cog_4326(source_path, dst, source_crs=source_crs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    footprint = conv["footprint_geojson"]
    meta = conv["meta"]
    raster_props = {
        "cog_path": conv["cog_path"],
        "meta": meta,
        "is_dem": bool(is_dem),
        "dem_encoding": _normalize_dem_encoding(dem_encoding),
    }
    if title:
        raster_props["title"] = title
    props: dict = {"raster": raster_props}
    if title:
        props["title"] = title

    data = FeatureCreate(
        collection_id=collection_id,
        geometry=Geometry(**footprint),
        properties=props,
    )
    try:
        return await features_crud.create_feature_with_id(db, data, feature_id)
    except Exception:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def _save_upload_to_temp(file: UploadFile, *, suffix: str) -> Path:
    settings = get_settings()
    max_b = settings.raster_upload_max_bytes
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    out_path = Path(tmp_path)
    with open(out_path, "wb") as out:
        total = 0
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_b:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File too large (max {max_b} bytes)",
                )
            out.write(chunk)
    return out_path


def _zip_tiff_members(zip_path: Path) -> list[str]:
    members: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            suffix = Path(info.filename).suffix.lower()
            if suffix in _ALLOWED_SUFFIX:
                members.append(info.filename)
    return members


def _vsizip_member_path(zip_path: Path, member_name: str) -> str:
    # Use GDAL virtual filesystem path so TIFF + sidecars (e.g. .tfw) can be read without extraction.
    safe_member = member_name.lstrip("/")
    return f"/vsizip/{zip_path.as_posix()}/{safe_member}"


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
    mosaic_vid = _mosaic_version_id(collection_id, item_ids)
    collection_is_dem, collection_dem_encoding = _collection_dem_settings(collection)
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
                    f"?mode=item&feature_id={fid}"
                ),
                "delete_url": f"{base}/collections/{collection_id}/items/{fid}",
                "map_layer": {
                    "collection_id": collection_id,
                    "raster_tiles": True,
                    "raster_collection_mode": "item",
                    "raster_feature_id": fid,
                    "tiles_url": (
                        f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
                        f"?mode=item&feature_id={fid}"
                    ),
                    "terrain_enabled": bool(is_dem or collection_is_dem),
                    "terrain_encoding": _maplibre_dem_encoding(dem_encoding if is_dem else collection_dem_encoding),
                },
            }
        )
    mosaic_url = None
    if len(item_ids) > 1:
        mosaic_url = (
            f"{base}/collections/{collection_id}/rasters/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
            f"?mode=mosaic&mv={mosaic_vid}"
        )
    return JSONResponse(
        content={
            "collection_id": collection_id,
            "item_count": len(item_ids),
            "mosaic_version_id": mosaic_vid,
            "default_mode": "mosaic" if len(item_ids) > 1 else ("item" if len(item_ids) == 1 else None),
            "mosaic_tiles_url": mosaic_url,
            "terrain_tilejson_url": f"{base}/collections/{collection_id}/rasters/terrain/tilejson.json",
            "collection_is_dem": collection_is_dem,
            "collection_dem_encoding": collection_dem_encoding,
            "items": items,
        }
    )


@router.post(
    "/{collection_id}/rasters",
    summary="Upload a GeoTIFF as Cloud Optimized GeoTIFF (EPSG:4326)",
    description=(
        "Converts to COG on disk in EPSG:4326. If the file has no CRS, georeferencing is assumed to be WGS84. "
        "Use source_crs (EPSG:xxxx, proj4, or WKT) when tags are missing or wrong so the server can reproject."
    ),
)
async def upload_raster(
    request: Request,
    collection_id: str,
    file: UploadFile = File(..., description="GeoTIFF (EPSG:4326 or source_crs)"),
    title: str | None = Form(None, description="Optional title stored in properties"),
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
    return await _upload_one_raster(
        request=request,
        collection_id=collection_id,
        file=file,
        title=title,
        db=db,
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
    crs_opt = source_crs.strip() if source_crs and source_crs.strip() else None
    items: list[dict] = []
    for f in files:
        name = (f.filename or "").lower()
        suffix = Path(name).suffix.lower()
        if suffix in _ALLOWED_SUFFIX:
            one = await _upload_one_raster(
                request=request,
                collection_id=collection_id,
                file=f,
                title=None,
                db=db,
                is_dem=is_dem,
                dem_encoding=dem_encoding,
                source_crs=crs_opt,
            )
            items.append(one.model_dump(mode="json"))
            continue
        if suffix != ".zip":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported raster batch input {suffix!r}; expected TIFF or ZIP.",
            )
        zip_tmp: Path | None = None
        try:
            zip_tmp = await _save_upload_to_temp(f, suffix=".zip")
            members = _zip_tiff_members(zip_tmp)
            if not members:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="ZIP does not contain any .tif/.tiff/.geotiff files.",
                )
            for member in members:
                feature_id = str(uuid7())
                title = Path(member).stem
                feature = await _create_raster_feature_from_source(
                    collection_id=collection_id,
                    source_path=_vsizip_member_path(zip_tmp, member),
                    feature_id=feature_id,
                    title=title,
                    db=db,
                    is_dem=is_dem,
                    dem_encoding=dem_encoding,
                    source_crs=crs_opt,
                )
                base = _base_url(request)
                from app.utils.geo import geometry_to_geojson

                geom_dict = geometry_to_geojson(feature.geometry) if feature.geometry is not None else None
                one = FeatureGeoJSON(
                    id=feature.id,
                    type="Feature",
                    geometry=Geometry(**geom_dict) if geom_dict else None,
                    properties=feature.properties,
                    links=[
                        Link(
                            href=f"{base}/collections/{collection_id}/items/{feature.id}",
                            rel="self",
                            type="application/geo+json",
                        ),
                        Link(
                            href=f"{base}/collections/{collection_id}/coverages/{feature.id}",
                            rel="related",
                            type="image/tiff",
                        ),
                    ],
                )
                items.append(one.model_dump(mode="json"))
        except zipfile.BadZipFile as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid ZIP file: {e}") from e
        finally:
            if zip_tmp is not None:
                try:
                    zip_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
    return JSONResponse(content={"collection_id": collection_id, "uploaded": len(items), "items": items})


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
    titiler = settings.titiler_internal_url.rstrip("/")
    if not titiler:
        raise HTTPException(status_code=503, detail="Titiler not configured")
    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if not secret or not fetch_base:
        raise HTTPException(status_code=503, detail="Titiler internal fetch is not configured")

    item_ids = await _raster_collection_item_ids(db, collection_id)
    mode = (mode or ("item" if len(item_ids) == 1 else "mosaic")).lower()
    params: list[tuple[str, str]] = []
    if mode == "item":
        if not feature_id:
            if len(item_ids) == 1:
                feature_id = item_ids[0]
            else:
                raise HTTPException(status_code=400, detail="feature_id is required when mode=item")
        cog_url = (
            f"{fetch_base}/internal/collections/{collection_id}/coverages/{feature_id}/cog"
            f"?token={quote(secret, safe='')}"
        )
        upstream = f"{titiler}/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        params.append(("url", cog_url))
        if feature_id:
            f = await features_crud.get_feature(db, collection_id, feature_id)
            if f:
                is_dem, dem_enc = _feature_dem_settings(f)
                if is_dem:
                    params.append(("algorithm", dem_enc))
    else:
        mosaic_url = (
            f"{fetch_base}/internal/collections/{collection_id}/rasters/mosaic.json"
            f"?token={quote(secret, safe='')}"
        )
        upstream = f"{titiler}/mosaicjson/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        params.append(("url", mosaic_url))
        mv = request.query_params.get("mv")
        if mv:
            params.append(("mv", mv))
    for k, v in request.query_params.multi_items():
        if k not in ("mode", "feature_id", "style_id"):
            params.append((k, v))
    style = None
    if style_id:
        style = await raster_styles_crud.get_raster_style(db, collection_id, style_id)
    elif mode == "mosaic":
        style = await raster_styles_crud.get_default_raster_style(db, collection_id)
    if style and isinstance(style.style_spec, dict):
        spec = style.style_spec
        for key in ("asset", "assets", "bidx", "rescale", "colormap_name", "expression"):
            value = spec.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                params.append((key, ",".join(str(x) for x in value)))
            else:
                params.append((key, str(value)))
    # Allow explicit DEM encoding override for terrain clients.
    dem_encoding_q = request.query_params.get("dem_encoding")
    if dem_encoding_q:
        params.append(("algorithm", _normalize_dem_encoding(dem_encoding_q))
        )
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(upstream, params=params)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:1000] or "Titiler error")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "image/png"))


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
    selected_mode = (mode or ("item" if len(item_ids) == 1 else "mosaic")).lower()
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
    ids = await _raster_collection_item_ids(db, collection_id)
    if not ids:
        raise HTTPException(status_code=404, detail="No raster items found for this collection")
    settings = get_settings()
    base = settings.raster_internal_fetch_base_url.rstrip("/")
    secret = settings.titiler_internal_secret
    if not base or not secret:
        raise HTTPException(status_code=503, detail="Titiler internal fetch is not configured")
    pairs = []
    for fid in ids:
        f = await features_crud.get_feature(db, collection_id, fid)
        if not f:
            continue
        pair = _extract_raster_footprint_and_href(f, base, secret)
        if pair is not None:
            pairs.append(pair)
    if not pairs:
        raise HTTPException(status_code=404, detail="No valid raster COG items found")
    mosaic = build_mosaicjson_from_footprints(pairs)
    return JSONResponse(content=mosaic)
