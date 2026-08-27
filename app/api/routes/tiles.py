"""OGC API Tiles: static MBTiles build + serve (ZXY), TileJSON with dynamic tiles (DB retrieve + Python MVT)."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
from contextvars import ContextVar
from urllib.parse import urlencode
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.core.permissions import can_edit_collection, can_see_collection
from app.utils.geo import mvt_layer_name
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.dynamic_tile_cache import (
    get_tile as get_cached_tile,
    invalidate_collection_cache,
    set_tile as set_cached_tile,
    get_tile_with_params,
    set_tile_with_params,
    push_tile_job,
    _params_key_from_query,
)
from app.services.tile_build_queue import (
    TileBuildOptions,
    clear_pending,
    create_tile_build_job,
    enqueue_tile_build,
    get_latest_tile_build_job,
    get_pending_job_id,
    update_tile_build_job,
)
from app.services.collection_tiles_revision import compute_collection_tiles_revision
from app.services.shadow_import import active_shadow_exclude_job_ids
from app.services.collection_type_guard import ensure_vector_collection
from app.models.collection import COLLECTION_TYPE_COMPOSITE
from app.services.composite_collections import (
    composite_dynamic_revision,
    composite_has_static_tiles,
    composite_members_max_feature_updated_at,
    composite_resolved_static_revision,
    composite_tiles_revision,
    member_tile_status,
    parse_composite_members,
)
from app.services.composite_tiles_cache import get_composite_tile, set_composite_tile
from app.services.mvt_merge import merge_mvt_tiles, read_tile_from_mbtiles
from app.services.static_tiles_path import (
    read_mbtiles_zoom_range,
    resolve_mbtiles_path,
)

router = APIRouter()
log = logging.getLogger(__name__)

# Bound for the duration of get_tiles_dynamic so nested MVT helpers can gate/cancel.
_tile_request_ctx: ContextVar[Request | None] = ContextVar("tile_request_ctx", default=None)



def _filter_geojson_properties(geojson_bytes: bytes, include: set[str]) -> bytes:
    """Keep only selected property keys (plus id) before MVT encode."""
    try:
        data = json.loads(geojson_bytes.decode("utf-8"))
    except Exception:
        return geojson_bytes
    keep = set(include)
    keep.add("id")
    for f in data.get("features") or []:
        props = f.get("properties")
        if isinstance(props, dict):
            f["properties"] = {k: v for k, v in props.items() if k in keep}
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


async def _serve_composite_static_tile(
    db: AsyncSession,
    composite_id: str,
    members: list[dict[str, str]],
    z: int,
    x: int,
    y: int,
) -> bytes:
    revision = await composite_resolved_static_revision(db, composite_id, members)
    if not revision:
        return b""
    cached = get_composite_tile(composite_id, z, x, y, revision)
    if cached is not None:
        return cached
    own_rec = await tiles_crud.get_collection_tiles(db, composite_id)
    own_path = resolve_mbtiles_path(composite_id, own_rec.pmtiles_path if own_rec else None)
    if own_path:
        raw = await asyncio.to_thread(read_tile_from_mbtiles, own_path, z, x, y)
        payload = raw if raw else b""
        set_composite_tile(composite_id, z, x, y, revision, payload)
        return payload
    raw_tiles: list[bytes] = []
    for m in members:
        cid = m["collection_id"]
        rec = await tiles_crud.get_collection_tiles(db, cid)
        if not rec:
            continue
        path = resolve_mbtiles_path(cid, rec.pmtiles_path)
        if not path:
            continue
        raw = await asyncio.to_thread(read_tile_from_mbtiles, path, z, x, y)
        if raw:
            raw_tiles.append(raw)
    merged = merge_mvt_tiles(raw_tiles, mvt_layer_name(composite_id), z, x, y)
    set_composite_tile(composite_id, z, x, y, revision, merged)
    return merged


async def _dynamic_mvt_bytes_for_member(
    db: AsyncSession,
    member_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Fetch member geometries from DB, encode MVT in Python (no PostGIS ST_AsMVT)."""
    from app.services.db_load_gate import run_dynamic_tile_db
    from app.services.dynamic_tile_geojson import get_geojson_for_tile
    from app.services.mvt_encode import encode_geojson_to_mvt

    async def _fetch() -> bytes:
        return await get_geojson_for_tile(db, member_id, z, x, y)

    async def _busy() -> bytes:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dynamic tile backend busy; retry",
        )

    try:
        geojson_bytes = await run_dynamic_tile_db(
            _tile_request_ctx.get(), _fetch, on_overload=_busy
        )
    except ValueError:
        return b""
    try:
        return await asyncio.to_thread(
            encode_geojson_to_mvt, geojson_bytes, member_id, z, x, y
        )
    except Exception:
        return b""


