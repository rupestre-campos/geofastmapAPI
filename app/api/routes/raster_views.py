"""Saved MosaicJSON / Titiler view definitions (metadata + disk JSON)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.api.deps import get_current_user_optional, get_current_user_required
from app.core.config import get_settings
from app.core.permissions import (
    can_access_raster_view_tiles_anonymous,
    can_edit_raster_view,
    can_see_raster_view,
)
from app.crud import raster_views as raster_views_crud
from app.crud.raster_views import _MISSING
from app.crud import resource_share as resource_share_crud
from app.db.session import get_db
from app.models.collection import VISIBILITY_PRIVATE
from app.models.resource_share import RESOURCE_TYPE_RASTER_VIEW
from app.models.user import User
from app.schemas.resource_share import ShareAdd, ShareRead
from app.services.mosaic_plan import build_mosaicjson_from_footprints
from app.services.titiler_http import get_titiler_http_client
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

router = APIRouter(prefix="/raster-views", tags=["raster-views"])


class RasterViewCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    visibility: str = Field(default=VISIBILITY_PRIVATE, description="private | public | logged")
    mosaic_json: dict = Field(..., description="Titiler-compatible MosaicJSON object.")
    definition: dict[str, Any] | None = None
    allow_public_maps: bool = False


class RasterViewRead(BaseModel):
    id: str
    title: str
    visibility: str
    json_relative_path: str
    bbox: list[float] | None = None
    definition: dict[str, Any] | None = None
    allow_public_maps: bool = False
    owner_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(cls, row: Any, base: str | None = None) -> "RasterViewRead":
        d = {
            "id": row.id,
            "title": row.title,
            "visibility": row.visibility,
            "json_relative_path": row.json_relative_path,
            "bbox": row.bbox,
            "definition": row.definition,
            "allow_public_maps": getattr(row, "allow_public_maps", False),
            "owner_id": row.owner_id,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
            "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        }
        return cls.model_validate(d)


class RasterViewUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=512)
    visibility: str | None = None
    mosaic_json: dict[str, Any] | None = None
    definition: dict[str, Any] | None = None
    allow_public_maps: bool | None = None


def _write_mosaic_file(settings: Any, rel_path: str, mosaic_json: dict) -> None:
    root = Path(settings.raster_storage_path)
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mosaic_json, separators=(",", ":")), encoding="utf-8")


def _mosaic_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Build MosaicJSON from saved definition.selected items (href + footprint GeoJSON)."""
    selected = definition.get("selected") or []
    pairs: list[tuple[str, BaseGeometry]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        href = item.get("href")
        fp = item.get("footprint")
        if not href or not isinstance(fp, dict):
            continue
        try:
            g = shape(fp)
            if g.geom_type == "Polygon":
                pairs.append((str(href), g))
            elif g.geom_type == "MultiPolygon":
                pairs.append((str(href), max(g.geoms, key=lambda p: p.area)))
        except Exception:
            continue
    if not pairs:
        raise ValueError("definition.selected must contain href and footprint for each item")
    return build_mosaicjson_from_footprints(pairs)


@router.get("", summary="List saved raster mosaics")
async def list_raster_views(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
    q: str | None = Query(None),
    bbox: str | None = Query(None, description="minx,miny,maxx,maxy — intersects stored bbox"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mine: bool = Query(False),
):
    bbox_t: tuple[float, float, float, float] | None = None
    if bbox and bbox.strip():
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_t = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                bbox_t = None
    rows, total = await raster_views_crud.list_views_visible_to_user(
        db,
        current_user=current_user,
        limit=limit,
        offset=offset,
        q=q,
        bbox_intersects=bbox_t,
        mine_only=mine,
    )
    return {
        "items": [RasterViewRead.from_row(r).model_dump() for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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

    mosaic_data = body.mosaic_json
    bbox_val: list[float] | None = None
    if body.definition and isinstance(body.definition, dict):
        try:
            mosaic_data = _mosaic_from_definition(body.definition)
        except ValueError:
            mosaic_data = body.mosaic_json
    b = mosaic_data.get("bounds")
    if isinstance(b, list) and len(b) >= 4:
        bbox_val = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]

    _write_mosaic_file(settings, rel, mosaic_data)

    row = await raster_views_crud.create_view(
        db,
        title=body.title,
        json_relative_path=rel,
        owner_id=current_user.id,
        visibility=body.visibility,
        view_id=vid,
        bbox=bbox_val,
        definition=body.definition,
        allow_public_maps=body.allow_public_maps,
    )
    return RasterViewRead.from_row(row)


@router.patch("/{view_id}", summary="Update mosaic metadata and/or MosaicJSON")
async def patch_raster_view(
    view_id: str,
    body: RasterViewUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")

    settings = get_settings()
    unset = body.model_dump(exclude_unset=True)
    if unset.get("visibility") is not None and unset["visibility"] not in ("private", "public", "logged"):
        raise HTTPException(status_code=400, detail="invalid visibility")

    new_mosaic = unset.get("mosaic_json")
    new_def = unset.get("definition")
    bbox_override: list[float] | None = None

    if new_def is not None and isinstance(new_def, dict):
        try:
            mosaic_data = _mosaic_from_definition(new_def)
            new_mosaic = mosaic_data
            b = mosaic_data.get("bounds")
            if isinstance(b, list) and len(b) >= 4:
                bbox_override = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        except ValueError:
            pass

    if new_mosaic is not None:
        _write_mosaic_file(settings, row.json_relative_path, new_mosaic)
        if bbox_override is None and isinstance(new_mosaic.get("bounds"), list):
            b = new_mosaic["bounds"]
            if len(b) >= 4:
                bbox_override = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]

    title = unset.get("title")
    vis = unset.get("visibility")
    allow_pm = unset.get("allow_public_maps")

    bbox_arg: object = _MISSING
    if bbox_override is not None:
        bbox_arg = bbox_override
    def_arg: object = _MISSING
    if "definition" in unset:
        def_arg = new_def

    updated = await raster_views_crud.update_view(
        db,
        view_id,
        title=title,
        visibility=vis,
        bbox=bbox_arg,
        definition=def_arg,
        allow_public_maps=allow_pm,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="View not found")
    return RasterViewRead.from_row(updated)


@router.get("/{view_id}", summary="Get raster view metadata", response_model=RasterViewRead)
async def get_raster_view(
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_see_raster_view(db, row.owner_id, row.visibility, view_id, current_user):
        raise HTTPException(status_code=404, detail="View not found")
    return RasterViewRead.from_row(row)


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
    current_user: User | None = Depends(get_current_user_optional),
):
    settings = get_settings()
    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Titiler not configured")

    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")

    allow_pm = getattr(row, "allow_public_maps", False)
    if current_user is None:
        if not can_access_raster_view_tiles_anonymous(
            visibility=row.visibility,
            allow_public_maps=allow_pm,
        ):
            raise HTTPException(status_code=404, detail="View not found")
    else:
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

    client = get_titiler_http_client()
    ct_holder: dict[str, str] = {}

    async def _stream():
        try:
            # Workaround: avoid upstream compression/content-length mismatches for binary tiles
            # by requesting an identity-encoded response from TiTiler.
            async with client.stream(
                "GET",
                f"{base}{forward_path}",
                params=params,
                headers={"Accept-Encoding": "identity"},
            ) as r:
                ct_holder["content-type"] = r.headers.get("content-type") or ""
                if r.status_code >= 400:
                    # Read only a small prefix to avoid buffering huge responses
                    detail = (await r.aread())[:2000]
                    raise HTTPException(
                        status_code=r.status_code,
                        detail=detail.decode("utf-8", errors="replace") if detail else "Titiler error",
                    )
                async for chunk in r.aiter_bytes():
                    yield chunk
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Titiler request failed: {e}") from e

    # Note: headers are determined up-front; content-type default kept.
    # We intentionally avoid r.content buffering to keep RAM stable under high tile concurrency.
    return StreamingResponse(
        _stream(),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Type": ct_holder.get("content-type") or "image/png",
        },
    )


@router.get(
    "/{view_id}/shares",
    response_model=list[ShareRead],
    summary="List shares for a raster mosaic",
)
async def list_raster_view_shares(
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_RASTER_VIEW, view_id)
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post(
    "/{view_id}/shares",
    response_model=ShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add share for a raster mosaic",
)
async def add_raster_view_share(
    view_id: str,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    share = await resource_share_crud.add_share(
        db, RESOURCE_TYPE_RASTER_VIEW, view_id, payload.username, payload.role
    )
    if not share:
        raise HTTPException(status_code=404, detail="User not found")
    return ShareRead(username=share.username, role=share.role)


@router.delete(
    "/{view_id}/shares/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove share from a raster mosaic",
)
async def remove_raster_view_share(
    view_id: str,
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    ok = await resource_share_crud.remove_share(db, RESOURCE_TYPE_RASTER_VIEW, view_id, username)
    if not ok:
        raise HTTPException(status_code=404, detail="Share not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)