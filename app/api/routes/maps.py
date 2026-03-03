"""User-created maps: gallery, create/edit, view (no geometry editors)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import maps as maps_crud
from app.db.session import get_db
from app.schemas.map import MapCreate, MapUpdate
from app.utils.thumbnail import image_to_thumbnail

router = APIRouter()

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _thumbnail_url(base: str, map_id: uuid.UUID) -> str:
    return f"{base}/maps/{map_id}/thumbnail"


def _map_to_read(m, base: str | None = None) -> dict:
    thumbnail = m.thumbnail
    if m.thumbnail_data and base:
        thumbnail = _thumbnail_url(base, m.id)
    return {
        "id": str(m.id),
        "name": m.name,
        "description": m.description,
        "thumbnail": thumbnail,
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
    base = _base_url(request)
    if wants_html(request):
        items = [_map_to_read(m, base) for m in maps_list]
        return html_response(
            "maps_gallery.html",
            base=base,
            maps=items,
        )
    return {"maps": [_map_to_read(m, base) for m in maps_list]}


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
    request: Request,
    data: MapCreate,
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.create_map(db, data)
    return _map_to_read(row, _base_url(request))


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
    base = _base_url(request)
    if wants_html(request):
        settings = get_settings()
        thumb_url = _thumbnail_url(base, row.id) if row.thumbnail_data else (row.thumbnail or "")
        return html_response(
            "map_view.html",
            base=base,
            map_id=str(row.id),
            map_name=row.name,
            map_description=row.description or "",
            map_thumbnail=thumb_url,
            map_definition=row.definition or {"layers": []},
            google_maps_api_key=settings.google_maps_api_key or "",
        )
    return _map_to_read(row, base)


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
    thumb_url = _thumbnail_url(base, row.id) if row.thumbnail_data else (row.thumbnail or "")
    settings = get_settings()
    return html_response(
        "map_edit.html",
        base=base,
        map_id=str(row.id),
        map_name=row.name,
        map_description=row.description or "",
        map_thumbnail=thumb_url,
        map_definition=row.definition or {"layers": []},
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.put(
    "/{map_id}",
    summary="Update map",
)
async def update_map(
    request: Request,
    map_id: uuid.UUID,
    data: MapUpdate,
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    payload = data.model_dump(exclude_unset=True)
    if payload.get("thumbnail") == _thumbnail_url(base, map_id):
        payload.pop("thumbnail", None)
    if payload:
        data = MapUpdate(**payload)
    row = await maps_crud.update_map(db, map_id, data if payload else MapUpdate())
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    return _map_to_read(row, base)


@router.get(
    "/{map_id}/thumbnail",
    summary="Get map thumbnail image",
    description="Returns the uploaded thumbnail as JPEG, or 404 if none.",
    responses={404: {"description": "Map or thumbnail not found"}},
)
async def get_map_thumbnail(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.get_map(db, map_id)
    if not row or not row.thumbnail_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found")
    return Response(content=row.thumbnail_data, media_type="image/jpeg")


@router.post(
    "/{map_id}/thumbnail",
    summary="Upload map thumbnail",
    description="Upload an image (JPEG, PNG, WebP, GIF). It is converted to a thumbnail and stored.",
)
async def upload_map_thumbnail(
    request: Request,
    map_id: uuid.UUID,
    file: UploadFile = File(..., description="Image file (JPEG, PNG, WebP, GIF). Max 5MB."),
    db: AsyncSession = Depends(get_db),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_IMAGE_CONTENT_TYPES)}",
        )
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large. Max {MAX_UPLOAD_BYTES // (1024*1024)}MB.",
        )
    try:
        thumb_bytes = image_to_thumbnail(data, content_type)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid image: {e!s}")
    updated = await maps_crud.set_map_thumbnail_data(db, map_id, thumb_bytes)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    base = _base_url(request)
    return {"thumbnail": _thumbnail_url(base, map_id)}


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