async def _serve_composite_dynamic_tile(
    db: AsyncSession,
    composite_id: str,
    members: list[dict[str, str]],
    z: int,
    x: int,
    y: int,
) -> bytes:
    revision = await composite_dynamic_revision(db, members)
    cache_revision = f"dyn:{revision}"
    cached = get_composite_tile(composite_id, z, x, y, cache_revision)
    if cached is not None:
        return cached
    raw_tiles: list[bytes] = []
    for m in members:
        cid = m["collection_id"]
        try:
            tile = await _dynamic_mvt_bytes_for_member(db, cid, z, x, y)
        except Exception:
            tile = b""
        if tile:
            raw_tiles.append(tile)
    merged = merge_mvt_tiles(raw_tiles, mvt_layer_name(composite_id), z, x, y)
    if not merged:
        merged = await _serve_composite_static_tile(db, composite_id, members, z, x, y)
    set_composite_tile(composite_id, z, x, y, cache_revision, merged)
    return merged


class TileBuildRequestBody(BaseModel):
    """Optional tile build options. If omitted, server defaults are used (min/max zoom from config, all attributes, drop-densest, drop-smallest, -ps and -r1 on)."""
    min_zoom: int | None = Field(None, ge=0, le=24, description="Minimum zoom level (default from config tippecanoe_minzoom).")
    max_zoom: int | None = Field(None, ge=0, le=24, description="Maximum zoom level (default from config tippecanoe_maxzoom).")
    include_attributes: list[str] | None = Field(None, description="Only include these feature attributes in tiles. If omitted, all attributes are included.")
    exclude_attributes: list[str] | None = Field(None, description="Exclude these feature attributes from tiles.")
    densest: Literal["drop", "coalesce"] | None = Field(None, description="When tile is too large: 'drop' = drop-densest-as-needed (default), 'coalesce' = coalesce-densest-as-needed.")
    smallest: Literal["drop", "coalesce"] | None = Field(None, description="When tile is too large: 'drop' = drop-smallest-as-needed (default), 'coalesce' = coalesce-smallest-as-needed.")
    no_line_simplification: bool | None = Field(None, description="If true, use -ps (no line/polygon simplification). Default false (simplification on).")
    simplify_only_low_zooms: bool | None = Field(None, description="If true, use -pS (simplify only at low zooms, not at maxzoom).")
    no_shared_node_simplification: bool | None = Field(None, description="If true, use -pn (do not simplify away shared nodes, e.g. road intersections).")
    no_tiny_polygon_reduction: bool | None = Field(None, description="If true, use -pt (do not combine tiny polygons into squares).")
    no_point_dropping: bool | None = Field(None, description="If true, use -r1 (do not drop fraction of points at low zooms; for clustering). Default false (point dropping on).")
    force: bool | None = Field(
        False,
        description="If true, queue a build even when features have not changed since the last tile build (e.g. new tippecanoe options).",
    )


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# When ?v= matches tiles_revision (content-addressable), same hard caching as versioned raster/mosaic tiles.
_STATIC_VECTOR_TILE_CACHE_VERSIONED = "public, max-age=31536000, s-maxage=31536000, immutable"


def _static_tile_cache_headers(*, etag: str | None = None, versioned: bool = False) -> dict[str, str]:
    """Browser + CDN cache for static (MBTiles) vector PBF; immutable when URL is revision-pinned (?v=)."""
    if versioned:
        headers = {
            "Cache-Control": _STATIC_VECTOR_TILE_CACHE_VERSIONED,
            "CDN-Cache-Control": _STATIC_VECTOR_TILE_CACHE_VERSIONED,
            "Surrogate-Control": _STATIC_VECTOR_TILE_CACHE_VERSIONED,
        }
    else:
        headers = {"Cache-Control": "public, max-age=3600"}
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def _tile_url_with_revision(base_url: str, collection_id: str, revision: str | None) -> str:
    path = f"{base_url}/collections/{collection_id}/tiles/static/{{z}}/{{x}}/{{y}}.pbf"
    if not revision:
        return path
    return f"{path}?{urlencode({'v': revision})}"


def _dynamic_tile_url_with_revision(base_url: str, collection_id: str, revision: str | None) -> str:
    path = f"{base_url}/collections/{collection_id}/tiles/dynamic/{{z}}/{{x}}/{{y}}.pbf"
    if not revision:
        return path
    return f"{path}?{urlencode({'v': revision})}"


def _dynamic_tile_cache_headers_for_zoom(z: int) -> dict[str, str]:
    """
    Browser cache for dynamic tiles: small TTL to reduce request floods while panning/zooming.
    Server-side Redis cache is invalidated on feature writes; clients may also bust cache via ?_gt= on sources.
    """
    _ = z  # kept for API stability; all zooms use the same short browser cache
    cc = "public, max-age=600"
    return {"Cache-Control": cc, "CDN-Cache-Control": cc, "Surrogate-Control": cc}


