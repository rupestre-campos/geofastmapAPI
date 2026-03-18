"""OGC API - Styles: public styles and collection-specific styles."""

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.crud import resource_share as resource_share_crud
from app.crud import styles as styles_crud
from app.db.session import get_db
from app.models.style import Style
from app.models.user import User
from app.models.resource_share import RESOURCE_TYPE_STYLE
from app.schemas.resource_share import ShareAdd, ShareRead
from app.schemas.ogc import Link
from app.schemas.style import (
    StyleCreate,
    StyleList,
    StylePatch,
    StyleRead,
    StyleReplace,
    default_style_spec,
)

router = APIRouter()

# Style id: slug (alphanumeric, hyphen, underscore)
STYLE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _style_links(base: str, collection_id: str, style_id: str, is_public: bool) -> list[Link]:
    if is_public:
        return [
            Link(href=f"{base}/styles/{style_id}", rel="self", type="application/json"),
        ]
    return [
        Link(href=f"{base}/collections/{collection_id}/styles/{style_id}", rel="self", type="application/json"),
        Link(href=f"{base}/collections/{collection_id}/styles", rel="collection", type="application/json"),
    ]


def _style_to_read(base: str, s: Style) -> StyleRead:
    is_public = s.collection_id == ""
    cid = s.collection_id or None
    return StyleRead(
        id=s.id,
        title=s.title,
        collection_id=cid,
        is_default=s.is_default,
        style_spec=s.style_spec,
        visibility=getattr(s, "visibility", None) or "private",
        created_at=s.created_at,
        updated_at=s.updated_at,
        links=_style_links(base, s.collection_id, s.id, is_public),
    )


def _validate_style_id(style_id: str) -> None:
    if not style_id or not STYLE_ID_PATTERN.match(style_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Style id must be non-empty and contain only letters, numbers, hyphen, underscore",
        )


def _normalize_spec(spec: dict | None) -> dict:
    if not spec:
        return default_style_spec()
    d = default_style_spec()
    for k in d:
        if k in spec:
            d[k] = spec[k]
    # Preserve extended keys so load/save round-trip works (zoom stops, pointOpacity, and advanced rules).
    for key in (
        "pointOpacity",
        "fillOpacityZoom",
        "lineWidthZoom",
        "lineOpacityZoom",
        "pointSizeZoom",
        "pointOpacityZoom",
        "rules",
    ):
        if key in spec and spec[key] is not None:
            d[key] = spec[key]
    return d


# ---------- Public styles (GET list, GET one, POST, PUT, PATCH, DELETE) ----------


@router.get(
    "",
    response_model=StyleList,
    summary="List public styles",
    description="OGC API - Styles: list named styles not tied to any collection (reusable with any layer).",
)
async def list_styles(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StyleList:
    base = _base_url(request)
    items = await styles_crud.list_public_styles(db)
    return StyleList(
        styles=[_style_to_read(base, s) for s in items],
        links=[
            Link(href=f"{base}/styles", rel="self", type="application/json"),
        ],
    )


@router.get(
    "/{style_id}",
    response_model=StyleRead,
    summary="Get public style",
)
async def get_style(
    request: Request,
    style_id: str,
    db: AsyncSession = Depends(get_db),
) -> StyleRead:
    style_obj = await styles_crud.get_public_style(db, style_id)
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _style_to_read(_base_url(request), style_obj)


@router.post(
    "",
    response_model=StyleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create public style",
)
async def create_style(
    request: Request,
    payload: StyleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> StyleRead:
    _validate_style_id(payload.id)
    existing = await styles_crud.get_public_style(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A public style with this id already exists",
        )
    spec = _normalize_spec(payload.style_spec)
    style_obj = await styles_crud.create_style(
        db,
        style_id=payload.id,
        style_spec=spec,
        title=payload.title,
        collection_id=styles_crud.PUBLIC_COLLECTION_ID,
        set_default=False,
        owner_id=current_user.id,
        visibility="private",
    )
    return _style_to_read(_base_url(request), style_obj)


@router.put(
    "/{style_id}",
    response_model=StyleRead,
    summary="Replace public style",
)
async def replace_style(
    request: Request,
    style_id: str,
    payload: StyleReplace,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> StyleRead:
    style_obj = await styles_crud.replace_public_style(
        db,
        style_id=style_id,
        style_spec=_normalize_spec(payload.style_spec),
        title=payload.title,
    )
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _style_to_read(_base_url(request), style_obj)


@router.patch(
    "/{style_id}",
    response_model=StyleRead,
    summary="Patch public style",
)
async def patch_style(
    request: Request,
    style_id: str,
    payload: StylePatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> StyleRead:
    spec = payload.style_spec if payload.style_spec is None else _normalize_spec(payload.style_spec)
    style_obj = await styles_crud.patch_public_style(
        db,
        style_id=style_id,
        title=payload.title,
        style_spec=spec,
        visibility=payload.visibility,
    )
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _style_to_read(_base_url(request), style_obj)


@router.get(
    "/{style_id}/shares",
    response_model=list[ShareRead],
    summary="List shares for a public style",
)
async def list_style_shares(
    style_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    style_obj = await styles_crud.get_public_style(db, style_id)
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if (style_obj.owner_id or 0) != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_STYLE, style_id)
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post(
    "/{style_id}/shares",
    response_model=ShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add share for a public style",
)
async def add_style_share(
    style_id: str,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    style_obj = await styles_crud.get_public_style(db, style_id)
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if (style_obj.owner_id or 0) != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    share = await resource_share_crud.add_share(
        db, RESOURCE_TYPE_STYLE, style_id, payload.username, payload.role
    )
    if not share:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return ShareRead(username=share.username, role=share.role)


@router.delete(
    "/{style_id}/shares/{username:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove share for a public style",
)
async def remove_style_share(
    style_id: str,
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    style_obj = await styles_crud.get_public_style(db, style_id)
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    if (style_obj.owner_id or 0) != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_STYLE, style_id, username)


@router.delete(
    "/{style_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete public style",
)
async def delete_style(
    style_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    deleted = await styles_crud.delete_style(db, styles_crud.PUBLIC_COLLECTION_ID, style_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
