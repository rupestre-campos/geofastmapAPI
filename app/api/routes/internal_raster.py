"""Internal raster file fetch for Titiler (token-gated). Not for direct public use."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import require_admin
from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.crud import raster_views as raster_views_crud
from app.db.session import get_db
from app.models.user import User
from app.services.coverages import cog_path_for
from app.services.mosaic_plan import build_mosaicjson_from_footprints

router = APIRouter()


@router.get(
    "/auth/grafana-check",
    include_in_schema=False,
    summary="Admin-only auth check for Grafana reverse-proxy auth_request",
)
async def internal_grafana_admin_check(
    current_user: User = Depends(require_admin),
):
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
@router.head(
    "/collections/{collection_id}/coverages/{feature_id}/cog",
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

    # Hot path: COG location is deterministic (storage_root/collection_id/feature_id.tif).
    # Avoid DB queries for every tile/range request to keep pool usage low under Titiler load.
    deterministic = cog_path_for(settings.raster_storage_path, collection_id, feature_id)
    if deterministic.exists():
        return FileResponse(
            deterministic,
            media_type="image/tiff; application=geotiff",
            filename=deterministic.name,
            headers={"Cache-Control": "private, max-age=60"},
        )

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
        headers={"Cache-Control": "private, max-age=0, must-revalidate"},
    )


@router.get(
    "/collections/{collection_id}/rasters/mosaic.json",
    summary="Internal raster collection MosaicJSON (Titiler fetch)",
    include_in_schema=False,
)
async def internal_fetch_collection_mosaic_json(
    collection_id: str,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    from shapely.geometry import shape
    from app.utils.geo import geometry_to_geojson

    settings = get_settings()
    secret = settings.titiler_internal_secret
    if not secret or token != secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    ids_r = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    ids = [r.id for r in ids_r.fetchall()]
    pairs: list[tuple[str, object]] = []
    for fid in ids:
        feature = await features_crud.get_feature(db, collection_id, fid)
        if not feature:
            continue
        gj = geometry_to_geojson(feature.geometry) if feature.geometry is not None else None
        if not gj:
            continue
        props = feature.properties or {}
        raster = props.get("raster") if isinstance(props, dict) else None
        cog_path = raster.get("cog_path") if isinstance(raster, dict) else None
        det = cog_path_for(settings.raster_storage_path, collection_id, fid)
        href: str | None = None
        if isinstance(cog_path, str) and cog_path and Path(cog_path).exists():
            href = cog_path
        elif det.exists():
            href = os.fspath(det)
        if not href:
            continue
        pairs.append((href, shape(gj)))
    if not pairs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    mosaic = build_mosaicjson_from_footprints(pairs)
    return Response(content=json.dumps(mosaic, separators=(",", ":")), media_type="application/json")
