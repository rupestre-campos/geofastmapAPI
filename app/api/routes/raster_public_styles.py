"""Public raster style presets (global Titiler parameter sets, collection_id == '')."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, get_current_user_required
from app.core.permissions import can_see_raster_style
from app.crud import raster_styles as raster_styles_crud
from app.db.session import get_db
from app.models.user import User
from app.schemas.ogc import Link
from app.schemas.raster_style import (
    RasterStyleCreate,
    RasterStyleList,
    RasterStylePatch,
    RasterStyleRead,
    RasterStyleReplace,
)

router = APIRouter()

RASTER_STYLE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _to_read(base: str, s) -> RasterStyleRead:
    return RasterStyleRead(
        id=s.id,
        title=s.title,
        collection_id=s.collection_id or "",
        is_default=s.is_default,
        style_spec=s.style_spec,
        visibility=getattr(s, "visibility", None) or "private",
        created_at=s.created_at,
        updated_at=s.updated_at,
        links=[
            Link(
                href=f"{base}/raster-styles/{s.id}",
                rel="self",
                type="application/json",
            )
        ],
    )


def _validate_style_id(style_id: str) -> None:
    if not style_id or not RASTER_STYLE_ID_PATTERN.match(style_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Style id must be non-empty and contain only letters, numbers, hyphen, underscore",
        )


@router.get(
    "",
    response_model=RasterStyleList,
    summary="List public raster style presets",
)
async def list_public_raster_styles_api(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> RasterStyleList:
    base = _base_url(request)
    items = await raster_styles_crud.list_public_raster_styles(db)
    visible: list = []
    for s in items:
        if await can_see_raster_style(
            db,
            getattr(s, "owner_id", None),
            getattr(s, "visibility", "private"),
            raster_styles_crud.PUBLIC_COLLECTION_ID,
            s.id,
            current_user,
        ):
            visible.append(s)
    return RasterStyleList(
        styles=[_to_read(base, s) for s in visible],
        links=[
            Link(href=f"{base}/raster-styles", rel="self", type="application/json"),
        ],
    )


@router.get(
    "/{style_id}",
    response_model=RasterStyleRead,
    summary="Get a public raster style preset",
)
async def get_public_raster_style_api(
    request: Request,
    style_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> RasterStyleRead:
    s = await raster_styles_crud.get_public_raster_style(db, style_id)
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if not await can_see_raster_style(
        db,
        getattr(s, "owner_id", None),
        getattr(s, "visibility", "private"),
        raster_styles_crud.PUBLIC_COLLECTION_ID,
        style_id,
        current_user,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _to_read(_base_url(request), s)


@router.post(
    "",
    response_model=RasterStyleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a public raster style preset",
)
async def create_public_raster_style_api(
    request: Request,
    payload: RasterStyleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> RasterStyleRead:
    _validate_style_id(payload.id)
    existing = await raster_styles_crud.get_public_raster_style(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A public raster style with this id already exists",
        )
    vis = payload.visibility or "public"
    s = await raster_styles_crud.upsert_raster_style(
        db,
        collection_id=raster_styles_crud.PUBLIC_COLLECTION_ID,
        style_id=payload.id,
        title=payload.title,
        style_spec=payload.style_spec,
        owner_id=current_user.id,
        set_default=False,
        visibility=vis,
    )
    return _to_read(_base_url(request), s)


@router.put(
    "/{style_id}",
    response_model=RasterStyleRead,
    summary="Replace a public raster style preset",
)
async def replace_public_raster_style_api(
    request: Request,
    style_id: str,
    payload: RasterStyleReplace,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> RasterStyleRead:
    cur = await raster_styles_crud.get_public_raster_style(db, style_id)
    if not cur:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if (cur.owner_id or 0) != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    s = await raster_styles_crud.upsert_raster_style(
        db,
        collection_id=raster_styles_crud.PUBLIC_COLLECTION_ID,
        style_id=style_id,
        title=payload.title,
        style_spec=payload.style_spec,
        owner_id=cur.owner_id,
        set_default=False,
    )
    return _to_read(_base_url(request), s)


@router.patch(
    "/{style_id}",
    response_model=RasterStyleRead,
    summary="Patch a public raster style preset",
)
async def patch_public_raster_style_api(
    request: Request,
    style_id: str,
    payload: RasterStylePatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> RasterStyleRead:
    cur = await raster_styles_crud.get_public_raster_style(db, style_id)
    if not cur:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if (cur.owner_id or 0) != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    s = await raster_styles_crud.upsert_raster_style(
        db,
        collection_id=raster_styles_crud.PUBLIC_COLLECTION_ID,
        style_id=style_id,
        title=payload.title if payload.title is not None else cur.title,
        style_spec=payload.style_spec if payload.style_spec is not None else cur.style_spec,
        owner_id=cur.owner_id,
        set_default=False,
        visibility=payload.visibility if payload.visibility is not None else None,
    )
    return _to_read(_base_url(request), s)
