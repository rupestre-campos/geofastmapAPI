"""User-created maps: gallery, create/edit, view (no geometry editors)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.core.permissions import can_edit_collection, can_edit_map, can_see_map
from app.crud import collections as collections_crud
from app.crud import maps as maps_crud
from app.crud import resource_share as resource_share_crud
from app.crud import user as user_crud
from app.db.session import get_db
from app.models.collection import VISIBILITY_PUBLIC
from app.models.resource_share import RESOURCE_TYPE_MAP
from app.schemas.map import MapCreate, MapUpdate
from app.schemas.resource_share import ShareAdd, ShareRead
from app.utils.thumbnail import image_to_thumbnail

router = APIRouter()

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _thumbnail_url(base: str, map_id: uuid.UUID) -> str:
    return f"{base}/maps/{map_id}/thumbnail"


def _map_layer_collection_ids(definition: dict | None) -> list[str]:
    """Unique collection ids from map definition layers, in order."""
    layers = (definition or {}).get("layers") or []
    out: list[str] = []
    seen: set[str] = set()
    for lyr in layers:
        if not isinstance(lyr, dict):
            continue
        cid = lyr.get("collection_id") or lyr.get("collectionId")
        if cid and isinstance(cid, str) and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


async def _can_edit_by_collection_for_map_layers(
    db: AsyncSession,
    definition: dict | None,
    current_user,
) -> dict[str, bool]:
    """collection_id -> whether the user may edit features in that collection (inline Edit feature on map)."""
    out: dict[str, bool] = {}
    for lyr in (definition or {}).get("layers") or []:
        cid = lyr.get("collection_id") or lyr.get("collectionId")
        if not cid or cid in out:
            continue
        coll = await collections_crud.get_collection(db, cid)
        if coll:
            out[cid] = await can_edit_collection(db, coll, current_user)
        else:
            out[cid] = False
    return out


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


def _collection_ids_from_definition(definition: dict) -> list[str]:
    """Extract unique collection ids from map definition layers (collection_id or collectionId)."""
    layers = definition.get("layers") or []
    seen: set[str] = set()
    out: list[str] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        cid = layer.get("collection_id") or layer.get("collectionId")
        if cid and isinstance(cid, str) and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


async def _check_map_public_layers(
    db: AsyncSession,
    definition: dict,
    current_user,
) -> tuple[list[dict], list[dict]]:
    """
    When setting map visibility to public: return (blocking, suggest).
    - blocking: collections that are not public and current_user cannot edit (viewer cannot make map public).
    - suggest: collections that are not public but current_user can edit (prompt owner to make them public).
    Each item is {"id": str, "title": str}.
    """
    blocking: list[dict] = []
    suggest: list[dict] = []
    for cid in _collection_ids_from_definition(definition):
        collection = await collections_crud.get_collection(db, cid)
        if not collection:
            continue
        if getattr(collection, "visibility", "private") == VISIBILITY_PUBLIC:
            continue
        can_edit = await can_edit_collection(db, collection, current_user)
        item = {"id": cid, "title": (collection.title or cid)}
        if can_edit:
            suggest.append(item)
        else:
            blocking.append(item)
    return (blocking, suggest)


# ----- List & create -----


@router.get(
    "",
    summary="List maps",
    description="Returns all user-created maps. Use ?f=html for the gallery page.",
)
async def list_maps(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    limit: int = 100,
    offset: int = 0,
):
    maps_list = await maps_crud.list_maps(db, limit=limit, offset=offset, current_user=current_user)
    base = _base_url(request)
    if wants_html(request):
        owner_ids = [m.owner_id for m in maps_list if getattr(m, "owner_id", None) is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if owner_ids else {}
        can_edit_list = [await can_edit_map(db, m.owner_id, str(m.id), current_user) for m in maps_list]
        items = []
        for i, m in enumerate(maps_list):
            item = _map_to_read(m, base)
            item["owner_username"] = owner_names.get(m.owner_id) if getattr(m, "owner_id", None) else None
            item["can_edit"] = can_edit_list[i]
            items.append(item)
        return html_response(
            "maps_gallery.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            maps=items,
        )
    return {"maps": [_map_to_read(m, base) for m in maps_list]}


@router.get(
    "/new",
    summary="Create map form",
    description="HTML form to create a new map (name, description, thumbnail, layers).",
)
async def new_map_form(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html for the create map page.")
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required to create a map")
    base = _base_url(request)
    settings = get_settings()
    return html_response(
        "map_edit.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        map_id=None,
        map_name="",
        map_description="",
        map_thumbnail="",
        map_definition={"layers": []},
        collection_titles={},
        google_maps_api_key=settings.google_maps_api_key or "",
        can_edit_by_collection={},
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
    current_user=Depends(get_current_user_optional),
):
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required to create a map")
    row = await maps_crud.create_map(db, data, owner_id=current_user.id, visibility="private")
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
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_see_map(db, row.owner_id, getattr(row, "visibility", "private"), str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    base = _base_url(request)
    if wants_html(request):
        settings = get_settings()
        thumb_url = _thumbnail_url(base, row.id) if row.thumbnail_data else (row.thumbnail or "")
        owner_username = None
        if getattr(row, "owner_id", None):
            owner_username = (await user_crud.get_usernames_by_ids(db, [row.owner_id])).get(row.owner_id)
        can_edit = await can_edit_map(db, row.owner_id, str(row.id), current_user)
        can_edit_by_collection = await _can_edit_by_collection_for_map_layers(
            db, row.definition or {"layers": []}, current_user
        )
        collection_titles = await collections_crud.get_collection_titles_by_ids(
            db, _map_layer_collection_ids(row.definition)
        )
        return html_response(
            "map_view.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            map_id=str(row.id),
            map_name=row.name,
            map_description=row.description or "",
            map_thumbnail=thumb_url,
            map_definition=row.definition or {"layers": []},
            collection_titles=collection_titles,
            owner_username=owner_username,
            google_maps_api_key=settings.google_maps_api_key or "",
            can_edit_map=can_edit,
            can_edit_by_collection=can_edit_by_collection,
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
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html for the edit map page.")
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this map")
    base = _base_url(request)
    thumb_url = _thumbnail_url(base, row.id) if row.thumbnail_data else (row.thumbnail or "")
    settings = get_settings()
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_MAP, str(row.id))
    can_edit_by_collection = await _can_edit_by_collection_for_map_layers(
        db, row.definition or {"layers": []}, current_user
    )
    collection_titles = await collections_crud.get_collection_titles_by_ids(
        db, _map_layer_collection_ids(row.definition)
    )
    return html_response(
        "map_edit.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        map_id=str(row.id),
        map_name=row.name,
        map_description=row.description or "",
        map_thumbnail=thumb_url,
        map_definition=row.definition or {"layers": []},
        collection_titles=collection_titles,
        google_maps_api_key=settings.google_maps_api_key or "",
        visibility=getattr(row, "visibility", "private"),
        viewer_can_edit=getattr(row, "viewer_can_edit", False),
        shares=[{"username": u, "role": r} for u, r in shares],
        shares_url=f"{base}/maps/{row.id}/shares",
        patch_url=f"{base}/maps/{row.id}",
        resource_label="this map",
        show_viewer_edit=True,
        can_edit_by_collection=can_edit_by_collection,
    )


@router.get(
    "/{map_id}/check-public-layers",
    summary="Check which map layers are not public",
    description="Returns blocking and suggest lists for making the map public. Use before setting map visibility to public.",
)
async def check_map_public_layers(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    """Return { blocking: [...], suggest: [...] } for the map's saved definition."""
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    definition = row.definition or {}
    blocking, suggest = await _check_map_public_layers(db, definition, current_user)
    return {"blocking": blocking, "suggest": suggest}


