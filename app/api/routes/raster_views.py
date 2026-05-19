"""Saved MosaicJSON / Titiler view definitions (metadata + disk JSON)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import hashlib
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7
from urllib.parse import quote

from app.api.deps import get_current_user_optional, get_current_user_required
from app.core.config import get_settings
from app.core.permissions import (
    can_access_raster_view_tiles_anonymous,
    can_edit_raster_view,
    can_see_raster_view,
)
from app.services.titiler_cancel import (
    ClientDisconnected,
    raise_if_disconnected,
    titiler_get_cancel_on_disconnect,
)
from app.services.titiler_inflight import await_tile_singleflight
from app.services.titiler_tile_cache import (
    cache_key_for_titiler_request,
    get_cached_tile,
    set_cached_tile,
)
from app.crud import raster_views as raster_views_crud
from app.crud.raster_views import _MISSING
from app.crud import resource_share as resource_share_crud
from app.db.session import get_db
from app.models.collection import VISIBILITY_PRIVATE
from app.models.resource_share import RESOURCE_TYPE_RASTER_VIEW
from app.models.user import User
from app.schemas.resource_share import ShareAdd, ShareRead
from app.services.mosaic_plan import build_mosaicjson_from_footprints
from app.services.titiler_gate import titiler_upstream_gate_run
from app.services.titiler_http import get_titiler_http_client
from app.services.titiler_point import enrich_point_response, fetch_titiler_point_json
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/raster-views", tags=["raster-views"])

# Mosaics are mutable; allow revalidation (CDN + browser) and avoid immutable long-term pinning.
_MOSAIC_TILE_CACHE_CONTROL = "public, max-age=0, must-revalidate, s-maxage=0"
# When tile URL includes ?v=<tiles_revision> matching the mosaic file fingerprint, safe to cache hard.
_MOSAIC_TILE_CACHE_VERSIONED = "public, max-age=31536000, s-maxage=31536000, immutable"
_MOSAIC_TILE_CDN_REVALIDATE = "public, max-age=0, must-revalidate"


def compute_mosaic_tiles_revision(settings: Any, view_id: str, json_relative_path: str) -> str | None:
    """SHA-256 hex of view_id + mosaic path mtime + size; None if file missing. Matches tile ETag body."""
    path = Path(settings.raster_storage_path) / json_relative_path
    if not path.exists():
        return None
    stat = path.stat()
    etag_base = f"{view_id}:{path}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.sha256(etag_base.encode()).hexdigest()


def _etag_header_value(etag_hex: str) -> str:
    return f'"{etag_hex}"'


def _mosaic_cache_headers(*, etag_hdr: str, versioned: bool) -> dict[str, str]:
    headers = {
        "ETag": etag_hdr,
        "Cache-Control": _MOSAIC_TILE_CACHE_VERSIONED if versioned else _MOSAIC_TILE_CACHE_CONTROL,
        # Explicit edge cache directives reduce REVALIDATED churn on CDNs like Cloudflare.
        "CDN-Cache-Control": _MOSAIC_TILE_CACHE_VERSIONED if versioned else _MOSAIC_TILE_CDN_REVALIDATE,
        "Surrogate-Control": _MOSAIC_TILE_CACHE_VERSIONED if versioned else _MOSAIC_TILE_CDN_REVALIDATE,
        "X-Mosaic-Versioned-Cache": "hit" if versioned else "miss",
    }
    return headers


def _if_none_match_includes_strong_etag(etag_hex: str, if_none_match: str | None) -> bool:
    if not if_none_match:
        return False
    for part in if_none_match.split(","):
        p = part.strip()
        if not p or p == "*":
            continue
        if p.upper().startswith("W/"):
            p = p[2:].lstrip()
        if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            p = p[1:-1]
        if p == etag_hex:
            return True
    return False


class RasterViewCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    visibility: str = Field(default=VISIBILITY_PRIVATE, description="private | public | logged")
    mosaic_json: dict = Field(..., description="Titiler-compatible MosaicJSON object.")
    definition: dict[str, Any] | None = None
    allow_public_maps: bool = False


class RasterViewRead(BaseModel):
    id: str
    title: str
    visibility: str
    json_relative_path: str
    bbox: list[float] | None = None
    definition: dict[str, Any] | None = None
    allow_public_maps: bool = False
    owner_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Append ?v={tiles_revision} to mosaic tile URLs for long-lived browser/CDN cache when unchanged.
    tiles_revision: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_row(
        cls,
        row: Any,
        base: str | None = None,
        *,
        tiles_revision: str | None = None,
    ) -> "RasterViewRead":
        d = {
            "id": row.id,
            "title": row.title,
            "visibility": row.visibility,
            "json_relative_path": row.json_relative_path,
            "bbox": row.bbox,
            "definition": row.definition,
            "allow_public_maps": getattr(row, "allow_public_maps", False),
            "owner_id": row.owner_id,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
            "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
            "tiles_revision": tiles_revision,
        }
        return cls.model_validate(d)


class RasterViewUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=512)
    visibility: str | None = None
    mosaic_json: dict[str, Any] | None = None
    definition: dict[str, Any] | None = None
    allow_public_maps: bool | None = None


def _write_mosaic_file(settings: Any, rel_path: str, mosaic_json: dict) -> None:
    root = Path(settings.raster_storage_path)
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mosaic_json, separators=(",", ":")), encoding="utf-8")


def _mosaic_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Build MosaicJSON from saved definition.selected items (href + footprint GeoJSON)."""
    selected = definition.get("selected") or []
    pairs: list[tuple[str, BaseGeometry]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        href = item.get("href")
        fp = item.get("footprint")
        if not href or not isinstance(fp, dict):
            continue
        try:
            g = shape(fp)
            if g.geom_type == "Polygon":
                pairs.append((str(href), g))
            elif g.geom_type == "MultiPolygon":
                pairs.append((str(href), max(g.geoms, key=lambda p: p.area)))
        except Exception:
            continue
    if not pairs:
        raise ValueError("definition.selected must contain href and footprint for each item")
    return build_mosaicjson_from_footprints(pairs)


@router.get("", summary="List saved raster mosaics")
async def list_raster_views(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
    q: str | None = Query(None),
    bbox: str | None = Query(None, description="minx,miny,maxx,maxy — intersects stored bbox"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    mine: bool = Query(False),
):
    bbox_t: tuple[float, float, float, float] | None = None
    if bbox and bbox.strip():
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_t = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                bbox_t = None
    rows, total = await raster_views_crud.list_views_visible_to_user(
        db,
        current_user=current_user,
        limit=limit,
        offset=offset,
        q=q,
        bbox_intersects=bbox_t,
        mine_only=mine,
    )
    return {
        "items": [RasterViewRead.from_row(r).model_dump() for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", summary="Save a MosaicJSON view to disk and register metadata")
async def create_raster_view(
    body: RasterViewCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if body.visibility not in ("private", "public", "logged"):
        raise HTTPException(status_code=400, detail="visibility must be private, public, or logged")

    settings = get_settings()
    vid = str(uuid7())
    rel = f"views/{vid}.json"

    mosaic_data = body.mosaic_json
    bbox_val: list[float] | None = None
    if body.definition and isinstance(body.definition, dict):
        try:
            mosaic_data = _mosaic_from_definition(body.definition)
        except ValueError:
            mosaic_data = body.mosaic_json
    b = mosaic_data.get("bounds")
    if isinstance(b, list) and len(b) >= 4:
        bbox_val = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]

    _write_mosaic_file(settings, rel, mosaic_data)

    row = await raster_views_crud.create_view(
        db,
        title=body.title,
        json_relative_path=rel,
        owner_id=current_user.id,
        visibility=body.visibility,
        view_id=vid,
        bbox=bbox_val,
        definition=body.definition,
        allow_public_maps=body.allow_public_maps,
    )
    rev = compute_mosaic_tiles_revision(settings, row.id, row.json_relative_path)
    return RasterViewRead.from_row(row, tiles_revision=rev)


@router.patch("/{view_id}", summary="Update mosaic metadata and/or MosaicJSON")
async def patch_raster_view(
    view_id: str,
    body: RasterViewUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")

    settings = get_settings()
    unset = body.model_dump(exclude_unset=True)
    if unset.get("visibility") is not None and unset["visibility"] not in ("private", "public", "logged"):
        raise HTTPException(status_code=400, detail="invalid visibility")

    new_mosaic = unset.get("mosaic_json")
    new_def = unset.get("definition")
    bbox_override: list[float] | None = None

    if new_def is not None and isinstance(new_def, dict):
        try:
            mosaic_data = _mosaic_from_definition(new_def)
            new_mosaic = mosaic_data
            b = mosaic_data.get("bounds")
            if isinstance(b, list) and len(b) >= 4:
                bbox_override = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        except ValueError:
            pass

    if new_mosaic is not None:
        _write_mosaic_file(settings, row.json_relative_path, new_mosaic)
        if bbox_override is None and isinstance(new_mosaic.get("bounds"), list):
            b = new_mosaic["bounds"]
            if len(b) >= 4:
                bbox_override = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]

    title = unset.get("title")
    vis = unset.get("visibility")
    allow_pm = unset.get("allow_public_maps")

    bbox_arg: object = _MISSING
    if bbox_override is not None:
        bbox_arg = bbox_override
    def_arg: object = _MISSING
    if "definition" in unset:
        def_arg = new_def

    updated = await raster_views_crud.update_view(
        db,
        view_id,
        title=title,
        visibility=vis,
        bbox=bbox_arg,
        definition=def_arg,
        allow_public_maps=allow_pm,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="View not found")
    rev = compute_mosaic_tiles_revision(settings, updated.id, updated.json_relative_path)
    return RasterViewRead.from_row(updated, tiles_revision=rev)


@router.get("/{view_id}", summary="Get raster view metadata", response_model=RasterViewRead)
async def get_raster_view(
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    allow_pm = getattr(row, "allow_public_maps", False)
    # Same rules as titiler_mosaic_tile: anonymous may read metadata when tiles are allowed
    # (e.g. private mosaic + allow_public_maps on a public map) so clients can append ?v=tiles_revision.
    if current_user is None:
        if not can_access_raster_view_tiles_anonymous(
            visibility=row.visibility,
            allow_public_maps=allow_pm,
        ):
            raise HTTPException(status_code=404, detail="View not found")
    else:
        if not await can_see_raster_view(
            db, row.owner_id, row.visibility, view_id, current_user
        ):
            raise HTTPException(status_code=404, detail="View not found")
    settings = get_settings()
    rev = compute_mosaic_tiles_revision(settings, row.id, row.json_relative_path)
    payload = RasterViewRead.from_row(row, tiles_revision=rev)
    # Embed clients poll this for tiles_revision (?v=); stale CDN/HTML cache breaks versioned mosaic URLs.
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "private, no-store, must-revalidate"},
    )


@router.get(
    "/{view_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy mosaic tile to Titiler",
)
async def titiler_mosaic_tile(
    request: Request,
    view_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    settings = get_settings()

    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Titiler not configured")

    # -----------------------------
    # View validation
    # -----------------------------
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")

    allow_pm = getattr(row, "allow_public_maps", False)

    if current_user is None:
        if not can_access_raster_view_tiles_anonymous(
            visibility=row.visibility,
            allow_public_maps=allow_pm,
        ):
            raise HTTPException(status_code=404, detail="View not found")
    else:
        if not await can_see_raster_view(
            db, row.owner_id, row.visibility, view_id, current_user
        ):
            raise HTTPException(status_code=404, detail="View not found")

    # -----------------------------
    # Mosaic file validation + ETag / ?v= tiles_revision
    # -----------------------------
    path = Path(settings.raster_storage_path) / row.json_relative_path
    etag = compute_mosaic_tiles_revision(settings, view_id, row.json_relative_path)
    if etag is None:
        raise HTTPException(status_code=404, detail="Mosaic JSON missing on disk")

    etag_hdr = _etag_header_value(etag)
    v_q = request.query_params.get("v")
    use_versioned_cache = v_q is not None and v_q == etag

    # Client cache hit → no upstream call at all
    if _if_none_match_includes_strong_etag(etag, request.headers.get("if-none-match")):
        headers = _mosaic_cache_headers(etag_hdr=etag_hdr, versioned=use_versioned_cache)
        return Response(
            status_code=304,
            headers=headers,
        )

    # -----------------------------
    # Mosaic URL
    # -----------------------------
    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")

    if secret and fetch_base:
        mosaic_url = (
            f"{fetch_base}/internal/raster-views/{view_id}/mosaic.json"
            f"?token={quote(secret, safe='')}"
        )
    else:
        mosaic_url = f"file://{path.resolve()}"

    forward_path = f"/mosaicjson/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"

    # IMPORTANT: use list of tuples (like first function). Strip `v` — not a Titiler param.
    param_pairs = [(k, val) for k, val in request.query_params.multi_items() if k != "v"]
    param_pairs.append(("url", mosaic_url))

    # -----------------------------
    # CACHE (revision-scoped: mosaic JSON edits change etag → new Redis key)
    # -----------------------------
    cache_key = cache_key_for_titiler_request(forward_path, param_pairs, key_extra=etag)
    cached = get_cached_tile(cache_key)

    if cached is not None:
        body, ct = cached
        headers = _mosaic_cache_headers(etag_hdr=etag_hdr, versioned=use_versioned_cache)
        headers.update(
            {
                "X-Tile-Cache": "HIT",
                "X-Titiler-Upstream-Ms": "0",
                "X-Titiler-Upstream-Attempts": "0",
            }
        )
        return Response(
            content=body,
            media_type=ct,
            headers=headers,
        )

    if await request.is_disconnected():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -----------------------------
    # Fetch with retry + singleflight (concurrent identical key → one Titiler call)
    # -----------------------------
    async def _fetch_mosaic_tile() -> tuple[bytes, str, float, int]:
        client = get_titiler_http_client()
        r: httpx.Response | None = None
        titiler_upstream_ms = 0.0
        titiler_attempts = 0
        for attempt in range(2):
            await raise_if_disconnected(request)
            t0 = time.perf_counter()
            try:
                async def _gated_titiler_get() -> httpx.Response:
                    return await titiler_get_cancel_on_disconnect(
                        request,
                        client,
                        f"{base}{forward_path}",
                        params=param_pairs,
                        headers={"Accept-Encoding": "identity"},
                    )

                r = await titiler_upstream_gate_run(request, _gated_titiler_get)
            except httpx.RequestError as e:
                titiler_attempts += 1
                titiler_upstream_ms += (time.perf_counter() - t0) * 1000.0
                if attempt == 0:
                    logger.warning(
                        "titiler mosaic tile httpx error (retrying): view_id=%s z=%s x=%s y=%s err=%r",
                        view_id,
                        z,
                        x,
                        y,
                        e,
                    )
                    await asyncio.sleep(0.08)
                    await raise_if_disconnected(request)
                    continue
                err_short = str(e)[:512]
                logger.warning(
                    "titiler mosaic tile httpx error (giving up): view_id=%s z=%s x=%s y=%s "
                    "titiler_base=%s err=%r",
                    view_id,
                    z,
                    x,
                    y,
                    base,
                    e,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Titiler request failed: {e}",
                    headers={
                        "X-Titiler-Upstream-Ms": str(int(round(titiler_upstream_ms))),
                        "X-Titiler-Upstream-Attempts": str(titiler_attempts),
                        "X-Titiler-Connect-Error": err_short,
                    },
                ) from e
            titiler_attempts += 1
            titiler_upstream_ms += (time.perf_counter() - t0) * 1000.0
            if r.status_code in (502, 503, 504) and attempt == 0:
                await asyncio.sleep(0.08)
                await raise_if_disconnected(request)
                continue
            break

        assert r is not None
        ms_header = str(int(round(titiler_upstream_ms)))
        att_header = str(titiler_attempts)
        if r.status_code >= 400:
            detail = r.content[:2000]
            detail_txt = (
                detail.decode("utf-8", errors="replace") if detail else "Titiler error"
            )
            logger.warning(
                "titiler mosaic tile upstream http error: view_id=%s z=%s x=%s y=%s status=%s body_prefix=%s",
                view_id,
                z,
                x,
                y,
                r.status_code,
                detail_txt[:500].replace("\n", " "),
            )
            raise HTTPException(
                status_code=r.status_code,
                detail=detail_txt,
                headers={
                    "X-Titiler-Upstream-Ms": ms_header,
                    "X-Titiler-Upstream-Attempts": att_header,
                },
            )
        content_type = r.headers.get("content-type", "image/png")
        mosaic_ttl = settings.titiler_mosaic_tile_cache_ttl_seconds
        if mosaic_ttl <= 0:
            mosaic_ttl = settings.titiler_tile_cache_ttl_seconds
        set_cached_tile(
            cache_key,
            r.content,
            content_type,
            ttl_seconds=mosaic_ttl,
        )
        return r.content, content_type, titiler_upstream_ms, titiler_attempts

    try:
        body, content_type, titiler_upstream_ms, titiler_attempts = await await_tile_singleflight(
            cache_key,
            _fetch_mosaic_tile,
        )
    except ClientDisconnected:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if await request.is_disconnected():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ms_header = str(int(round(titiler_upstream_ms)))
    att_header = str(titiler_attempts)

    headers = _mosaic_cache_headers(etag_hdr=etag_hdr, versioned=use_versioned_cache)
    headers.update(
        {
            "X-Titiler-Upstream-Ms": ms_header,
            "X-Titiler-Upstream-Attempts": att_header,
        }
    )
    return Response(
        content=body,
        media_type=content_type,
        headers=headers,
    )


@router.get(
    "/{view_id}/titiler/point",
    summary="Sample mosaic view pixel values at lon/lat",
)
async def titiler_mosaic_point(
    request: Request,
    view_id: str,
    lon: float = Query(..., description="Longitude WGS84"),
    lat: float = Query(..., description="Latitude WGS84"),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    settings = get_settings()
    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Titiler not configured")

    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")

    allow_pm = getattr(row, "allow_public_maps", False)
    if current_user is None:
        if not can_access_raster_view_tiles_anonymous(
            visibility=row.visibility,
            allow_public_maps=allow_pm,
        ):
            raise HTTPException(status_code=404, detail="View not found")
    else:
        if not await can_see_raster_view(
            db, row.owner_id, row.visibility, view_id, current_user
        ):
            raise HTTPException(status_code=404, detail="View not found")

    path = Path(settings.raster_storage_path) / row.json_relative_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Mosaic JSON missing on disk")

    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if secret and fetch_base:
        mosaic_url = (
            f"{fetch_base}/internal/raster-views/{view_id}/mosaic.json"
            f"?token={quote(secret, safe='')}"
        )
    else:
        mosaic_url = f"file://{path.resolve()}"

    coord = f"{lon},{lat}"
    forward_path = f"/mosaicjson/point/{coord}"
    param_pairs = [(k, val) for k, val in request.query_params.multi_items() if k not in ("v", "lon", "lat")]
    param_pairs.append(("url", mosaic_url))

    client = get_titiler_http_client()
    raw = await fetch_titiler_point_json(
        client,
        base,
        forward_path,
        param_pairs,
        shared_secret=settings.titiler_internal_secret,
    )
    return JSONResponse(content=enrich_point_response(raw, {}))


@router.get(
    "/{view_id}/shares",
    response_model=list[ShareRead],
    summary="List shares for a raster mosaic",
)
async def list_raster_view_shares(
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_RASTER_VIEW, view_id)
    return [ShareRead(username=u, role=r) for u, r in shares]


@router.post(
    "/{view_id}/shares",
    response_model=ShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add share for a raster mosaic",
)
async def add_raster_view_share(
    view_id: str,
    payload: ShareAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    share = await resource_share_crud.add_share(
        db, RESOURCE_TYPE_RASTER_VIEW, view_id, payload.username, payload.role
    )
    if not share:
        raise HTTPException(status_code=404, detail="User not found")
    return ShareRead(username=share.username, role=share.role)


@router.delete(
    "/{view_id}/shares/{username}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove share from a raster mosaic",
)
async def remove_raster_view_share(
    view_id: str,
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    row = await raster_views_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="View not found")
    if not await can_edit_raster_view(db, row.owner_id, view_id, current_user):
        raise HTTPException(status_code=403, detail="Permission denied")
    ok = await resource_share_crud.remove_share(db, RESOURCE_TYPE_RASTER_VIEW, view_id, username)
    if not ok:
        raise HTTPException(status_code=404, detail="Share not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)