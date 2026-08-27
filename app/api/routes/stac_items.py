"""STAC Item viewer (HTML) and Titiler proxy for assets from registered catalogs."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, get_current_user_required
from app.crud import stac_public_tile_grants as stac_public_grants_crud
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.models.user import User
from app.crud import stac_catalogs as stac_catalogs_crud
from app.db.session import get_db
from app.services.stac_item_client import (
    StacCatalogRef,
    default_tile_asset_key,
    get_asset_href,
    get_stac_item_cached,
    get_thumbnail_href,
    list_tile_assets,
)
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.titiler_cancel import ClientDisconnected, raise_if_disconnected, titiler_get_cancel_on_disconnect
from app.services.titiler_gate import titiler_upstream_gate_run
from app.services.titiler_http import get_titiler_http_client
from app.services.titiler_retry import titiler_execute_with_retry
from app.services.titiler_inflight import await_tile_singleflight
from app.services.titiler_tile_cache import cache_key_for_titiler_request, get_cached_tile, set_cached_tile
from app.services.titiler_point import (
    enrich_point_response,
    fetch_titiler_point_json,
    style_spec_from_request_query,
)

router = APIRouter()

# Hot path: tile requests must not query Postgres every time (browser limits ~6 concurrent connections
# per host; DB pool + session work per tile was a major bottleneck).
_CATALOG_REF_CACHE: dict[str, tuple[float, StacCatalogRef]] = {}
_CATALOG_REF_CACHE_TTL = 120.0
_CATALOG_REF_LOCK = asyncio.Lock()


async def _get_enabled_catalog_ref_for_tiles(db: AsyncSession, catalog_id: str) -> StacCatalogRef:
    now = time.monotonic()
    async with _CATALOG_REF_LOCK:
        hit = _CATALOG_REF_CACHE.get(catalog_id)
        if hit and (now - hit[0]) < _CATALOG_REF_CACHE_TTL:
            return hit[1]
    row = await stac_catalogs_crud.get_catalog(db, catalog_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STAC catalog not found")
    ref = StacCatalogRef(id=row.id, stac_api_root_url=row.stac_api_root_url)
    async with _CATALOG_REF_LOCK:
        _CATALOG_REF_CACHE[catalog_id] = (now, ref)
    return ref


async def _get_enabled_catalog(db: AsyncSession, catalog_id: str):
    row = await stac_catalogs_crud.get_catalog(db, catalog_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STAC catalog not found")
    return row


async def _titiler_session_or_public_grant(
    db: AsyncSession,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    current_user: User | None,
) -> None:
    """Logged-in users always; anonymous only if a public-tile grant exists."""
    if current_user is not None:
        return
    if await stac_public_grants_crud.has_grant(db, catalog_id, collection_id, item_id):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/public-tile-grant",
    summary="Whether anonymous users may load Titiler tiles (public map viewers)",
)
async def get_stac_public_tile_grant_flag(
    catalog_id: str,
    collection_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
):
    ok = await stac_public_grants_crud.has_grant(db, catalog_id, collection_id, item_id)
    return {"granted": ok}


@router.post(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/public-tile-grant",
    summary="Allow anonymous Titiler tile access for this item (owner consent, for public maps)",
)
async def post_stac_public_tile_grant(
    catalog_id: str,
    collection_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    await stac_public_grants_crud.ensure_grant(
        db,
        catalog_id=catalog_id,
        stac_collection_id=collection_id,
        stac_item_id=item_id,
        granted_by_user_id=current_user.id,
    )
    return {"granted": True}


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}",
    summary="STAC Item (JSON or HTML viewer)",
)
async def stac_item_detail(
    request: Request,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    base = str(request.base_url).rstrip("/")
    if not current_user:
        if wants_html(request):
            return RedirectResponse(
                url=f"{base}/auth/login?f=html&next={quote(str(request.url), safe='')}",
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    catalog = await _get_enabled_catalog(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog, collection_id, item_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream STAC error: HTTP {e.response.status_code}",
        ) from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream STAC unreachable: {e!s}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load STAC item: {e!s}",
        ) from e

    if wants_html(request):
        settings = get_settings()
        tile_assets = list_tile_assets(item)
        default_asset = default_tile_asset_key(item) or ""
        thumb = get_thumbnail_href(item)
        embed_raster = request.query_params.get("embed") == "raster"
        return html_response(
            "stac_item.html",
            base=base,
            username=current_user.username,
            is_admin=current_user.is_admin,
            catalog_id=catalog_id,
            catalog_title=catalog.title,
            collection_id=collection_id,
            item_id=item_id,
            stac_item=item,
            tile_assets=tile_assets,
            default_tile_asset=default_asset,
            thumbnail_url=thumb,
            titiler_configured=bool((settings.titiler_internal_url or "").strip()),
            google_maps_api_key=settings.google_maps_api_key or "",
            raster_style_embed=embed_raster,
            hide_site_chrome=embed_raster,
        )
    return item


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy STAC asset to Titiler (raster tiles)",
    description="Requires TITILER_INTERNAL_URL. Either query param `asset` (single asset via /cog/tiles) or repeated `assets` (multi-asset via /stac/tiles). Other params are forwarded to Titiler (bidx, rescale, expression, color_formula, etc.).",
)
async def stac_item_titiler_tile(
    request: Request,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    asset: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    await _titiler_session_or_public_grant(
        db, catalog_id, collection_id, item_id, current_user
    )
    settings = get_settings()
    base_t = settings.titiler_internal_url.rstrip("/")
    if not base_t:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )

    catalog_ref = await _get_enabled_catalog_ref_for_tiles(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog_ref, collection_id, item_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or invalid asset key")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not load STAC item: {e!s}",
        ) from e

    # If `assets` is provided (repeat param), proxy through Titiler's /stac/tiles (multi-asset).
    requested_assets = [v for (k, v) in request.query_params.multi_items() if k == "assets" and v]
    if requested_assets:
        # Prefer upstream item self href if present; otherwise build from catalog root.
        item_url = None
        try:
            for L in item.get("links") or []:
                if isinstance(L, dict) and L.get("rel") == "self" and L.get("href"):
                    item_url = str(L["href"])
                    break
        except Exception:
            item_url = None
        if not item_url:
            item_url = f"{catalog_ref.stac_api_root_url.rstrip('/')}/collections/{collection_id}/items/{item_id}"

        forward_path = f"/stac/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        param_pairs: list[tuple[str, str]] = [
            (k, v) for k, v in request.query_params.multi_items() if k not in ("asset",)
        ]
        # Ensure required params exist.
        param_pairs.append(("url", item_url))
        # Always pass assets (repeat).
        # (They are already in param_pairs, but we keep them as-is.)
    else:
        if not asset or not str(asset).strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query param `asset` (or `assets`) is required")
        cog_url = get_asset_href(item, str(asset).strip())
        forward_path = f"/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        param_pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "asset"]
        param_pairs.append(("url", cog_url))

    cache_key = cache_key_for_titiler_request(forward_path, param_pairs)
    cached = get_cached_tile(cache_key)
    if cached is not None:
        body, ct = cached
        return Response(
            content=body,
            media_type=ct,
            headers={
                "Cache-Control": "public, max-age=31536000, s-maxage=31536000, immutable",
                "X-Tile-Cache": "HIT",
                "X-Titiler-Upstream-Ms": "0",
                "X-Titiler-Upstream-Attempts": "0",
            },
        )

    if await request.is_disconnected():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _fetch_stac_tile() -> tuple[bytes, str, float, int]:
        client = get_titiler_http_client()

        async def _before_retry(_attempt: int, _cause: BaseException | httpx.Response) -> None:
            await raise_if_disconnected(request)

        async def _gated_titiler_get() -> httpx.Response:
            return await titiler_get_cancel_on_disconnect(
                request,
                client,
                f"{base_t}{forward_path}",
                params=param_pairs,
            )

        t0 = time.perf_counter()
        try:
            r, titiler_attempts = await titiler_execute_with_retry(
                lambda: titiler_upstream_gate_run(request, _gated_titiler_get),
                before_retry=_before_retry,
            )
        except httpx.RequestError as e:
            titiler_upstream_ms = (time.perf_counter() - t0) * 1000.0
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Titiler request failed: {e}",
                headers={
                    "X-Titiler-Upstream-Ms": str(max(0, int(round(titiler_upstream_ms)))),
                    "X-Titiler-Upstream-Attempts": str(
                        max(1, int(get_settings().titiler_retry_max_attempts))
                    ),
                },
            ) from e
        titiler_upstream_ms = (time.perf_counter() - t0) * 1000.0
        ms_header = str(max(0, int(round(titiler_upstream_ms))))
        att_header = str(titiler_attempts)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=r.status_code,
                detail=sanitize_titiler_upstream_error_text(
                    r.text,
                    shared_secret=settings.titiler_internal_secret,
                    max_len=2000,
                ),
                headers={
                    "X-Titiler-Upstream-Ms": ms_header,
                    "X-Titiler-Upstream-Attempts": att_header,
                },
            )
        ct = r.headers.get("content-type", "image/png")
        set_cached_tile(cache_key, r.content, ct)
        return r.content, ct, titiler_upstream_ms, titiler_attempts

    try:
        body, ct, titiler_upstream_ms, titiler_attempts = await await_tile_singleflight(
            cache_key,
            _fetch_stac_tile,
        )
    except ClientDisconnected:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if await request.is_disconnected():
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ms_header = str(max(0, int(round(titiler_upstream_ms))))
    att_header = str(titiler_attempts)
    return Response(
        content=body,
        media_type=ct,
        headers={
            "Cache-Control": "public, max-age=31536000, s-maxage=31536000, immutable",
            "X-Titiler-Upstream-Ms": ms_header,
            "X-Titiler-Upstream-Attempts": att_header,
        },
    )

@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/titiler/point",
    summary="Sample STAC raster pixel values at lon/lat",
)
async def stac_item_titiler_point(
    request: Request,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    lon: float = Query(..., description="Longitude WGS84"),
    lat: float = Query(..., description="Latitude WGS84"),
    asset: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    await _titiler_session_or_public_grant(
        db, catalog_id, collection_id, item_id, current_user
    )
    settings = get_settings()
    base_t = settings.titiler_internal_url.rstrip("/")
    if not base_t:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )

    catalog_ref = await _get_enabled_catalog_ref_for_tiles(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog_ref, collection_id, item_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or invalid asset key")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not load STAC item: {e!s}",
        ) from e

    coord = f"{lon},{lat}"
    requested_assets = [v for (k, v) in request.query_params.multi_items() if k == "assets" and v]
    if requested_assets:
        item_url = None
        try:
            for L in item.get("links") or []:
                if isinstance(L, dict) and L.get("rel") == "self" and L.get("href"):
                    item_url = str(L["href"])
                    break
        except Exception:
            item_url = None
        if not item_url:
            item_url = f"{catalog_ref.stac_api_root_url.rstrip('/')}/collections/{collection_id}/items/{item_id}"
        forward_path = f"/stac/point/{coord}"
        param_pairs: list[tuple[str, str]] = [
            (k, v) for k, v in request.query_params.multi_items() if k not in ("asset", "lon", "lat")
        ]
        param_pairs.append(("url", item_url))
    else:
        if not asset or not str(asset).strip():
            asset = request.query_params.get("asset")
        if not asset or not str(asset).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query param `asset` (or `assets`) is required",
            )
        cog_url = get_asset_href(item, str(asset).strip())
        forward_path = f"/cog/point/{coord}"
        param_pairs = [
            (k, v) for k, v in request.query_params.multi_items() if k not in ("asset", "lon", "lat")
        ]
        param_pairs.append(("url", cog_url))

    client = get_titiler_http_client()
    raw = await fetch_titiler_point_json(
        client,
        base_t,
        forward_path,
        param_pairs,
        shared_secret=settings.titiler_internal_secret,
    )
    pseudo_spec = style_spec_from_request_query(request)
    return JSONResponse(content=enrich_point_response(raw, pseudo_spec))


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/titiler/suggest-rescale",
    summary="Suggest rescale for Titiler rendering (percentiles)",
)
async def stac_item_titiler_suggest_rescale(
    request: Request,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """
    Compute a reasonable `rescale=min,max` suggestion using Titiler statistics percentiles.

    Supports either:
    - `asset=<key>` + optional repeated `bidx=<n>` (defaults to 1..3 when absent)
    - repeated `assets=<key>` (RGB assets mode) + `asset_as_band=true`
    """
    await _titiler_session_or_public_grant(
        db, catalog_id, collection_id, item_id, current_user
    )
    settings = get_settings()
    base_t = settings.titiler_internal_url.rstrip("/")
    if not base_t:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Titiler not configured")

    catalog_ref = await _get_enabled_catalog_ref_for_tiles(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog_ref, collection_id, item_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not load STAC item: {e!s}") from e

    q = request.query_params
    assets = [v for (k, v) in q.multi_items() if k == "assets" and v]
    asset = q.get("asset")
    bidx_list = [v for (k, v) in q.multi_items() if k == "bidx" and v]

    # Percentiles
    params: list[tuple[str, str]] = [("p", "2"), ("p", "98")]

    if assets:
        # /stac/statistics
        item_url = None
        try:
            for L in item.get("links") or []:
                if isinstance(L, dict) and L.get("rel") == "self" and L.get("href"):
                    item_url = str(L["href"])
                    break
        except Exception:
            item_url = None
        if not item_url:
            item_url = f"{catalog_ref.stac_api_root_url.rstrip('/')}/collections/{collection_id}/items/{item_id}"
        params.append(("url", item_url))
        for a in assets:
            params.append(("assets", a))
        if q.get("asset_as_band"):
            params.append(("asset_as_band", q.get("asset_as_band") or "true"))
        stats_url = f"{base_t}/stac/statistics"
    else:
        # /cog/statistics
        if not asset:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide `asset` or repeated `assets`")
        cog_url = get_asset_href(item, asset.strip())
        params.append(("url", cog_url))
        if bidx_list:
            for b in bidx_list:
                params.append(("bidx", b))
        else:
            params.extend([("bidx", "1"), ("bidx", "2"), ("bidx", "3")])
        stats_url = f"{base_t}/cog/statistics"

    try:
        client = get_titiler_http_client()
        r = await client.get(stats_url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Titiler statistics failed: {e!s}") from e

    # Titiler returns per-band/asset stats; derive one combined min/max to match our single rescale input.
    def _float(x):
        try:
            return float(x)
        except Exception:
            return None

    def _pick_percentile(perc: object, key: str) -> float | None:
        """Extract percentile value from common Titiler shapes."""
        if isinstance(perc, dict):
            for k in (key, f"{key}.0", f"{int(key):d}", f"p{key}"):
                if k in perc:
                    return _float(perc.get(k))
        if isinstance(perc, list):
            # Rare shapes: [{"percentile": 2, "value": ...}, ...] or [[2, ...], ...]
            for entry in perc:
                if isinstance(entry, dict) and "percentile" in entry and "value" in entry:
                    if str(entry.get("percentile")) in (key, f"{key}.0"):
                        return _float(entry.get("value"))
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    if str(entry[0]) in (key, f"{key}.0"):
                        return _float(entry[1])
        return None

    mins: list[float] = []
    maxs: list[float] = []

    # Common shapes:
    # - { "statistics": { "1": { "percentile_2": .., "percentile_98": .. }, ... } }
    # - { "statistics": [ { "percentile_2": .., "percentile_98": .. }, ... ] }
    stats = data.get("statistics") if isinstance(data, dict) else None
    if isinstance(stats, dict):
        for v in stats.values():
            if not isinstance(v, dict):
                continue
            perc = v.get("percentiles")
            lo = _pick_percentile(perc, "2") or _float(v.get("percentile_2") or v.get("percentile_02") or v.get("p2"))
            hi = _pick_percentile(perc, "98") or _float(v.get("percentile_98") or v.get("p98"))
            if lo is not None and hi is not None:
                mins.append(lo)
                maxs.append(hi)
    elif isinstance(stats, list):
        for v in stats:
            if not isinstance(v, dict):
                continue
            perc = v.get("percentiles")
            lo = _pick_percentile(perc, "2") or _float(v.get("percentile_2") or v.get("percentile_02") or v.get("p2"))
            hi = _pick_percentile(perc, "98") or _float(v.get("percentile_98") or v.get("p98"))
            if lo is not None and hi is not None:
                mins.append(lo)
                maxs.append(hi)

    if not mins or not maxs:
        return {"suggested": "", "detail": "No percentile stats returned"}

    lo = min(mins)
    hi = max(maxs)
    # Clamp nonsensical ranges
    if hi <= lo:
        return {"suggested": "", "detail": "Invalid percentile range"}
    return {"suggested": f"{lo:.0f},{hi:.0f}", "p2": lo, "p98": hi}


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/titiler/statistics",
    summary="Zonal statistics for a STAC item asset inside a zone feature",
    description=(
        "Loads the zone from the database (`zone_collection_id` + `zone_feature_id`) and "
        "POSTs it to Titiler. Use `asset` for a single COG asset (`/cog/statistics`) or "
        "repeated `assets` for multi-asset (`/stac/statistics`). Set `categorical=true` for "
        "unique value counts."
    ),
)
async def stac_item_titiler_statistics(
    request: Request,
    catalog_id: str,
    collection_id: str,
    item_id: str,
    zone_collection_id: str = Query(..., description="Vector collection containing the zone feature"),
    zone_feature_id: str = Query(..., description="Feature id whose geometry is the analysis zone"),
    asset: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    from app.services.zonal_statistics import (
        load_zone_feature_geojson,
        normalize_titiler_statistics_payload,
        post_titiler_zonal_statistics,
        query_flag_true,
    )

    await _titiler_session_or_public_grant(db, catalog_id, collection_id, item_id, current_user)
    # Zone always requires permission on the zone collection (logged-in or public zone).
    geojson_feature, zone_meta = await load_zone_feature_geojson(
        db, zone_collection_id, zone_feature_id, current_user, require_auth_user=False
    )

    catalog_ref = await _get_enabled_catalog_ref_for_tiles(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog_ref, collection_id, item_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or invalid asset key")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not load STAC item: {e!s}",
        ) from e

    await db.close()

    query_pairs = list(request.query_params.multi_items())
    categorical = query_flag_true(query_pairs, "categorical")
    requested_assets = [v for (k, v) in query_pairs if k == "assets" and v]

    if requested_assets:
        item_url = None
        try:
            for L in item.get("links") or []:
                if isinstance(L, dict) and L.get("rel") == "self" and L.get("href"):
                    item_url = str(L["href"])
                    break
        except Exception:
            item_url = None
        if not item_url:
            item_url = (
                f"{catalog_ref.stac_api_root_url.rstrip('/')}/collections/{collection_id}/items/{item_id}"
            )
        forward_path = "/stac/statistics"
        url = item_url
        raster_meta = {
            "catalog_id": catalog_id,
            "collection_id": collection_id,
            "item_id": item_id,
            "assets": requested_assets,
        }
        drop_extra = frozenset({"asset"})
    else:
        if not asset or not str(asset).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query param `asset` (or repeated `assets`) is required",
            )
        try:
            cog_url = get_asset_href(item, str(asset).strip())
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or invalid asset key")
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        forward_path = "/cog/statistics"
        url = cog_url
        raster_meta = {
            "catalog_id": catalog_id,
            "collection_id": collection_id,
            "item_id": item_id,
            "asset": str(asset).strip(),
        }
        drop_extra = frozenset({"asset"})

    raw = await post_titiler_zonal_statistics(
        forward_path=forward_path,
        url=url,
        geojson_feature=geojson_feature,
        query_pairs=query_pairs,
        drop_keys=drop_extra,
    )
    payload = normalize_titiler_statistics_payload(
        raw,
        categorical=categorical,
        raster_meta=raster_meta,
        zone_meta=zone_meta,
    )
    return JSONResponse(content=payload)
