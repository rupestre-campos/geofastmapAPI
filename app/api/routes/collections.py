"""OGC API - Features collections: list, get, create, replace, patch, delete."""

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.config import get_settings
from app.core.permissions import can_edit_collection, can_see_collection
from app.crud import collection_tiles as tiles_crud
from app.crud import user as user_crud
from app.services.tile_build_queue import get_latest_tile_build_job
from app.utils.geo import mvt_layer_name
from app.crud import collections as collections_crud
from app.services.raster_style_display import raster_style_viz_context
from app.services.raster_style_edit_context import get_raster_style_edit_context
from app.crud import resource_share as resource_share_crud
from app.crud import raster_styles as raster_styles_crud
from app.crud import styles as styles_crud
from app.core.html import html_response, wants_html
from app.db.session import get_db
from app.models.resource_share import RESOURCE_TYPE_COLLECTION
from app.models.collection import COLLECTION_TYPE_COMPOSITE, COLLECTION_TYPE_RASTER, COLLECTION_TYPE_VECTOR
from app.schemas.collection import (
    CollectionCreate,
    CollectionPatch,
    CollectionRead,
    CollectionReplace,
    CollectionsList,
    CompositeMemberStatus,
    ExtentRecomputeResponse,
    clamp_bbox,
)
from app.services.composite_collections import (
    composite_has_static_tiles,
    composite_resolved_static_revision,
    is_composite_collection,
    mark_composite_static_stale,
    member_tile_status,
    parse_composite_members,
    validate_composite_members,
)
from app.services.collection_tiles_revision import compute_collection_tiles_revision
from app.services.static_tiles_path import read_mbtiles_zoom_range, resolve_mbtiles_path
from app.services.composite_tiles_cache import invalidate_composite_tiles_cache
from app.services.collection_property_indexes import normalize_property_index_fields
from app.schemas.ogc import Link
from app.schemas.resource_share import ShareAdd, ShareRead

router = APIRouter()

# Reserved path segment: GET /collections/edit is the edit-collections (create form) page.
COLLECTION_ID_RESERVED = "edit"


