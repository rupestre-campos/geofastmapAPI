"""Proxy to Titiler for dynamic COG tiles (auth-checked)."""

from __future__ import annotations

from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_see_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.services.titiler_http import get_titiler_http_client

router = APIRouter()


def _cog_path_from_feature(feature) -> str | None:
    props = feature.properties or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if isinstance(raster, dict):
        p = raster.get("cog_path")
        return p if isinstance(p, str) and p else None
    return None


@router.get(
    "/{collection_id}/coverages/{feature_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy tile to Titiler (WebMercator etc.)",
    description="Requires Titiler sidecar (TITILER_INTERNAL_URL). Forwards query params (rescale, bidx, ...).",
)
async def titiler_proxy_tile(
    request: Request,
    collection_id: str,
    feature_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )

    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view this collection")

    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    cog_path = _cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")

    p = Path(cog_path)
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="COG file missing on disk")

    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if secret and fetch_base:
        cog_url = f"{fetch_base}/internal/collections/{collection_id}/coverages/{feature_id}/cog?token={secret}"
    else:
        cog_url = f"file://{p.resolve()}"

    forward_path = f"/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
    params = dict(request.query_params)
    params["url"] = cog_url

    try:
        client = get_titiler_http_client()
        r = await client.get(f"{base}{forward_path}", params=params)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Titiler request failed: {e}",
        ) from e

    if r.status_code >= 400:
        raise HTTPException(
            status_code=r.status_code,
            detail=r.text[:2000] if r.text else "Titiler error",
        )

    ct = r.headers.get("content-type", "image/png")
    return Response(
        content=r.content,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=3600"},
    )
