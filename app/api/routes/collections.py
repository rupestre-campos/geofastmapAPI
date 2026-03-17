"""OGC API - Features collections: list, get, create, replace, patch, delete."""

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collection_tiles as tiles_crud
from app.services.tile_build_queue import get_latest_tile_build_job
from app.utils.geo import mvt_layer_name
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

# Reserved path segment: GET /collections/edit is the edit-collections (create form) page.
COLLECTION_ID_RESERVED = "edit"


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
    description="q=full-text (id/title/description), bbox=minx,miny,maxx,maxy, sortby, sortdesc, limit, offset. Use ?f=html for HTML.",
)
async def list_collections(
    request: Request,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, description="Full-text search in id, title, description."),
    bbox: str | None = Query(None, description="Bounding box minx,miny,maxx,maxy (WGS84). Collections whose extent intersects this bbox."),
    sortby: str | None = Query(None, description="Sort by: id, title, description, created_at."),
    sortdesc: bool = Query(False, description="Sort descending."),
    limit: int | None = Query(None, ge=1, le=1000, description="Max collections per page."),
    offset: int = Query(0, ge=0, description="Number of collections to skip."),
    has_static_tiles: bool = Query(False, description="If true, only list collections that have static tiles built (for map layer picker)."),
):
    base = _base_url(request)
    bbox_tuple: tuple[float, float, float, float] | None = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                pass
    # HTML: default limit for pagination (smaller for faster first load; use Search to change)
    if wants_html(request) and limit is None:
        limit = 20
    items_list, number_matched = await collections_crud.list_collections(
        db,
        q=q.strip() if q and q.strip() else None,
        bbox_tuple=bbox_tuple,
        sortby=sortby,
        sortdesc=sortdesc,
        limit=limit,
        offset=offset,
        has_static_tiles=has_static_tiles,
    )
    collections_out = []
    for item in items_list:
        out = CollectionRead.model_validate(item)
        collections_out.append(
            out.model_copy(update={"links": _collection_links(base, item.id)}),
        )
    if wants_html(request):
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
        limit_val = limit or 100
        prev_page_url = None
        next_page_url = None
        base_path = f"{base}/collections"
        query_params = dict(request.query_params)
        query_params.setdefault("f", "html")
        if offset > 0:
            prev_params = {**query_params, "offset": str(max(0, offset - limit_val)), "limit": str(limit_val)}
            prev_page_url = base_path + "?" + urlencode(sorted(prev_params.items()))
        if offset + len(items_list) < number_matched:
            next_params = {**query_params, "offset": str(offset + limit_val), "limit": str(limit_val)}
            next_page_url = base_path + "?" + urlencode(sorted(next_params.items()))
        # JSON link: same query params without f=html
        q_params = dict(request.query_params)
        q_params.pop("f", None)
        collections_url_json = base_path + ("?" + urlencode(sorted(q_params.items())) if q_params else "")

        return html_response(
            "collections.html",
            base=base,
            collections=[
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "feature_count": c.feature_count,
                }
                for c in collections_out
            ],
            collections_with_bbox=collections_with_bbox,
            number_matched=number_matched,
            number_returned=len(collections_out),
            limit=limit_val,
            offset=offset,
            prev_page_url=prev_page_url,
            next_page_url=next_page_url,
            q=q or "",
            bbox=bbox or "",
            sortby=sortby or "",
            sortdesc=sortdesc,
            collections_url_json=collections_url_json,
        )
    return CollectionsList(
        collections=collections_out,
        links=[
            Link(href=f"{base}/collections", rel="self", type="application/json"),
            Link(href=f"{base}/collections?f=html", rel="alternate", type="text/html"),
        ],
    )


@router.get(
    "/edit",
    summary="Edit collections (HTML): create new collection",
    description="HTML page with form to create a new collection. Use ?f=html. Path is /collections/edit to avoid conflicting with collection id.",
)
async def edit_collections_form(request: Request):
    """Serve the edit-collections page (create collection form)."""
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    base = _base_url(request)
    return html_response("collections_edit.html", base=base)


@router.get(
    "/{collection_id}/edit",
    summary="Edit collection (HTML form)",
    description="HTML page to edit collection metadata and bbox (map with polygon editor). Use ?f=html.",
)
async def get_collection_edit_form(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve the collection edit page: map with GeoEditor for bbox, attributes (title, description, bbox inputs)."""
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    settings = get_settings()
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    has_static_tiles = bool(rec and rec.pmtiles_path and Path(rec.pmtiles_path).exists())
    static_minzoom = rec.minzoom if (rec and rec.minzoom is not None) else 0
    static_maxzoom = rec.maxzoom if (rec and rec.maxzoom is not None) else 14
    out = CollectionRead.model_validate(collection)
    default_style = await styles_crud.get_default_style(db, collection_id)
    tile_build_job = None
    if settings.bulk_queue_type == "redis":
        tile_build_job = get_latest_tile_build_job(collection_id)
    show_cancel_tile_build = (
        tile_build_job is not None and tile_build_job.status in ("pending", "running")
    )
    return html_response(
        "collection_edit.html",
        base=base,
        collection_id=collection_id,
        collection={"id": out.id, "title": out.title, "description": out.description},
        extent_geojson=out.extent.model_dump() if out.extent else None,
        has_static_tiles=has_static_tiles,
        static_minzoom=static_minzoom,
        static_maxzoom=static_maxzoom,
        tile_layer_id=mvt_layer_name(collection_id),
        google_maps_api_key=settings.google_maps_api_key or "",
        default_style={"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec} if default_style else None,
        collection_styles_url=f"{base}/collections/{collection_id}/styles",
        show_cancel_tile_build=show_cancel_tile_build,
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
        static_minzoom = rec.minzoom if (rec and rec.minzoom is not None) else 0
        static_maxzoom = rec.maxzoom if (rec and rec.maxzoom is not None) else 14
        settings = get_settings()
        return html_response(
            "collection.html",
            base=base,
            collection={"id": out.id, "title": out.title, "description": out.description},
            extent_geojson=out.extent.model_dump() if out.extent else None,
            has_static_tiles=has_static_tiles,
            static_minzoom=static_minzoom,
            static_maxzoom=static_maxzoom,
            tile_layer_id=mvt_layer_name(collection_id),
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
    if payload.id == COLLECTION_ID_RESERVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection id {COLLECTION_ID_RESERVED!r} is reserved (conflicts with /collections/edit route).",
        )
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
