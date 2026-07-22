"""Proxy to Titiler for dynamic COG tiles (auth-checked)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_see_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.services.titiler_cancel import ClientDisconnected, raise_if_disconnected, titiler_get_cancel_on_disconnect
from app.services.titiler_gate import titiler_upstream_gate_run
from app.services.titiler_http import get_titiler_http_client
from app.services.titiler_retry import titiler_execute_with_retry
from app.services.titiler_inflight import await_tile_singleflight
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.titiler_tile_cache import (
    cache_key_for_titiler_request,
    get_cached_tile,
    set_cached_tile,
)
from app.services.coverages import CogPathOutsideStorageError, resolve_stored_cog_path
from app.services.zonal_statistics import (
    ensure_raster_coverage_feature,
    load_zone_feature_geojson,
    normalize_titiler_statistics_payload,
    post_titiler_zonal_statistics,
    query_flag_true,
    resolve_local_cog_url_for_titiler,
)

router = APIRouter()


def _cog_path_from_feature(feature) -> str | None:
    props = feature.properties or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if isinstance(raster, dict):
        p = raster.get("cog_path")
        return p if isinstance(p, str) and p else None
    return None


@router.get(
    "/{collection_id}/coverages/{feature_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy tile to Titiler (WebMercator etc.)",
    description="Requires Titiler sidecar (TITILER_INTERNAL_URL). Forwards query params (rescale, bidx, ...).",
)
async def titiler_proxy_tile(
    request: Request,
    collection_id: str,
    feature_id: str,
    tile_matrix_set_id: str,
    z: int,
    x: int,
    y: int,
    ext: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    base = settings.titiler_internal_url.rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )

    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to view this collection")

    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    cog_path = _cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")

    try:
        p = resolve_stored_cog_path(cog_path, settings.raster_storage_path)
    except CogPathOutsideStorageError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage") from None
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="COG file missing on disk")

    # Release the pooled DB connection before the slow upstream tile fetch, so a burst of tile
    # requests can't exhaust the DB pool and stall unrelated page requests.
    await db.close()

    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if secret and fetch_base:
        cog_url = f"{fetch_base}/internal/collections/{collection_id}/coverages/{feature_id}/cog?token={secret}"
    else:
        cog_url = f"file://{p.resolve()}"

    forward_path = f"/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
    param_pairs = [(k, v) for k, v in request.query_params.multi_items()]
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

    async def _fetch_cog_tile() -> tuple[bytes, str, float, int]:
        client = get_titiler_http_client()

        async def _before_retry(_attempt: int, _cause: BaseException | httpx.Response) -> None:
            await raise_if_disconnected(request)

        async def _gated_titiler_get() -> httpx.Response:
            return await titiler_get_cancel_on_disconnect(
                request,
                client,
                f"{base}{forward_path}",
                params=param_pairs,
                headers={"Accept-Encoding": "identity"},
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
            _fetch_cog_tile,
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
    "/{collection_id}/coverages/{feature_id}/statistics",
    summary="Zonal statistics for a raster coverage inside a zone feature",
    description=(
        "Loads the zone Polygon/MultiPolygon from the database "
        "(`zone_collection_id` + `zone_feature_id`) and POSTs it to Titiler "
        "`/cog/statistics`. Returns min/max/mean/count/std (and unique values when "
        "`categorical=true`). Extra query params (bidx, nodata, max_size, p, …) are "
        "forwarded to Titiler."
    ),
    response_class=JSONResponse,
)
async def coverage_zonal_statistics(
    request: Request,
    collection_id: str,
    feature_id: str,
    zone_collection_id: str = Query(..., description="Vector collection containing the zone feature"),
    zone_feature_id: str = Query(..., description="Feature id whose geometry is the analysis zone"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    feature = await ensure_raster_coverage_feature(db, collection_id, feature_id, current_user)
    geojson_feature, zone_meta = await load_zone_feature_geojson(
        db, zone_collection_id, zone_feature_id, current_user
    )
    cog_url = resolve_local_cog_url_for_titiler(
        collection_id=collection_id, feature_id=feature_id, feature=feature
    )
    await db.close()

    query_pairs = list(request.query_params.multi_items())
    categorical = query_flag_true(query_pairs, "categorical")
    raw = await post_titiler_zonal_statistics(
        forward_path="/cog/statistics",
        url=cog_url,
        geojson_feature=geojson_feature,
        query_pairs=query_pairs,
    )
    bidx = [v for k, v in query_pairs if k == "bidx" and v]
    payload = normalize_titiler_statistics_payload(
        raw,
        categorical=categorical,
        raster_meta={
            "collection_id": collection_id,
            "feature_id": feature_id,
            "bidx": [int(x) for x in bidx if str(x).isdigit()] or None,
        },
        zone_meta=zone_meta,
    )
    return JSONResponse(content=payload)
