"""Raster style presets for raster collections."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.permissions import can_edit_collection, can_see_collection
from app.crud import collections as collections_crud
from app.crud import raster_styles as raster_styles_crud
from app.crud import resource_share as resource_share_crud
from app.db.session import get_db
from app.models.resource_share import RESOURCE_TYPE_STYLE, raster_style_resource_id
from app.schemas.ogc import Link
from app.schemas.raster_style import (
    RasterStyleCreate,
    RasterStyleList,
    RasterStylePatch,
    RasterStyleRead,
)
from app.schemas.resource_share import ShareAdd, ShareRead
from app.services.collection_type_guard import ensure_raster_collection
from app.services.raster_style_spec import normalize_raster_style_spec_http

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _to_read(base: str, s) -> RasterStyleRead:
    return RasterStyleRead(
        id=s.id,
        title=s.title,
        collection_id=s.collection_id,
        is_default=s.is_default,
        style_spec=s.style_spec,
        visibility=s.visibility,
        created_at=s.created_at,
        updated_at=s.updated_at,
        links=[
            Link(
                href=f"{base}/collections/{s.collection_id}/raster-styles/{s.id}",
                rel="self",
                type="application/json",
            )
        ],
    )


@router.get("/{collection_id}/raster-styles", response_model=RasterStyleList)
async def list_raster_styles(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_see_collection(db, c, current_user):
        raise HTTPException(status_code=404, detail="Collection not found")
    items = await raster_styles_crud.list_raster_styles(db, collection_id)
    base = _base_url(request)
    return RasterStyleList(styles=[_to_read(base, s) for s in items])


@router.get("/{collection_id}/raster-styles/{style_id}", response_model=RasterStyleRead)
async def get_raster_style(
    request: Request,
    collection_id: str,
    style_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_see_collection(db, c, current_user):
        raise HTTPException(status_code=404, detail="Collection not found")
    s = await raster_styles_crud.get_raster_style(db, collection_id, style_id)
    if not s:
        raise HTTPException(status_code=404, detail="Style not found")
    return _to_read(_base_url(request), s)


@router.post("/{collection_id}/raster-styles", response_model=RasterStyleRead, status_code=status.HTTP_201_CREATED)
async def create_raster_style(
    request: Request,
    collection_id: str,
    payload: RasterStyleCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_edit_collection(db, c, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    spec = normalize_raster_style_spec_http(payload.style_spec)
    s = await raster_styles_crud.upsert_raster_style(
        db,
        collection_id=collection_id,
        style_id=payload.id,
        title=payload.title,
        style_spec=spec,
        owner_id=current_user.id if current_user else None,
        set_default=payload.set_default,
        visibility=payload.visibility,
    )
    return _to_read(_base_url(request), s)


@router.patch("/{collection_id}/raster-styles/{style_id}", response_model=RasterStyleRead)
async def patch_raster_style(
    request: Request,
    collection_id: str,
    style_id: str,
    payload: RasterStylePatch,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_edit_collection(db, c, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    cur = await raster_styles_crud.get_raster_style(db, collection_id, style_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Style not found")
    set_default = bool(payload.set_default) if payload.set_default is not None else False
    raw_spec = payload.style_spec if payload.style_spec is not None else cur.style_spec
    spec = normalize_raster_style_spec_http(raw_spec)
    s = await raster_styles_crud.upsert_raster_style(
        db,
        collection_id=collection_id,
        style_id=style_id,
        title=payload.title if payload.title is not None else cur.title,
        style_spec=spec,
        owner_id=cur.owner_id,
        set_default=set_default,
        visibility=payload.visibility,
    )
    return _to_read(_base_url(request), s)


@router.get("/{collection_id}/raster-styles/{style_id}/shares", response_model=list[ShareRead])
async def list_raster_style_shares(
    collection_id: str,
    style_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_edit_collection(db, c, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    rid = raster_style_resource_id(collection_id, style_id)
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_STYLE, rid)
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post("/{collection_id}/raster-styles/{style_id}/shares", response_model=ShareRead, status_code=status.HTTP_201_CREATED)
async def add_raster_style_share(
    collection_id: str,
    style_id: str,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    c = await collections_crud.get_collection(db, collection_id)
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")
    ensure_raster_collection(c)
    if not await can_edit_collection(db, c, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    rid = raster_style_resource_id(collection_id, style_id)
    share = await resource_share_crud.add_share(db, RESOURCE_TYPE_STYLE, rid, payload.username, payload.role)
    if not share:
        raise HTTPException(status_code=404, detail="User not found")
    return ShareRead(username=share.username, role=share.role)