@router.put(
    "/{map_id}",
    summary="Update map",
)
async def update_map(
    request: Request,
    map_id: uuid.UUID,
    data: MapUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    base = _base_url(request)
    payload = data.model_dump(exclude_unset=True)
    if payload.get("thumbnail") == _thumbnail_url(base, map_id):
        payload.pop("thumbnail", None)
    # If setting visibility to public, ensure user can make all layers public.
    if payload.get("visibility") == "public":
        definition = payload.get("definition") if "definition" in payload else (row.definition or {})
        blocking, _ = await _check_map_public_layers(db, definition, current_user)
        if blocking:
            names = ", ".join(f"{c['title']} ({c['id']})" for c in blocking)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": f"Cannot make map public: the following layers are not public and you do not have permission to change their visibility: {names}.",
                    "collections_blocking": blocking,
                },
            )
    if payload:
        data = MapUpdate(**payload)
    row = await maps_crud.update_map(db, map_id, data if payload else MapUpdate())
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    headers = {}
    if payload.get("visibility") == "public":
        _, suggest = await _check_map_public_layers(db, row.definition or {}, current_user)
        if suggest:
            headers["X-Suggest-Make-Public-Collections"] = json.dumps(suggest)
    if headers:
        return JSONResponse(content=_map_to_read(row, base), headers=headers)
    return _map_to_read(row, base)