@router.get(
    "/{collection_id}/tiles/build",
    summary="Tile build options page",
    description="HTML page to configure and start a static tile build. Use ?f=html. From here you set zoom levels, attributes, and simplification strategy before queuing the build.",
)
async def get_tile_build_page(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html for the tile build options page")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    settings = get_settings()
    base = _base_url(request)
    return html_response(
        "tile_build.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        collection_id=collection_id,
        collection_title=collection.title or collection_id,
        default_min_zoom=settings.tippecanoe_minzoom,
        default_max_zoom=settings.tippecanoe_maxzoom,
    )


@router.post(
    "/{collection_id}/tiles/build",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request static MBTiles build",
    description="Enqueue build of MBTiles. Optional body: min_zoom, max_zoom, include/exclude_attributes, densest, smallest, no_line_simplification, simplify_only_low_zooms, no_shared_node_simplification, no_tiny_polygon_reduction, no_point_dropping. Returns job_id to poll status.",
)
async def build_tiles(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    body: TileBuildRequestBody | None = None,
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tile build requires Redis (BULK_QUEUE_TYPE=redis)",
        )
    base = _base_url(request)
    # Dedup: if a build is already queued or building, return that job_id (unless stuck)
    pending_job_id = get_pending_job_id(collection_id)
    if pending_job_id:
        from datetime import datetime, timezone
        job = get_latest_tile_build_job(collection_id)
        if job and job.status in ("pending", "running") and job.updated_at:
            age_seconds = (datetime.now(timezone.utc) - job.updated_at).total_seconds()
            if age_seconds <= 30 * 60:
                update_tile_build_job(pending_job_id, message="Tile build")
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "message": "Tile build already queued or in progress.",
                        "collection_id": collection_id,
                        "job_id": pending_job_id,
                        "status_url": f"{base}/jobs/{pending_job_id}",
                    },
                )
        clear_pending(collection_id)
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    is_composite = getattr(collection, "collection_type", "") == COLLECTION_TYPE_COMPOSITE
    if is_composite:
        members = parse_composite_members(getattr(collection, "composite_members", None))
        if not members:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Composite has no members; add members before building tiles.",
            )
        max_updated = await composite_members_max_feature_updated_at(db, members)
    else:
        max_updated = await tiles_crud.get_max_feature_updated_at(db, collection_id)
    need_build = False
    if max_updated is None and rec and rec.built_at:
        need_build = True  # clear previous build
    elif max_updated is not None:
        if rec is None or rec.features_updated_at is None or max_updated > rec.features_updated_at:
            need_build = True
    if body is not None and body.force:
        need_build = True

    artifact_path = resolve_mbtiles_path(collection_id, rec.pmtiles_path if rec else None)
    if not need_build and artifact_path is None:
        need_build = True

    if not need_build:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "message": "No build needed; tiles are up to date.",
                "collection_id": collection_id,
                "tiles_path": str(artifact_path) if artifact_path else None,
            },
        )
    # Build options from request body (None = use defaults in worker)
    options = None
    if body is not None:
        options = TileBuildOptions(
            min_zoom=body.min_zoom,
            max_zoom=body.max_zoom,
            include_attributes=body.include_attributes if body.include_attributes else None,
            exclude_attributes=body.exclude_attributes if body.exclude_attributes else None,
            densest=body.densest,
            smallest=body.smallest,
            no_line_simplification=body.no_line_simplification,
            simplify_only_low_zooms=body.simplify_only_low_zooms,
            no_shared_node_simplification=body.no_shared_node_simplification,
            no_tiny_polygon_reduction=body.no_tiny_polygon_reduction,
            no_point_dropping=body.no_point_dropping,
        )
    job = create_tile_build_job(collection_id, owner_id=current_user.id if current_user else None)
    start_msg = (
        f"Queued composite tile build ({len(members)} members)"
        if is_composite
        else "Queued tile build"
    )
    update_tile_build_job(job.job_id, message=start_msg)
    enqueued = enqueue_tile_build(
        collection_id,
        job.job_id,
        options=options,
        is_composite=is_composite,
    )
    if not enqueued:
        # Race: another request set pending; return that job
        existing = get_pending_job_id(collection_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Tile build queued.",
                "collection_id": collection_id,
                "job_id": existing or job.job_id,
                "status_url": f"{base}/jobs/{existing or job.job_id}",
            },
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "Tile build queued.",
            "collection_id": collection_id,
            "job_id": job.job_id,
            "status_url": f"{base}/jobs/{job.job_id}",
        },
    )


@router.post(
    "/{collection_id}/tiles/build/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel / reset tile build",
    description="Clears the current build state so you can trigger a new build. Use when a build is stuck (queued or building).",
)
async def cancel_tile_build(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tile build requires Redis (BULK_QUEUE_TYPE=redis)",
        )
    job = get_latest_tile_build_job(collection_id)
    clear_pending(collection_id)
    if job and job.status in ("pending", "running"):
        update_tile_build_job(job.job_id, status="cancelled", message="Cancelled by user")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": "Tile build cancelled. You can request a new build with POST /collections/{collection_id}/tiles/build.",
            "collection_id": collection_id,
        },
    )


