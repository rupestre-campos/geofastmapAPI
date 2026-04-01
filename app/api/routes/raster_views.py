"""Saved MosaicJSON / Titiler view definitions (metadata + disk JSON)."""

from __future__ import annotations

import json
from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.api.deps import get_current_user_optional, get_current_user_required
from app.core.config import get_settings
from app.core.permissions import can_see_raster_view
from app.crud import raster_views as raster_views_crud
from app.db.session import get_db
from app.services.titiler_http import get_titiler_http_client
from app.models.collection import VISIBILITY_PRIVATE
from app.models.user import User

router = APIRouter(prefix="/raster-views", tags=["raster-views"])


class RasterViewCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    visibility: str = Field(default=VISIBILITY_PRIVATE, description="private | public | logged")
    mosaic_json: dict = Field(..., description="Titiler-compatible MosaicJSON object.")


class RasterViewRead(BaseModel):
    id: str
    title: str
    visibility: str
    json_relative_path: str

    model_config = {"from_attributes": True}


@router.post("", summary="Save a MosaicJSON view to disk and register metadata")
async def create_raster_view(
    body: RasterViewCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if body.visibility not in ("private", "public", "logged"):
        raise HTTPException(status_code=400, detail="visibility must be private, public, or logged")

    settings = get_settings()
    vid = str(uuid7())
    rel = f"views/{vid}.json"
    root = Path(settings.raster_storage_path)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body.mosaic_json, separators=(",", ":")), encoding="utf-8")

    row = await raster_views_crud.create_view(
        db,
        title=body.title,
        json_relative_path=rel,
        owner_id=current_user.id,
        visibility=body.visibility,
        view_id=vid,
    )
    return RasterViewRead.model_validate(row)


@router.get("/{view_id}", summary="Get raster view metadata", response_model=RasterViewRead)
async def get_raster_view(
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_see_raster_view(db, row.owner_id, row.visibility, view_id, current_user):
        raise HTTPException(status_code=404, detail="View not found")
    return RasterViewRead.model_validate(row)


@router.get(
    "/{view_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy mosaic tile to Titiler",
)
async def titiler_mosaic_tile(
    request: Request,
    view_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    settings = get_settings()
    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Titiler not configured")

    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_see_raster_view(db, row.owner_id, row.visibility, view_id, current_user):
        raise HTTPException(status_code=404, detail="View not found")

    path = Path(settings.raster_storage_path) / row.json_relative_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Mosaic JSON missing on disk")

    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if secret and fetch_base:
        from urllib.parse import quote

        mosaic_url = f"{fetch_base}/internal/raster-views/{view_id}/mosaic.json?token={quote(secret, safe='')}"
    else:
        mosaic_url = f"file://{path.resolve()}"

    forward_path = f"/mosaicjson/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
    params = dict(request.query_params)
    params["url"] = mosaic_url

    try:
        client = get_titiler_http_client()
        r = await client.get(f"{base}{forward_path}", params=params)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Titiler request failed: {e}") from e

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:2000] if r.text else "Titiler error")

    ct = r.headers.get("content-type", "image/png")
    return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=3600"})