@router.patch(
    "/{map_id}",
    summary="Partially update map (e.g. visibility)",
)
async def patch_map(
    request: Request,
    map_id: uuid.UUID,
    data: MapUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    """PATCH map with partial payload (e.g. only visibility)."""
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    base = _base_url(request)
    payload = data.model_dump(exclude_unset=True)
    if payload.get("thumbnail") == _thumbnail_url(base, map_id):
        payload.pop("thumbnail", None)
    # If setting visibility to public, ensure user can make all layers public (no viewer-only layers).
    if payload.get("visibility") == "public":
        definition = payload.get("definition") if "definition" in payload else (row.definition or {})
        blocking, suggest = await _check_map_public_layers(db, definition, current_user)
        if blocking:
            names = ", ".join(f"{c['title']} ({c['id']})" for c in blocking)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": f"Cannot make map public: the following layers are not public and you do not have permission to change their visibility: {names}.",
                    "collections_blocking": blocking,
                },
            )
    if payload:
        row = await maps_crud.update_map(db, map_id, MapUpdate(**payload))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    # If we just set visibility to public and some layers are not public but user can edit them, suggest making them public.
    headers: dict[str, str] = {}
    if payload.get("visibility") == "public":
        definition = row.definition or {}
        _, suggest = await _check_map_public_layers(db, definition, current_user)
        if suggest:
            headers["X-Suggest-Make-Public-Collections"] = json.dumps(suggest)
    if headers:
        return JSONResponse(content=_map_to_read(row, base), headers=headers)
    return _map_to_read(row, base)


@router.get(
    "/{map_id}/shares",
    response_model=list[ShareRead],
    summary="List shares for a map",
)
async def list_map_shares(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_MAP, str(map_id))
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post(
    "/{map_id}/shares",
    response_model=ShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add share for a map",
)
async def add_map_share(
    map_id: uuid.UUID,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    share = await resource_share_crud.add_share(
        db, RESOURCE_TYPE_MAP, str(map_id), payload.username, payload.role
    )
    if not share:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return ShareRead(username=share.username, role=share.role)


@router.delete(
    "/{map_id}/shares/{username:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove share for a map",
)
async def remove_map_share(
    map_id: uuid.UUID,
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_MAP, str(map_id), username)


@router.get(
    "/{map_id}/thumbnail",
    summary="Get map thumbnail image",
    description="Returns the uploaded thumbnail as JPEG, or 404 if none.",
    responses={404: {"description": "Map or thumbnail not found"}},
)
async def get_map_thumbnail(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row or not row.thumbnail_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thumbnail not found")
    if not await can_see_map(db, row.owner_id, getattr(row, "visibility", "private"), str(row.id), current_user):
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
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
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
    current_user=Depends(get_current_user_optional),
):
    row = await maps_crud.get_map(db, map_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if not await can_edit_map(db, row.owner_id, str(row.id), current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    ok = await maps_crud.delete_map(db, map_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
