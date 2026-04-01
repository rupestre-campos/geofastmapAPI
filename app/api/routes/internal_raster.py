"""Internal raster file fetch for Titiler (token-gated). Not for direct public use."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.crud import raster_views as raster_views_crud
from app.db.session import get_db

router = APIRouter()


def _cog_path_from_feature(feature) -> str | None:
    props = feature.properties or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if isinstance(raster, dict):
        p = raster.get("cog_path")
        return p if isinstance(p, str) and p else None
    return None


@router.get(
    "/collections/{collection_id}/coverages/{feature_id}/cog",
    summary="Internal COG bytes (Titiler fetch)",
    include_in_schema=False,
)
async def internal_fetch_cog(
    collection_id: str,
    feature_id: str,
    token: str = Query(..., description="Must match TITILER_INTERNAL_SECRET"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    secret = settings.titiler_internal_secret
    if not secret or token != secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    cog_path = _cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    p = Path(cog_path)
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return FileResponse(
        p,
        media_type="image/tiff; application=geotiff",
        filename=p.name,
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get(
    "/raster-views/{view_id}/mosaic.json",
    summary="Internal MosaicJSON (Titiler fetch)",
    include_in_schema=False,
)
async def internal_fetch_mosaic_json(
    view_id: str,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    secret = settings.titiler_internal_secret
    if not secret or token != secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    path = Path(settings.raster_storage_path) / row.json_relative_path
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return Response(
        content=path.read_bytes(),
        media_type="application/json",
        headers={"Cache-Control": "private, max-age=60"},
    )
