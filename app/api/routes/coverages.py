from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_see_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.services.coverages import CogPathOutsideStorageError, resolve_stored_cog_path

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _get_cog_path_from_feature(feature) -> str | None:
    props = feature.properties or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if isinstance(raster, dict):
        p = raster.get("cog_path")
        return p if isinstance(p, str) and p else None
    return None


@router.get(
    "/{collection_id}/coverages/{feature_id}",
    summary="Get coverage (COG GeoTIFF) for a raster item",
    description="Returns the stored Cloud Optimized GeoTIFF for this raster item.",
)
async def get_coverage_geotiff(
    request: Request,
    collection_id: str,
    feature_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view this collection")

    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    cog_path = _get_cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")

    try:
        p = resolve_stored_cog_path(cog_path, get_settings().raster_storage_path)
    except CogPathOutsideStorageError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item is not a coverage",
        ) from None
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="COG file missing on disk")
    return FileResponse(
        p,
        media_type="image/tiff; application=geotiff",
        filename=p.name,
        headers={"Cache-Control": "private, max-age=0"},
    )


@router.get(
    "/{collection_id}/coverages/{feature_id}/tiles/{z:int}/{x:int}/{y:int}.png",
    summary="Coverage tile (Z/X/Y PNG) from COG",
    description="Returns a rendered PNG tile for this coverage from its Cloud Optimized GeoTIFF.",
)
async def get_coverage_tile_png(
    collection_id: str,
    feature_id: str,
    z: int,
    x: int,
    y: int,
    mode: str = Query("rgb", description="rgb | ndvi"),
    brightness: float = Query(0.0, ge=-100, le=100, description="Brightness offset (-100..100)"),
    contrast: float = Query(1.0, ge=0.1, le=3.0, description="Contrast multiplier (0.1..3)"),
    rgb: str | None = Query(None, description="RGB band indexes, comma-separated (1-based), e.g. 1,2,3"),
    red: int | None = Query(None, ge=1, description="Red band for NDVI (1-based)"),
    nir: int | None = Query(None, ge=1, description="NIR band for NDVI (1-based)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view this collection")
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    cog_path = _get_cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")

    try:
        p = resolve_stored_cog_path(cog_path, get_settings().raster_storage_path)
    except CogPathOutsideStorageError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item is not a coverage",
        ) from None
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="COG file missing on disk")

    def _parse_rgb(v: str | None) -> list[int] | None:
        if not v:
            return None
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if len(parts) != 3:
            return None
        try:
            out = [int(parts[0]), int(parts[1]), int(parts[2])]
        except ValueError:
            return None
        if any(i < 1 for i in out):
            return None
        return out

    rgb_idx = _parse_rgb(rgb)

    try:
        import numpy as np
        from rio_tiler.io import COGReader
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Raster tile dependencies not available: {e}")

    try:
        with COGReader(p) as cog:
            if mode == "ndvi":
                red_idx = int(red) if red else 1
                nir_idx = int(nir) if nir else 4
                img = cog.tile(x, y, z, indexes=[red_idx, nir_idx])
                arr = img.data.astype("float32")
                red_a = arr[0]
                nir_a = arr[1]
                denom = (nir_a + red_a)
                denom[denom == 0] = 1e-6
                ndvi = (nir_a - red_a) / denom
                # Map [-1,1] -> [0,255]
                ndvi_8 = np.clip(((ndvi + 1.0) / 2.0) * 255.0, 0, 255).astype("uint8")
                img = img.from_array(ndvi_8[None, :, :], img.mask)
            else:
                if rgb_idx:
                    img = cog.tile(x, y, z, indexes=rgb_idx)
                else:
                    img = cog.tile(x, y, z)

            data = img.data.astype("float32")
            data = data * float(contrast) + float(brightness)
            data = np.clip(data, 0, 255).astype("uint8")
            img = img.from_array(data, img.mask)
            content = img.render(img_format="PNG")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Tile render failed: {e}")

    return Response(content=content, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