@router.delete(
    "/{collection_id}/tiles/static",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete static tiles",
    description="Remove the built MBTiles file from disk (if it exists) and clear the static tiles record. TileJSON will then only show dynamic tiles until you run POST .../tiles/build again.",
)
async def delete_tiles_static(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if rec and rec.pmtiles_path:
        path = Path(rec.pmtiles_path)
        if path.is_file():
            try:
                os.unlink(path)
            except OSError:
                pass
    await tiles_crud.clear_static_tiles(db, collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{collection_id}/tiles/cache/invalidate",
    status_code=status.HTTP_200_OK,
    summary="Invalidate tile cache",
    description="Clear Redis cache for this collection (dynamic tile and search-result cache). Use after rebuilding static tiles to avoid serving stale cached tiles. No-op if Redis is not used or cache is empty.",
)
async def invalidate_tiles_cache(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    invalidate_collection_cache(collection_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": "Tile cache invalidated for this collection.",
            "collection_id": collection_id,
        },
    )


@router.get(
    "/{collection_id}/tiles",
    summary="TileJSON for this collection",
    description="Returns TileJSON with links to static MBTiles (if built) and dynamic vector tiles from the database.",
)
async def get_tiles_tilejson(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_vector_collection(collection)
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    base = _base_url(request)
    settings = get_settings()
    is_composite = getattr(collection, "collection_type", "") == COLLECTION_TYPE_COMPOSITE
    if is_composite:
        members = parse_composite_members(getattr(collection, "composite_members", None))
        statuses = await member_tile_status(db, members)
        has_static = await composite_has_static_tiles(db, collection_id, members)
        tiles_revision = await composite_resolved_static_revision(db, collection_id, members)
        own_rec = await tiles_crud.get_collection_tiles(db, collection_id)
        own_path = resolve_mbtiles_path(collection_id, own_rec.pmtiles_path if own_rec else None)
        if own_path:
            if own_rec and own_rec.minzoom is not None and own_rec.maxzoom is not None:
                minzoom = own_rec.minzoom
                maxzoom = own_rec.maxzoom
            else:
                minzoom, maxzoom = read_mbtiles_zoom_range(own_path)
        else:
            minzoom = min((s["minzoom"] for s in statuses if s.get("minzoom") is not None), default=0)
            maxzoom = max((s["maxzoom"] for s in statuses if s.get("maxzoom") is not None), default=14)
        dyn_revision = await composite_dynamic_revision(db, members)
        tile_urls = [_dynamic_tile_url_with_revision(base, collection_id, dyn_revision)]
        if has_static:
            tile_urls.insert(0, _tile_url_with_revision(base, collection_id, tiles_revision))
        layer_id = mvt_layer_name(collection_id)
    else:
        rec = await tiles_crud.get_collection_tiles(db, collection_id)
        resolved = resolve_mbtiles_path(collection_id, rec.pmtiles_path if rec else None)
        has_static = resolved is not None
        tiles_revision = (
            (rec.tiles_revision if rec else None)
            or compute_collection_tiles_revision(collection_id, str(resolved) if resolved else None)
        )
        if resolved and rec and rec.minzoom is not None and rec.maxzoom is not None:
            minzoom = rec.minzoom
            maxzoom = rec.maxzoom
        elif resolved:
            minzoom, maxzoom = read_mbtiles_zoom_range(resolved)
        else:
            minzoom = 0
            maxzoom = 14
        tile_urls = [f"{base}/collections/{collection_id}/tiles/dynamic/{{z}}/{{x}}/{{y}}.pbf"]
        if has_static:
            tile_urls.insert(0, _tile_url_with_revision(base, collection_id, tiles_revision))
        layer_id = mvt_layer_name(collection_id)
    tilejson = {
        "tilejson": "2.2.0",
        "name": collection_id,
        "description": collection.description or "",
        "version": "1.0.0",
        "scheme": "xyz",
        "tiles": tile_urls,
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "tiles_revision": tiles_revision,
        "vector_layers": [{"id": layer_id, "description": "", "minzoom": minzoom, "maxzoom": maxzoom}],
    }
    return JSONResponse(content=tilejson)



def _safe_json_key(s: str) -> str:
    """Allow only alphanumeric and underscore for JSON key (SQL injection safety)."""
    return "".join(c for c in s if c.isalnum() or c == "_")[:200]


async def _serve_composite_filtered_dynamic_tile(
    db: AsyncSession,
    collection_id: str,
    composite_members: list[dict[str, str]],
    z: int,
    x: int,
    y: int,
    *,
    limit: int | None,
    offset: int,
    sortby: str | None,
    sortdesc: bool,
    bbox: str | None,
    datetime_param: str | None,
    filter_param: list[str] | None,
    q: str | None,
    ids: str | None,
    properties: str | None,
    params_key: str | None,
    cache_headers: dict[str, str],
    cache_hit_headers: dict[str, str],
) -> Response:
    """Filtered composite dynamic tile via search-result GeoJSON + in-process MVT encode."""
    from app.services.dynamic_tile_geojson import get_geojson_for_tile
    from app.services.mvt_encode import encode_geojson_to_mvt

    settings = get_settings()
    if params_key and settings.tiles_dynamic_cache_with_params:
        cached = get_tile_with_params(collection_id, z, x, y, params_key)
        if cached is not None:
            return Response(
                content=cached,
                media_type="application/x-protobuf",
                headers=cache_hit_headers,
            )
    ids_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
    bbox_tuple = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                pass
    geojson_bytes = await get_geojson_for_tile(
        db,
        collection_id,
        z,
        x,
        y,
        limit=limit,
        offset=offset,
        sortby=sortby,
        sortdesc=sortdesc,
        bbox_user=bbox_tuple,
        datetime_param=datetime_param,
        filter_param=filter_param,
        q=q,
        ids=ids_list,
    )
    try:
        payload = await asyncio.to_thread(
            encode_geojson_to_mvt, geojson_bytes, collection_id, z, x, y
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Composite tile build failed: {e!s}",
        ) from e
    if params_key is not None:
        set_tile_with_params(collection_id, z, x, y, params_key, payload)
    return Response(
        content=payload,
        media_type="application/x-protobuf",
        headers=cache_headers,
    )



@router.get(
    "/{collection_id}/tiles/dynamic/{z:int}/{x:int}/{y:int}.pbf",
    summary="Dynamic vector tile (PostGIS MVT or tippecanoe worker)",
    description="Returns Mapbox Vector Tile. Same query params as GET items. DB only retrieves geometries; MVT is encoded in Python (or LIFO queue workers when TILES_DYNAMIC_USE_QUEUE is on). Optional TILES_DYNAMIC_WORKER_URL HTTP worker.",
)
async def get_tiles_dynamic(
    request: Request,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
    limit: int | None = Query(None, ge=1, le=10000, description="Max features (same as GET items)."),
    offset: int = Query(0, ge=0),
    sortby: str | None = Query(None),
    sortdesc: bool = Query(False),
    bbox: str | None = Query(None),
    datetime_param: str | None = Query(None, alias="datetime"),
    filter_param: list[str] | None = Query(None, alias="filter"),
    q: str | None = Query(None),
    ids: str | None = Query(None, description="Comma-separated feature ids (e.g. single item view)."),
    properties: str | None = Query(None),
):
    token = _tile_request_ctx.set(request)
    try:
        return await _get_tiles_dynamic_impl(
            request,
            collection_id,
            z,
            x,
            y,
            db,
            current_user,
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox=bbox,
            datetime_param=datetime_param,
            filter_param=filter_param,
            q=q,
            ids=ids,
            properties=properties,
        )
    finally:
        _tile_request_ctx.reset(token)


async def _get_tiles_dynamic_impl(
    request: Request,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession,
    current_user,
    *,
    limit: int | None,
    offset: int,
    sortby: str | None,
    sortdesc: bool,
    bbox: str | None,
    datetime_param: str | None,
    filter_param: list[str] | None,
    q: str | None,
    ids: str | None,
    properties: str | None,
):
    if z < 0 or z > 22:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid z")
    if x < 0 or x >= (1 << z) or y < 0 or y >= (1 << z):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid x or y for zoom")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    settings = get_settings()
    cache_headers = _dynamic_tile_cache_headers_for_zoom(z)
    cache_hit_headers = {**cache_headers, "X-From-Cache": "true"}

    is_composite = getattr(collection, "collection_type", "") == COLLECTION_TYPE_COMPOSITE
    composite_members: list[dict[str, str]] | None = None
    if is_composite:
        composite_members = parse_composite_members(getattr(collection, "composite_members", None))

    feature_ids: list[str] | None = None
    if ids:
        feature_ids = [i.strip() for i in ids.split(",") if i.strip()]
    props_include: list[str] | None = None
    if properties:
        props_include = [p.strip() for p in properties.split(",") if p.strip()]
    # Preserve original query params for cache keys so items list warm + tile URLs with ?q=
    # share the same Redis entry (exact-token rewrite below must not change the key).
    orig_q = q
    orig_ids = ids

    # Full-text search (q) requires at least 4 characters; ignore short q to avoid slow queries.
    if q and q.strip() and len(q.strip()) < 4:
        q = None
        orig_q = None

    # Exact tokens (CAR codes, etc.): resolve once to ids for SQL/MVT fallback paths.
    # Search-cache path still keys on original ?q= (showcase contract for other apps).
    if q and q.strip() and not feature_ids:
        from app.crud import features as features_crud
        from app.services.collection_property_indexes import normalize_property_index_fields

        if features_crud.is_exact_search_token(q):
            resolved = await features_crud.resolve_exact_search_feature_ids(
                db,
                collection_id,
                q.strip(),
                property_keys=normalize_property_index_fields(
                    getattr(collection, "property_index_fields", None)
                ),
                limit=min(limit or settings.items_default_limit, 50),
                exclude_bulk_job_ids=active_shadow_exclude_job_ids(collection_id) or None,
            )
            feature_ids = resolved
            q = None
            ids = ",".join(feature_ids) if feature_ids else ""

    has_query_params = (
        limit is not None
        or offset != 0
        or (sortby is not None and sortby.strip())
        or sortdesc
        or (bbox is not None and bbox.strip())
        or (datetime_param is not None and datetime_param.strip())
        or bool(filter_param)
        or (orig_q is not None and orig_q.strip())
        or bool(feature_ids)
        or (orig_ids is not None and orig_ids.strip())
        or bool(props_include)
    )

    if is_composite and composite_members is not None:
        if not composite_members:
            return Response(content=b"", media_type="application/x-protobuf", headers=cache_headers)
        if not has_query_params:
            tile_bytes = await _serve_composite_dynamic_tile(db, collection_id, composite_members, z, x, y)
            return Response(content=tile_bytes, media_type="application/x-protobuf", headers=cache_headers)
        # Filtered / single-item composite tiles (e.g. ?ids=member:feature-uuid).
        # Never fall through to the vector search-cache path — composite ids are not native feature ids.
        params_key = _params_key_from_query(
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox=bbox,
            datetime_param=datetime_param,
            filter_param=filter_param,
            q=orig_q,
            ids=orig_ids,
            properties=properties,
        )
        return await _serve_composite_filtered_dynamic_tile(
            db,
            collection_id,
            composite_members,
            z,
            x,
            y,
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox=bbox,
            datetime_param=datetime_param,
            filter_param=filter_param,
            q=orig_q,
            ids=orig_ids or ids,
            properties=properties,
            params_key=params_key,
            cache_headers=cache_headers,
            cache_hit_headers=cache_hit_headers,
        )

    if not has_query_params:
        cached = get_cached_tile(collection_id, z, x, y)
        if cached is not None:
            return Response(
                content=cached,
                media_type="application/x-protobuf",
                headers=cache_hit_headers,
            )

    # Compute params_key from the *client* query (orig_q / orig_ids) so the same URL
    # used by GET items warms the cache other apps hit via /tiles/dynamic?...&q=...
    params_key: str | None = None
    if has_query_params:
        params_key = _params_key_from_query(
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox=bbox,
            datetime_param=datetime_param,
            filter_param=filter_param,
            q=orig_q,
            ids=orig_ids,
            properties=properties,
        )
        if settings.tiles_dynamic_cache_with_params:
            cached = get_tile_with_params(collection_id, z, x, y, params_key)
            if cached is not None:
                return Response(
                    content=cached,
                    media_type="application/x-protobuf",
                    headers=cache_hit_headers,
                )

    # Query-once path: cache matching GeoJSON once, encode each tile in-process (or via
    # tile workers when tiles_dynamic_use_queue is on). Used for items-table sync AND for
    # property filters (car_code:eq) so we do not run heavy per-tile ST_Union under the
    # concurrency gate (which returned empty tiles that Redis then cached as holes).
    use_queue = getattr(settings, "tiles_dynamic_use_queue", False)
    search_cache_ttl = getattr(settings, "tiles_search_result_cache_ttl_seconds", 300)
    list_sync_mode = (
        limit is not None
        or offset != 0
        or (sortby is not None and str(sortby).strip())
        or (orig_q is not None and str(orig_q).strip())
        or (orig_ids is not None and str(orig_ids).strip())
    )
    filter_only_mode = bool(filter_param) and not list_sync_mode and not bool(feature_ids)
    use_search_cache = (
        has_query_params
        and params_key is not None
        and search_cache_ttl > 0
        and (list_sync_mode or filter_only_mode)
    )
    if use_search_cache:
        from app.services.search_result_cache import ensure_search_result_cached
        from app.services.dynamic_tile_cache import get_search_result
        from app.services.dynamic_tile_geojson import filter_geojson_to_tile_bbox
        from app.services.db_load_gate import run_dynamic_tile_db
        from app.services.mvt_encode import encode_geojson_to_mvt

        # Farm / property filters: pull all matching features (not the items default page of 100).
        if filter_only_mode:
            search_limit = int(
                getattr(settings, "tiles_filter_max_features", 0)
                or max(int(settings.tiles_mvt_max_features), int(settings.items_max_limit), 10000)
            )
        else:
            search_limit = limit or settings.items_default_limit

        async def _cache_search() -> bool:
            # Fill with original client params (exact q resolved inside get_search_result_geojson).
            return await ensure_search_result_cached(
                db,
                collection_id,
                params_key,
                limit=search_limit,
                offset=offset,
                sortby=sortby,
                sortdesc=sortdesc,
                bbox=bbox,
                datetime_param=datetime_param,
                filter_param=filter_param,
                q=orig_q,
                ids=orig_ids,
            )

        async def _cache_miss() -> bool:
            return False

        ok = await run_dynamic_tile_db(request, _cache_search, on_overload=_cache_miss)
        if not ok:
            # Overloaded while filling search cache — ask client to retry (do not cache hole).
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dynamic tile search cache busy; retry",
            )
        # Re-check tile cache (another request may have built it)
        payload = get_tile_with_params(collection_id, z, x, y, params_key)
        if payload is not None:
            return Response(
                content=payload,
                media_type="application/x-protobuf",
                headers=cache_hit_headers,
            )
        # Build requested tile inline so we don't wait for queue (major latency win)
        geojson_bytes = get_search_result(collection_id, params_key)
        if geojson_bytes:
            filtered = filter_geojson_to_tile_bbox(geojson_bytes, z, x, y)
            # Skip tippecanoe for empty tiles (faster)
            try:
                fc = json.loads(filtered.decode("utf-8"))
                if not fc.get("features"):
                    payload = b""
                    set_tile_with_params(collection_id, z, x, y, params_key, payload)
                    return Response(
                        content=payload,
                        media_type="application/x-protobuf",
                        headers=cache_headers,
                    )
            except Exception:
                pass
            try:
                # Offload CPU encode so the event loop can serve other tile requests.
                payload = await asyncio.to_thread(
                    encode_geojson_to_mvt, filtered, collection_id, z, x, y
                )
                set_tile_with_params(collection_id, z, x, y, params_key, payload)
                # Precompute adjacent zooms in background when queue workers are enabled
                if use_queue:
                    if z > 0:
                        push_tile_job(collection_id, params_key, z - 1, x // 2, y // 2)
                    if z < 22:
                        push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2)
                        push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2)
                        push_tile_job(collection_id, params_key, z + 1, x * 2, y * 2 + 1)
                        push_tile_job(collection_id, params_key, z + 1, x * 2 + 1, y * 2 + 1)
                return Response(
                    content=payload,
                    media_type="application/x-protobuf",
                    headers=cache_headers,
                )
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Tile build failed: {e!s}",
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search result cache missing after ensure",
        )

    # HTTP worker (legacy): same params as GET items + ids
    worker_url = (settings.tiles_dynamic_worker_url or "").rstrip("/")
    if worker_url:
        # Build query string from request params so limit/offset and all filters are forwarded
        from urllib.parse import urlencode
        qs = urlencode(list(request.query_params.multi_items()))
        worker_tile_url = f"{worker_url}/collections/{collection_id}/tiles/dynamic/{z}/{x}/{y}.pbf"
        if qs:
            worker_tile_url += "?" + qs
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(worker_tile_url)
                resp.raise_for_status()
                payload = resp.content
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Dynamic tile worker error: {e!s}",
            )
        if not has_query_params:
            set_cached_tile(collection_id, z, x, y, payload)
        elif params_key is not None:
            set_tile_with_params(collection_id, z, x, y, params_key, payload)
        return Response(
            content=payload,
            media_type="application/x-protobuf",
            headers=cache_headers,
        )

    # Default path: DB retrieves GeoJSON for the tile bbox only; MVT encode is Python
    # (no PostGIS ST_AsMVT / ST_AsMVTGeom). Same pattern as filtered search-cache tiles.
    from app.services.db_load_gate import run_dynamic_tile_db
    from app.services.dynamic_tile_geojson import get_geojson_for_tile
    from app.services.mvt_encode import encode_geojson_to_mvt

    bbox_tuple: tuple[float, float, float, float] | None = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                pass

    async def _fetch_geojson() -> bytes:
        timeout_sec = getattr(settings, "tiles_dynamic_statement_timeout_seconds", 0) or 0
        if timeout_sec > 0:
            await db.execute(text(f"SET LOCAL statement_timeout = {int(timeout_sec * 1000)}"))
        return await get_geojson_for_tile(
            db,
            collection_id,
            z,
            x,
            y,
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox_user=bbox_tuple,
            datetime_param=datetime_param,
            filter_param=filter_param or None,
            q=q,
            ids=feature_ids,
        )

    async def _busy() -> bytes:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dynamic tile backend busy; retry",
        )

    try:
        geojson_bytes = await run_dynamic_tile_db(
            request, _fetch_geojson, on_overload=_busy
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except Exception as e:
        await db.rollback()
        msg = str(e).lower()
        if any(
            token in msg
            for token in (
                "geom",
                "geometry",
                "topology",
                "invalid",
                "noding",
                "self-intersection",
                "self intersection",
                "non-noded",
                "cwring",
                "isvalid",
            )
        ):
            log.warning(
                "Dynamic tile GeoJSON fetch failed (empty tile): %s %s/%s/%s err=%s",
                collection_id,
                z,
                x,
                y,
                e,
            )
            geojson_bytes = b'{"type":"FeatureCollection","features":[]}'
        else:
            raise

    if props_include:
        safe_props = {k for k in props_include if _safe_json_key(k) == k}
        if safe_props:
            geojson_bytes = _filter_geojson_properties(geojson_bytes, safe_props)

    try:
        fc = json.loads(geojson_bytes.decode("utf-8"))
        if not fc.get("features"):
            payload = b""
        else:
            payload = await asyncio.to_thread(
                encode_geojson_to_mvt, geojson_bytes, collection_id, z, x, y
            )
    except Exception as e:
        log.warning(
            "Dynamic tile MVT encode failed (empty tile): %s %s/%s/%s err=%s",
            collection_id,
            z,
            x,
            y,
            e,
        )
        payload = b""

    if not has_query_params:
        set_cached_tile(collection_id, z, x, y, payload)
    elif params_key is not None:
        set_tile_with_params(collection_id, z, x, y, params_key, payload)
    return Response(
        content=payload,
        media_type="application/x-protobuf",
        headers=cache_headers,
    )


def _read_tile_from_mbtiles(path: Path, z: int, x: int, y: int) -> bytes | None:
    """Read a single tile from an MBTiles (SQLite) file. Sync, run in thread. Returns decompressed tile bytes or None.
    MBTiles uses TMS row order (y=0 at bottom); we convert XYZ y to TMS row."""
    import sqlite3
    tms_row = (1 << z) - 1 - y
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, tms_row),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    raw = row[0]
    if raw is None:
        return None
    # MBTiles from tippecanoe typically store gzip-compressed tiles
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


