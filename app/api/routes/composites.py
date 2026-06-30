"""Composite (merged vector mosaic) collections: HTML pages and JSON API alias."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.api.routes.collections import _collection_read_with_extras
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.core.permissions import can_edit_collection
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.models.collection import COLLECTION_TYPE_COMPOSITE, COLLECTION_TYPE_VECTOR
from app.schemas.collection import (
    CollectionCreate,
    CollectionRead,
    CollectionsList,
    CompositeMember,
    Extent,
)
from app.schemas.ogc import Link
from app.services.composite_collections import (
    is_composite_collection,
    member_tile_status,
    parse_composite_members,
    validate_composite_members,
)
from app.services.collection_property_indexes import normalize_property_index_fields
from app.utils.geo import mvt_layer_name

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


class CompositeCreateBody(BaseModel):
    """Body for POST /composites (composite-only create alias)."""

    id: str = Field(..., description="Identifier of the composite collection.")
    title: str | None = None
    description: str | None = None
    extent: Extent | None = None
    composite_members: list[CompositeMember] | None = Field(
        default=None,
        description="Ordered member vector collection ids. Can be set later via PATCH.",
    )


async def _vector_picker(
    db: AsyncSession,
    *,
    current_user,
    exclude_id: str | None = None,
) -> list[dict[str, str]]:
    all_collections, _ = await collections_crud.list_collections(
        db,
        limit=500,
        offset=0,
        current_user=current_user,
        collection_type=COLLECTION_TYPE_VECTOR,
    )
    return [
        {"id": c.id, "title": c.title or c.id}
        for c in all_collections
        if c.id != exclude_id and getattr(c, "collection_type", "") != COLLECTION_TYPE_COMPOSITE
    ]


async def _composite_edit_context(
    db: AsyncSession,
    collection,
    *,
    current_user,
) -> dict:
    members = parse_composite_members(getattr(collection, "composite_members", None))
    member_status_list = await member_tile_status(db, members)
    has_static_tiles = any(s.get("has_static_tiles") for s in member_status_list)
    static_minzoom = min(
        (s["minzoom"] for s in member_status_list if s.get("minzoom") is not None),
        default=0,
    )
    static_maxzoom = max(
        (s["maxzoom"] for s in member_status_list if s.get("maxzoom") is not None),
        default=14,
    )
    picker = await _vector_picker(db, current_user=current_user, exclude_id=collection.id)
    out = CollectionRead.model_validate(collection)
    return {
        "collection_id": collection.id,
        "collection_title": collection.title or collection.id,
        "collection_description": collection.description or "",
        "property_index_fields": normalize_property_index_fields(
            getattr(collection, "property_index_fields", None)
        ),
        "member_status": member_status_list,
        "member_count": len(members),
        "vector_collections": picker,
        "has_static_tiles": has_static_tiles,
        "static_minzoom": static_minzoom,
        "static_maxzoom": static_maxzoom,
        "tile_layer_id": mvt_layer_name(collection.id),
        "extent_geojson": out.extent.model_dump() if out.extent else None,
    }


@router.get(
    "",
    summary="List composite collections",
    description="JSON list of composite collections, or HTML table with ?f=html.",
)
async def list_composites(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    q: str | None = Query(None, description="Full-text search in id, title, description."),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    base = _base_url(request)
    if wants_html(request) and current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if wants_html(request) and limit is None:
        limit = 50
    items_list, number_matched = await collections_crud.list_collections(
        db,
        q=q.strip() if q and q.strip() else None,
        limit=limit,
        offset=offset,
        current_user=current_user,
        collection_type=COLLECTION_TYPE_COMPOSITE,
    )
    if wants_html(request):
        rows = []
        for c in items_list:
            members = parse_composite_members(getattr(c, "composite_members", None))
            member_status_list = await member_tile_status(db, members)
            can_edit = await can_edit_collection(db, c, current_user)
            rows.append({
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "member_count": len(members),
                "has_merged_tiles": any(s.get("has_static_tiles") for s in member_status_list),
                "can_edit": can_edit,
            })
        limit_val = limit or 50
        prev_page_url = None
        next_page_url = None
        base_path = f"{base}/composites"
        query_params = dict(request.query_params)
        query_params.setdefault("f", "html")
        if offset > 0:
            prev_params = {**query_params, "offset": str(max(0, offset - limit_val)), "limit": str(limit_val)}
            prev_page_url = base_path + "?" + urlencode(sorted(prev_params.items()))
        if offset + len(items_list) < number_matched:
            next_params = {**query_params, "offset": str(offset + limit_val), "limit": str(limit_val)}
            next_page_url = base_path + "?" + urlencode(sorted(next_params.items()))
        return html_response(
            "composites_list.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            composites=rows,
            number_matched=number_matched,
            number_returned=len(rows),
            limit=limit_val,
            offset=offset,
            prev_page_url=prev_page_url,
            next_page_url=next_page_url,
            q=q or "",
        )
    collections_out = []
    for item in items_list:
        members = parse_composite_members(getattr(item, "composite_members", None))
        member_status_json = await member_tile_status(db, members)
        out = _collection_read_with_extras(
            item,
            base=base,
            collection_id=item.id,
            member_status=member_status_json,
        )
        collections_out.append(out)
    return CollectionsList(
        collections=collections_out,
        links=[
            Link(href=f"{base}/composites", rel="self", type="application/json"),
            Link(href=f"{base}/composites?f=html", rel="alternate", type="text/html"),
        ],
    )


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create composite collection",
    description=(
        "Create a merged vector mosaic collection. Same as POST /collections with "
        "collection_type composite. Provide composite_members to set members in one request, "
        "or PATCH them later."
    ),
)
async def create_composite(
    payload: CompositeCreateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> CollectionRead:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required to create a collection")
    existing = await collections_crud.get_collection(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection with this id already exists",
        )
    members = [{"collection_id": m.collection_id} for m in (payload.composite_members or [])]
    if members:
        await validate_composite_members(db, payload.id, members)
    create_payload = CollectionCreate(
        id=payload.id,
        title=payload.title,
        description=payload.description,
        extent=payload.extent,
        collection_type=COLLECTION_TYPE_COMPOSITE,
        composite_members=payload.composite_members,
    )
    collection = await collections_crud.create_collection(
        db, create_payload, owner_id=current_user.id, visibility="private"
    )
    base = _base_url(request)
    member_status_json = await member_tile_status(
        db, parse_composite_members(collection.composite_members)
    )
    return _collection_read_with_extras(
        collection,
        base=base,
        collection_id=collection.id,
        member_status=member_status_json,
    )


@router.get(
    "/new",
    summary="Create composite collection (HTML)",
)
async def composites_new_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    base = _base_url(request)
    picker = await _vector_picker(db, current_user=current_user)
    return html_response(
        "composites_new.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        vector_collections=picker,
    )


@router.get(
    "/{collection_id}/edit",
    summary="Edit composite collection (HTML)",
)
async def composites_edit_form(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not is_composite_collection(collection):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a composite collection")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    base = _base_url(request)
    ctx = await _composite_edit_context(db, collection, current_user=current_user)
    settings = get_settings()
    return html_response(
        "composites_edit.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        google_maps_api_key=settings.google_maps_api_key or "",
        **ctx,
    )
