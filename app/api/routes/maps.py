"""User-created maps: gallery, create/edit, view (no geometry editors)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import maps as maps_crud
from app.db.session import get_db
from app.schemas.map import MapCreate, MapUpdate

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _map_to_read(m) -> dict:
    return {
        "id": str(m.id),
        "name": m.name,
        "description": m.description,
        "thumbnail": m.thumbnail,
        "definition": m.definition or {},
        "created_at": m.created_at.isoformat() + "Z" if isinstance(m.created_at, datetime) else m.created_at,
        "updated_at": m.updated_at.isoformat() + "Z" if isinstance(m.updated_at, datetime) else m.updated_at,
    }


# ----- List & create -----


@router.get(
    "",
    summary="List maps",
    description="Returns all user-created maps. Use ?f=html for the gallery page.",
)
async def list_maps(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    maps_list = await maps_crud.list_maps(db, limit=limit, offset=offset)
    if wants_html(request):
        base = _base_url(request)
        items = [_map_to_read(m) for m in maps_list]
        return html_response(
            "maps_gallery.html",
            base=base,
            maps=items,
        )
    return {"maps": [_map_to_read(m) for m in maps_list]}


@router.get(
    "/new",
    summary="Create map form",
    description="HTML form to create a new map (name, description, thumbnail, layers).",
)
async def new_map_form(request: Request):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html for the create map page.")
    base = _base_url(request)
    settings = get_settings()
    return html_response(
        "map_edit.html",
        base=base,
        map_id=None,
        map_name="",
        map_description="",
        map_thumbnail="",
        map_definition={"layers": []},
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create map",
)
async def create_map(
    data: MapCreate,
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.create_map(db, data)
    return _map_to_read(row)


# ----- Single map: view, edit form, update, delete -----


@router.get(
    "/{map_id}",
    summary="Get or view map",
    description="Returns map JSON or HTML view page. Use ?f=html to visualize the map.",
)
async def get_map(
    request: Request,
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if wants_html(request):
        base = _base_url(request)
        settings = get_settings()
        return html_response(
            "map_view.html",
            base=base,
            map_id=str(row.id),
            map_name=row.name,
            map_description=row.description or "",
            map_thumbnail=row.thumbnail or "",
            map_definition=row.definition or {"layers": []},
            google_maps_api_key=settings.google_maps_api_key or "",
        )
    return _map_to_read(row)


@router.get(
    "/{map_id}/edit",
    summary="Edit map form",
    description="HTML form to edit map name, description, thumbnail, and layers.",
)
async def edit_map_form(
    request: Request,
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html for the edit map page.")
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    base = _base_url(request)
    settings = get_settings()
    return html_response(
        "map_edit.html",
        base=base,
        map_id=str(row.id),
        map_name=row.name,
        map_description=row.description or "",
        map_thumbnail=row.thumbnail or "",
        map_definition=row.definition or {"layers": []},
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.put(
    "/{map_id}",
    summary="Update map",
)
async def update_map(
    map_id: uuid.UUID,
    data: MapUpdate,
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.update_map(db, map_id, data)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    return _map_to_read(row)


@router.delete(
    "/{map_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete map",
)
async def delete_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    ok = await maps_crud.delete_map(db, map_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
