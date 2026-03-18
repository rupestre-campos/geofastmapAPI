"""API routes for system basemaps (under /styles/basemaps)."""

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.crud import basemaps as basemaps_crud
from app.db.session import get_db
from app.models.user import User
from app.schemas.basemap import BasemapCreate, BasemapList, BasemapRead, BasemapUpdate

router = APIRouter(include_in_schema=False)

BASEMAP_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_basemap_id(basemap_id: str) -> None:
    if not basemap_id or not BASEMAP_ID_PATTERN.match(basemap_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Basemap id must be non-empty and contain only letters, numbers, hyphen, underscore",
        )


def _to_read(b) -> BasemapRead:
    return BasemapRead(
        id=b.id,
        name=b.name,
        copyright=b.copyright,
        min_zoom=b.min_zoom,
        max_zoom=b.max_zoom,
        tiles=b.tiles or [],
        labels=b.labels,
        sort_order=b.sort_order,
    )


@router.get("", response_model=BasemapList, summary="List basemaps")
async def list_basemaps(db: AsyncSession = Depends(get_db)) -> BasemapList:
    """List all basemaps (for map selector and admin)."""
    items = await basemaps_crud.list_basemaps(db)
    return BasemapList(basemaps=[_to_read(b) for b in items])


@router.get("/{basemap_id}", response_model=BasemapRead, summary="Get basemap")
async def get_basemap(
    basemap_id: str,
    db: AsyncSession = Depends(get_db),
) -> BasemapRead:
    """Get a basemap by id."""
    b = await basemaps_crud.get_basemap(db, basemap_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Basemap not found")
    return _to_read(b)


@router.post("", response_model=BasemapRead, status_code=status.HTTP_201_CREATED, summary="Create basemap")
async def create_basemap(
    payload: BasemapCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> BasemapRead:
    """Create a new basemap."""
    _validate_basemap_id(payload.id)
    existing = await basemaps_crud.get_basemap(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A basemap with this id already exists",
        )
    b = await basemaps_crud.create_basemap(
        db,
        id=payload.id,
        name=payload.name,
        tiles=payload.tiles,
        copyright=payload.copyright,
        min_zoom=payload.min_zoom,
        max_zoom=payload.max_zoom,
        labels=payload.labels,
        sort_order=payload.sort_order,
    )
    return _to_read(b)


@router.put("/{basemap_id}", response_model=BasemapRead, summary="Update basemap")
async def update_basemap(
    basemap_id: str,
    payload: BasemapUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> BasemapRead:
    """Replace basemap (all fields). For partial update use PATCH."""
    b = await basemaps_crud.get_basemap(db, basemap_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Basemap not found")
    # PUT: replace with payload; use payload fields if set, else keep current
    name = payload.name if payload.name is not None else b.name
    copyright_val = payload.copyright if payload.copyright is not None else b.copyright
    min_zoom = payload.min_zoom if payload.min_zoom is not None else b.min_zoom
    max_zoom = payload.max_zoom if payload.max_zoom is not None else b.max_zoom
    tiles = payload.tiles if payload.tiles is not None else b.tiles
    labels = payload.labels if payload.labels is not None else b.labels
    sort_order = payload.sort_order if payload.sort_order is not None else b.sort_order
    updated = await basemaps_crud.update_basemap(
        db,
        basemap_id,
        name=name,
        copyright=copyright_val,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tiles=tiles,
        labels=labels,
        sort_order=sort_order,
    )
    return _to_read(updated)


@router.patch("/{basemap_id}", response_model=BasemapRead, summary="Patch basemap")
async def patch_basemap(
    basemap_id: str,
    payload: BasemapUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> BasemapRead:
    """Partially update a basemap."""
    b = await basemaps_crud.get_basemap(db, basemap_id)
    if not b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Basemap not found")
    updated = await basemaps_crud.update_basemap(
        db,
        basemap_id,
        name=payload.name,
        copyright=payload.copyright,
        min_zoom=payload.min_zoom,
        max_zoom=payload.max_zoom,
        tiles=payload.tiles,
        labels=payload.labels,
        sort_order=payload.sort_order,
    )
    return _to_read(updated)


@router.delete("/{basemap_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete basemap")
async def delete_basemap(
    basemap_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Delete a basemap."""
    deleted = await basemaps_crud.delete_basemap(db, basemap_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Basemap not found")
