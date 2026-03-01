"""OGC API - Features collections: list, get, create, replace, patch, delete."""

from collections.abc import Sequence
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.crud import styles as styles_crud
from app.core.html import html_response, wants_html
from app.db.session import get_db
from app.schemas.collection import (
    CollectionCreate,
    CollectionPatch,
    CollectionRead,
    CollectionReplace,
    CollectionsList,
    ExtentRecomputeResponse,
    clamp_bbox,
)
from app.schemas.ogc import Link

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _collection_links(base: str, collection_id: str, default_style_id: str | None = None) -> list[Link]:
    links = [
        Link(href=f"{base}/collections/{collection_id}", rel="self", type="application/json"),
        Link(href=f"{base}/collections/{collection_id}/items", rel="items", type="application/geo+json"),
        Link(href=f"{base}/collections/{collection_id}/tiles", rel="tiles", type="application/json", title="TileJSON for this collection"),
        Link(href=f"{base}/collections/{collection_id}/styles", rel="styles", type="application/json", title="Styles for this collection"),
    ]
    if default_style_id:
        links.append(
            Link(href=f"{base}/collections/{collection_id}/styles/{default_style_id}", rel="style", type="application/json", title="Default style"),
        )
    return links


@router.get(
    "",
    summary="List collections",
    description="Use ?f=html or Accept: text/html for HTML.",
)
async def list_collections(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    items_list: Sequence = await collections_crud.list_collections(db)
    collections_out = []
    for item in items_list:
        out = CollectionRead.model_validate(item)
        collections_out.append(
            out.model_copy(update={"links": _collection_links(base, item.id)}),
        )
    if wants_html(request):
        # Use stored extent per collection (no heavy query over features table); clamp to valid range for map
        collections_with_bbox = []
        for item in items_list:
            if not item.extent or not item.extent.get("bbox"):
                continue
            box = item.extent["bbox"][0]
            if len(box) >= 4:
                minx, miny, maxx, maxy = clamp_bbox(box[0], box[1], box[2], box[3])
                collections_with_bbox.append({
                    "id": item.id,
                    "title": item.title or "",
                    "description": item.description or "",
                    "bbox": [minx, miny, maxx, maxy],
                })
        return html_response(
            "collections.html",
            base=base,
            collections=[{"id": c.id, "title": c.title} for c in collections_out],
            collections_with_bbox=collections_with_bbox,
        )
    return CollectionsList(
        collections=collections_out,
        links=[
            Link(href=f"{base}/collections", rel="self", type="application/json"),
            Link(href=f"{base}/collections?f=html", rel="alternate", type="text/html"),
        ],
    )


@router.get(
    "/{collection_id}",
    summary="Get collection by id",
    description="Use ?f=html or Accept: text/html for HTML (map, tile actions).",
)
async def get_collection(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    default_style = await styles_crud.get_default_style(db, collection_id)
    default_style_id = default_style.id if default_style else None
    out = CollectionRead.model_validate(collection)
    if wants_html(request):
        rec = await tiles_crud.get_collection_tiles(db, collection_id)
        has_static_tiles = bool(rec and rec.pmtiles_path and Path(rec.pmtiles_path).exists())
        settings = get_settings()
        return html_response(
            "collection.html",
            base=base,
            collection={"id": out.id, "title": out.title, "description": out.description},
            extent_geojson=out.extent.model_dump() if out.extent else None,
            has_static_tiles=has_static_tiles,
            google_maps_api_key=settings.google_maps_api_key or "",
            default_style={"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec} if default_style else None,
            collection_styles_url=f"{base}/collections/{collection_id}/styles",
        )
    return out.model_copy(
        update={
            "links": _collection_links(base, collection_id, default_style_id)
            + [Link(href=f"{base}/collections/{collection_id}?f=html", rel="alternate", type="text/html")],
        },
    )


@router.put(
    "/{collection_id}",
    response_model=CollectionRead,
    summary="Replace collection metadata",
)
async def replace_collection(
    request: Request,
    collection_id: str,
    payload: CollectionReplace,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    collection = await collections_crud.replace_collection(db, collection_id, payload)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    out = CollectionRead.model_validate(collection)
    return out.model_copy(update={"links": _collection_links(base, collection_id)})


@router.patch(
    "/{collection_id}",
    response_model=CollectionRead,
    summary="Partially update collection",
)
async def patch_collection(
    request: Request,
    collection_id: str,
    payload: CollectionPatch,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    collection = await collections_crud.patch_collection(db, collection_id, payload)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    out = CollectionRead.model_validate(collection)
    return out.model_copy(update={"links": _collection_links(base, collection_id)})


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create collection",
)
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
) -> CollectionRead:
    existing = await collections_crud.get_collection(db, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection with this id already exists",
        )
    collection = await collections_crud.create_collection(db, payload)
    return CollectionRead.model_validate(collection)


@router.post(
    "/{collection_id}/extent/recompute",
    response_model=ExtentRecomputeResponse,
    summary="Recompute extent from features",
    description="Compute bounding box from feature geometries, update the collection's stored extent, and return it. Use after bulk import or when extent is stale. Returns extent null if the collection has no features with geometry.",
)
async def recompute_collection_extent(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> ExtentRecomputeResponse:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    extent = await collections_crud.recompute_and_update_collection_extent(db, collection_id)
    return ExtentRecomputeResponse(extent=extent)


@router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a collection",
)
async def delete_collection(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await collections_crud.delete_collection(db, collection_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
