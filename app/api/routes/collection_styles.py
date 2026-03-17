"""OGC API - Styles: collection-specific styles (list, get, create, replace, patch, delete)."""

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collections as collections_crud
from app.crud import styles as styles_crud
from app.db.session import get_db
from app.models.style import Style
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

STYLE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _style_links(base: str, collection_id: str, style_id: str) -> list[Link]:
    return [
        Link(href=f"{base}/collections/{collection_id}/styles/{style_id}", rel="self", type="application/json"),
        Link(href=f"{base}/collections/{collection_id}/styles", rel="collection", type="application/json"),
    ]


def _style_to_read(base: str, s: Style, collection_id: str) -> StyleRead:
    return StyleRead(
        id=s.id,
        title=s.title,
        collection_id=collection_id or None,
        is_default=s.is_default,
        style_spec=s.style_spec,
        created_at=s.created_at,
        updated_at=s.updated_at,
        links=_style_links(base, collection_id, s.id),
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


@router.get(
    "/{collection_id}/styles",
    summary="List styles for collection",
    description="List styles saved for this collection. Use the default style link on the collection for the initial style.",
)
async def list_collection_styles(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> StyleList:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    base = _base_url(request)
    items = await styles_crud.list_collection_styles(db, collection_id)
    return StyleList(
        styles=[_style_to_read(base, s, collection_id) for s in items],
        links=[
            Link(href=f"{base}/collections/{collection_id}/styles", rel="self", type="application/json"),
            Link(href=f"{base}/collections/{collection_id}", rel="collection", type="application/json"),
        ],
    )


@router.get(
    "/{collection_id}/styles/{style_id}",
    response_model=StyleRead,
    summary="Get style (collection or public)",
)
async def get_collection_style(
    request: Request,
    collection_id: str,
    style_id: str,
    db: AsyncSession = Depends(get_db),
) -> StyleRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    base = _base_url(request)
    style_obj = await styles_crud.get_collection_style(db, collection_id, style_id)
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    cid = style_obj.collection_id or collection_id
    return _style_to_read(base, style_obj, cid)


@router.post(
    "/{collection_id}/styles",
    response_model=StyleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save style (new or replace)",
)
async def save_collection_style(
    request: Request,
    collection_id: str,
    payload: StyleCreate,
    db: AsyncSession = Depends(get_db),
) -> StyleRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    _validate_style_id(payload.id)
    base = _base_url(request)
    spec = _normalize_spec(payload.style_spec)
    existing = await styles_crud.get_collection_style(db, collection_id, payload.id)
    if existing and existing.collection_id == collection_id:
        style_obj = await styles_crud.replace_style(
            db,
            collection_id=collection_id,
            style_id=payload.id,
            style_spec=spec,
            title=payload.title,
            set_default=payload.set_default,
        )
    else:
        style_obj = await styles_crud.create_style(
            db,
            style_id=payload.id,
            style_spec=spec,
            title=payload.title,
            collection_id=collection_id,
            set_default=payload.set_default,
        )
    return _style_to_read(base, style_obj, collection_id)


@router.put(
    "/{collection_id}/styles/{style_id}",
    response_model=StyleRead,
    summary="Replace collection style",
)
async def replace_collection_style(
    request: Request,
    collection_id: str,
    style_id: str,
    payload: StyleReplace,
    db: AsyncSession = Depends(get_db),
) -> StyleRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    base = _base_url(request)
    style_obj = await styles_crud.replace_style(
        db,
        collection_id=collection_id,
        style_id=style_id,
        style_spec=_normalize_spec(payload.style_spec),
        title=payload.title,
        set_default=payload.set_default,
    )
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _style_to_read(base, style_obj, collection_id)


@router.patch(
    "/{collection_id}/styles/{style_id}",
    response_model=StyleRead,
    summary="Patch collection style",
)
async def patch_collection_style(
    request: Request,
    collection_id: str,
    style_id: str,
    payload: StylePatch,
    db: AsyncSession = Depends(get_db),
) -> StyleRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    spec = payload.style_spec if payload.style_spec is None else _normalize_spec(payload.style_spec)
    base = _base_url(request)
    style_obj = await styles_crud.patch_style(
        db,
        collection_id=collection_id,
        style_id=style_id,
        title=payload.title,
        style_spec=spec,
        set_default=payload.set_default,
    )
    if not style_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
    return _style_to_read(base, style_obj, collection_id)


@router.delete(
    "/{collection_id}/styles/{style_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete collection style",
)
async def delete_collection_style(
    collection_id: str,
    style_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    deleted = await styles_crud.delete_style(db, collection_id, style_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Style not found")