@router.get(
    "/{collection_id}/tiles/static/{z:int}/{x:int}/{y:int}.pbf",
    summary="Static vector tile (Z/X/Y from MBTiles)",
    description="Returns the tile at z/x/y from the built MBTiles file for this collection.",
)
async def get_tiles_static_zxy(
    request: Request,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    if z < 0 or z > 22:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid z")
    if x < 0 or x >= (1 << z) or y < 0 or y >= (1 << z):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid x or y for zoom")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if getattr(collection, "collection_type", "") == COLLECTION_TYPE_COMPOSITE:
        members = parse_composite_members(getattr(collection, "composite_members", None))
        if not members:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Composite has no members.")
        own_rec = await tiles_crud.get_collection_tiles(db, collection_id)
        own_path = resolve_mbtiles_path(collection_id, own_rec.pmtiles_path if own_rec else None)
        if own_path:
            tiles_revision = (
                (own_rec.tiles_revision if own_rec else None)
                or compute_collection_tiles_revision(collection_id, str(own_path))
            )
            version_query = request.query_params.get("v")
            pinned_version = bool(tiles_revision and version_query and version_query == tiles_revision)
            cache_headers = _static_tile_cache_headers(etag=tiles_revision, versioned=pinned_version)
            tile_bytes = await asyncio.to_thread(_read_tile_from_mbtiles, own_path, z, x, y)
            if tile_bytes is None:
                tile_bytes = b""
            return Response(
                content=tile_bytes,
                media_type="application/x-protobuf",
                headers=cache_headers,
            )
        statuses = await member_tile_status(db, members)
        if not any(s.get("has_static_tiles") for s in statuses):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No static tiles built yet. POST /tiles/build on this composite or its members.",
            )
        tiles_revision = await composite_resolved_static_revision(db, collection_id, members)
        version_query = request.query_params.get("v")
        pinned_version = bool(tiles_revision and version_query and version_query == tiles_revision)
        cache_headers = _static_tile_cache_headers(etag=tiles_revision, versioned=pinned_version)
        tile_bytes = await _serve_composite_static_tile(db, collection_id, members, z, x, y)
        return Response(
            content=tile_bytes,
            media_type="application/x-protobuf",
            headers=cache_headers,
        )
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    path = resolve_mbtiles_path(collection_id, rec.pmtiles_path if rec else None)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiles not built yet. POST to /tiles/build first.")
    tiles_revision = (
        (rec.tiles_revision if rec else None)
        or compute_collection_tiles_revision(collection_id, str(path))
    )
    version_query = request.query_params.get("v")
    pinned_version = bool(tiles_revision and version_query and version_query == tiles_revision)
    cache_headers = _static_tile_cache_headers(etag=tiles_revision, versioned=pinned_version)
    tile_bytes = await asyncio.to_thread(_read_tile_from_mbtiles, path, z, x, y)
    if tile_bytes is None:
        return Response(
            content=b"",
            media_type="application/x-protobuf",
            headers=cache_headers,
        )
    return Response(
        content=tile_bytes,
        media_type="application/x-protobuf",
        headers=cache_headers,
    )