def _collection_read_with_extras(
    collection,
    *,
    base: str,
    collection_id: str,
    member_status: list | None = None,
    default_style_id: str | None = None,
) -> CollectionRead:
    out = CollectionRead.model_validate(collection)
    extra: dict = {}
    if member_status is not None:
        extra["member_status"] = [CompositeMemberStatus.model_validate(m) for m in member_status]
    links = _collection_links(base, collection_id, default_style_id) + [
        Link(href=f"{base}/collections/{collection_id}?f=html", rel="alternate", type="text/html")
    ]
    extra["links"] = links
    return out.model_copy(update=extra)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _versioned_static_tile_url(base: str, collection_id: str, revision: str | None) -> str:
    path = f"{base}/collections/{collection_id}/tiles/static/{{z}}/{{x}}/{{y}}.pbf"
    if revision:
        return f"{path}?{urlencode({'v': revision})}"
    return path


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
    current_user=Depends(get_current_user_optional),
    q: str | None = Query(None, description="Full-text search in id, title, description."),
    bbox: str | None = Query(None, description="Bounding box minx,miny,maxx,maxy (WGS84). Collections whose extent intersects this bbox."),
    sortby: str | None = Query(None, description="Sort by: id, title, description, created_at."),
    sortdesc: bool = Query(False, description="Sort descending."),
    limit: int | None = Query(None, ge=1, le=1000, description="Max collections per page."),
    offset: int = Query(0, ge=0, description="Number of collections to skip."),
    has_static_tiles: bool = Query(False, description="If true, only list collections that have static tiles built (for map layer picker)."),
    collection_type: str | None = Query(None, description="Filter by collection type: vector, raster, or composite."),
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
        current_user=current_user,
        collection_type=collection_type,
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

        owner_ids = [c.owner_id for c in items_list if getattr(c, "owner_id", None) is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if owner_ids else {}
        can_edit_list = []
        for c in items_list:
            can_edit_list.append(await can_edit_collection(db, c, current_user))
        return html_response(
            "collections.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            collections=[
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "collection_type": getattr(c, "collection_type", "vector"),
                    "feature_count": c.feature_count,
                    "owner_username": owner_names.get(c.owner_id) if getattr(c, "owner_id", None) else None,
                    "can_edit": can_edit_list[i],
                }
                for i, c in enumerate(items_list)
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
async def edit_collections_form(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    """Serve the edit-collections page (create collection form). Requires login."""
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required to create a collection")
    base = _base_url(request)
    return html_response("collections_edit.html", base=base, username=current_user.username, is_admin=current_user.is_admin)


@router.get(
    "/{collection_id}/edit",
    summary="Edit collection (HTML form)",
    description="HTML page to edit collection metadata and bbox (map with polygon editor). Use ?f=html.",
)
async def get_collection_edit_form(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    """Serve the collection edit page: map with GeoEditor for bbox, attributes (title, description, bbox inputs)."""
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")
    base = _base_url(request)
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HTML only")
    if is_composite_collection(collection):
        return RedirectResponse(
            url=f"{base}/composites/{collection_id}/edit?f=html",
            status_code=status.HTTP_302_FOUND,
        )
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
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_COLLECTION, collection_id)
    is_raster = getattr(collection, "collection_type", "vector") == "raster"
    raster_ctx = await get_raster_style_edit_context(db, collection_id) if is_raster else None
    default_raster_style = None
    if is_raster:
        dr = await raster_styles_crud.get_default_raster_style(db, collection_id)
        if dr:
            default_raster_style = {"id": dr.id, "title": dr.title, "style_spec": dr.style_spec}
    return html_response(
        "collection_edit.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        collection_id=collection_id,
        collection={
            "id": out.id,
            "title": out.title,
            "description": out.description,
            "collection_type": getattr(collection, "collection_type", "vector"),
            "raster_settings": getattr(collection, "raster_settings", None),
            "property_index_fields": normalize_property_index_fields(
                getattr(collection, "property_index_fields", None)
            ),
        },
        extent_geojson=out.extent.model_dump() if out.extent else None,
        has_static_tiles=has_static_tiles,
        static_minzoom=static_minzoom,
        static_maxzoom=static_maxzoom,
        tile_layer_id=mvt_layer_name(collection_id),
        google_maps_api_key=settings.google_maps_api_key or "",
        default_style={"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec} if default_style else None,
        collection_styles_url=f"{base}/collections/{collection_id}/styles",
        show_cancel_tile_build=show_cancel_tile_build,
        visibility=getattr(collection, "visibility", "private"),
        viewer_can_edit=getattr(collection, "viewer_can_edit", False),
        shares=[{"username": u, "role": r} for u, r in shares],
        shares_url=f"{base}/collections/{collection_id}/shares",
        patch_url=f"{base}/collections/{collection_id}",
        resource_label="this collection",
        show_viewer_edit=True,
        raster_tile_assets=raster_ctx["tile_assets"] if raster_ctx else [],
        raster_default_tile_asset=raster_ctx["default_tile_asset"] if raster_ctx else None,
        raster_mosaic_version_id=raster_ctx["mosaic_version_id"] if raster_ctx else "",
        raster_band_counts=raster_ctx["band_counts"] if raster_ctx else {},
        titiler_configured=raster_ctx["titiler_configured"] if raster_ctx else False,
        public_raster_styles_url=f"{base}/raster-styles",
        collection_raster_styles_url=f"{base}/collections/{collection_id}/raster-styles",
        default_raster_style=default_raster_style,
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
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    base = _base_url(request)
    default_style = await styles_crud.get_default_style(db, collection_id)
    default_style_id = default_style.id if default_style else None
    out = CollectionRead.model_validate(collection)
    is_composite = is_composite_collection(collection)
    if wants_html(request):
        member_status_list: list | None = None
        tiles_revision: str | None = None
        static_tile_url: str | None = None
        has_own_static = False
        if is_composite:
            members = parse_composite_members(getattr(collection, "composite_members", None))
            member_status_list = await member_tile_status(db, members)
            own_rec = await tiles_crud.get_collection_tiles(db, collection_id)
            own_path = resolve_mbtiles_path(collection_id, own_rec.pmtiles_path if own_rec else None)
            has_own_static = own_path is not None
            has_static_tiles = has_own_static or await composite_has_static_tiles(
                db, collection_id, members
            )
            if has_own_static and own_path:
                if own_rec and own_rec.minzoom is not None and own_rec.maxzoom is not None:
                    static_minzoom = own_rec.minzoom
                    static_maxzoom = own_rec.maxzoom
                else:
                    static_minzoom, static_maxzoom = read_mbtiles_zoom_range(own_path)
                tiles_revision = (
                    (own_rec.tiles_revision if own_rec else None)
                    or compute_collection_tiles_revision(collection_id, str(own_path))
                )
            else:
                tiles_revision = await composite_resolved_static_revision(db, collection_id, members)
                static_minzoom = min(
                    (s["minzoom"] for s in member_status_list if s.get("minzoom") is not None),
                    default=0,
                )
                static_maxzoom = max(
                    (s["maxzoom"] for s in member_status_list if s.get("maxzoom") is not None),
                    default=14,
                )
        else:
            rec = await tiles_crud.get_collection_tiles(db, collection_id)
            resolved = resolve_mbtiles_path(collection_id, rec.pmtiles_path if rec else None)
            has_static_tiles = resolved is not None
            if resolved and rec and rec.minzoom is not None and rec.maxzoom is not None:
                static_minzoom = rec.minzoom
                static_maxzoom = rec.maxzoom
            elif resolved:
                static_minzoom, static_maxzoom = read_mbtiles_zoom_range(resolved)
            else:
                static_minzoom = 0
                static_maxzoom = 14
            if resolved:
                tiles_revision = (
                    (rec.tiles_revision if rec else None)
                    or compute_collection_tiles_revision(collection_id, str(resolved))
                )
        if has_static_tiles:
            static_tile_url = _versioned_static_tile_url(base, collection_id, tiles_revision)
        settings = get_settings()
        owner_username = None
        if getattr(collection, "owner_id", None):
            owner_username = (await user_crud.get_usernames_by_ids(db, [collection.owner_id])).get(collection.owner_id)
        can_edit = await can_edit_collection(db, collection, current_user)
        default_raster_style = None
        raster_viz = None
        if getattr(collection, "collection_type", "vector") == COLLECTION_TYPE_RASTER:
            dr = await raster_styles_crud.get_default_raster_style(db, collection_id)
            if dr:
                default_raster_style = {"id": dr.id, "title": dr.title}
                raster_viz = raster_style_viz_context(dr.style_spec)
        return html_response(
            "collection.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            collection={
                "id": out.id,
                "title": out.title,
                "description": out.description,
                "collection_type": getattr(collection, "collection_type", "vector"),
                "raster_settings": getattr(collection, "raster_settings", None),
                "created_at": out.created_at,
                "updated_at": out.updated_at,
                "features_last_updated_at": out.features_last_updated_at,
            },
            owner_username=owner_username,
            extent_geojson=out.extent.model_dump() if out.extent else None,
            has_static_tiles=has_static_tiles,
            static_minzoom=static_minzoom,
            static_maxzoom=static_maxzoom,
            tiles_revision=tiles_revision,
            static_tile_url=static_tile_url,
            tile_layer_id=mvt_layer_name(collection_id),
            google_maps_api_key=settings.google_maps_api_key or "",
            default_style={"id": default_style.id, "title": default_style.title, "style_spec": default_style.style_spec} if default_style else None,
            collection_styles_url=f"{base}/collections/{collection_id}/styles",
            can_edit_collection=can_edit,
            stac_source=getattr(collection, "stac_source", None),
            default_raster_style=default_raster_style,
            raster_viz=raster_viz,
            is_composite=is_composite,
            has_own_static_tiles=has_own_static if is_composite else False,
            member_status=member_status_list or [],
        )
    member_status_json = None
    if is_composite:
        members = parse_composite_members(getattr(collection, "composite_members", None))
        member_status_json = await member_tile_status(db, members)
    return _collection_read_with_extras(
        collection,
        base=base,
        collection_id=collection_id,
        member_status=member_status_json,
        default_style_id=default_style_id,
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
    current_user=Depends(get_current_user_optional),
) -> CollectionRead:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
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
    current_user=Depends(get_current_user_optional),
) -> CollectionRead:
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, coll, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    if "property_index_fields" in payload.model_fields_set:
        if getattr(coll, "collection_type", COLLECTION_TYPE_VECTOR) not in (
            COLLECTION_TYPE_VECTOR,
            COLLECTION_TYPE_COMPOSITE,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Property indexes are only supported on vector and composite collections",
            )
        try:
            normalize_property_index_fields(payload.property_index_fields)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if "composite_members" in payload.model_fields_set and is_composite_collection(coll):
        members = [
            {"collection_id": m.collection_id}
            for m in (payload.composite_members or [])
        ]
        await validate_composite_members(db, collection_id, members)
    collection, property_index_job_id = await collections_crud.patch_collection(
        db, collection_id, payload
    )
    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    if is_composite_collection(collection):
        invalidate_composite_tiles_cache(collection_id)
        if "composite_members" in payload.model_fields_set:
            await mark_composite_static_stale(db, collection_id)
    base = _base_url(request)
    member_status_json = None
    if is_composite_collection(collection):
        member_status_json = await member_tile_status(
            db, parse_composite_members(collection.composite_members)
        )
    out = _collection_read_with_extras(
        collection,
        base=base,
        collection_id=collection_id,
        member_status=member_status_json,
    )
    if property_index_job_id:
        links = list(out.links or [])
        links.append(
            Link(
                href=f"{base}/jobs/{property_index_job_id}",
                rel="monitor",
                type="application/json",
                title="Property index job status",
            )
        )
        return out.model_copy(
            update={
                "links": links,
                "property_index_job_id": property_index_job_id,
            }
        )
    return out


@router.get(
    "/{collection_id}/shares",
    response_model=list[ShareRead],
    summary="List shares for a collection",
)
async def list_collection_shares(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, coll, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_COLLECTION, collection_id)
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post(
    "/{collection_id}/shares",
    response_model=ShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add share for a collection",
)
async def add_collection_share(
    collection_id: str,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, coll, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    share = await resource_share_crud.add_share(
        db, RESOURCE_TYPE_COLLECTION, collection_id, payload.username, payload.role
    )
    if not share:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return ShareRead(username=share.username, role=share.role)


@router.delete(
    "/{collection_id}/shares/{username:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove share for a collection",
)
async def remove_collection_share(
    collection_id: str,
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, coll, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_COLLECTION, collection_id, username)


@router.post(
    "",
    response_model=CollectionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create collection",
    description=(
        "Create a vector, raster, or composite collection. "
        "For composite (merged vector mosaic), set collection_type to composite and optionally "
        "composite_members as an ordered list of member vector collection ids. "
        "Members can also be set later via PATCH. See POST /composites for a composite-only alias."
    ),
)
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> CollectionRead:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required to create a collection")
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
    if payload.collection_type == COLLECTION_TYPE_COMPOSITE:
        members = [
            {"collection_id": m.collection_id}
            for m in (payload.composite_members or [])
        ]
        if members:
            await validate_composite_members(db, payload.id, members)
    if payload.collection_type == COLLECTION_TYPE_VECTOR and payload.property_index_fields:
        try:
            normalize_property_index_fields(payload.property_index_fields)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    collection = await collections_crud.create_collection(
        db, payload, owner_id=current_user.id, visibility="private"
    )
    member_status_json = None
    if is_composite_collection(collection):
        member_status_json = await member_tile_status(
            db, parse_composite_members(collection.composite_members)
        )
    return CollectionRead.model_validate(collection).model_copy(
        update={"member_status": member_status_json} if member_status_json else {}
    )


@router.post(
    "/{collection_id}/composite/invalidate-tiles-cache",
    status_code=status.HTTP_200_OK,
    summary="Invalidate merged composite tile cache",
)
async def invalidate_composite_tiles(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not is_composite_collection(collection):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a composite collection")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    invalidate_composite_tiles_cache(collection_id)
    return {"message": "Composite tile cache invalidated", "collection_id": collection_id}


@router.get(
    "/{collection_id}/composite/edit",
    summary="Edit composite collection members (HTML)",
)
async def get_composite_collection_edit_form(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not is_composite_collection(collection):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a composite collection")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    base = _base_url(request)
    return RedirectResponse(
        url=f"{base}/composites/{collection_id}/edit?f=html",
        status_code=status.HTTP_302_FOUND,
    )


@router.post(
    "/{collection_id}/extent/recompute",
    response_model=ExtentRecomputeResponse,
    summary="Recompute extent from features",
    description="Compute bounding box from feature geometries, update the collection's stored extent, and return it. Use after bulk import or when extent is stale. Returns extent null if the collection has no features with geometry.",
)
async def recompute_collection_extent(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
) -> ExtentRecomputeResponse:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
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
    current_user=Depends(get_current_user_optional),
) -> Response:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    deleted = await collections_crud.delete_collection(db, collection_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
