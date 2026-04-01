"""STAC Item viewer (HTML) and Titiler proxy for assets from registered catalogs."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import stac_catalogs as stac_catalogs_crud
from app.db.session import get_db
from app.services.stac_item_client import (
    default_tile_asset_key,
    get_asset_href,
    get_stac_item_cached,
    get_thumbnail_href,
    list_tile_assets,
)

router = APIRouter()


async def _get_enabled_catalog(db: AsyncSession, catalog_id: str):
    row = await stac_catalogs_crud.get_catalog(db, catalog_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="STAC catalog not found")
    return row


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
):
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
        base = str(request.base_url).rstrip("/")
        return html_response(
            "stac_item.html",
            base=base,
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
        )
    return item


@router.get(
    "/catalogs/{catalog_id}/collections/{collection_id}/items/{item_id}/titiler/tiles/{tile_matrix_set_id}/{z:int}/{x:int}/{y:int}.{ext}",
    summary="Proxy STAC asset to Titiler (raster tiles)",
    description="Requires TITILER_INTERNAL_URL. Query param `asset` selects the STAC asset key; other params are forwarded to Titiler (bidx, rescale, etc.).",
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
    asset: str,
    db: AsyncSession = Depends(get_db),
):
    if not asset or not asset.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query param `asset` is required")
    settings = get_settings()
    base_t = settings.titiler_internal_url.rstrip("/")
    if not base_t:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )

    catalog = await _get_enabled_catalog(db, catalog_id)
    try:
        item = await get_stac_item_cached(catalog, collection_id, item_id)
        cog_url = get_asset_href(item, asset.strip())
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or invalid asset key")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not load STAC item: {e!s}",
        ) from e

    forward_path = f"/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
    param_pairs: list[tuple[str, str]] = [
        (k, v) for k, v in request.query_params.multi_items() if k != "asset"
    ]
    param_pairs.append(("url", cog_url))

    timeout = float(settings.stac_search_http_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base_t}{forward_path}", params=param_pairs)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Titiler request failed: {e}",
        ) from e

    if r.status_code >= 400:
        raise HTTPException(
            status_code=r.status_code,
            detail=r.text[:2000] if r.text else "Titiler error",
        )

    ct = r.headers.get("content-type", "image/png")
    return Response(
        content=r.content,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=3600"},
    )
