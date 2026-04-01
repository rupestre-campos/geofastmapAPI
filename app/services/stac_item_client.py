"""Fetch STAC items from registered catalog roots (with short in-memory cache)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, NamedTuple, Union
from urllib.parse import quote, urlparse

import httpx

from app.core.config import get_settings
from app.models.stac_catalog import StacCatalog

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()
_CACHE_TTL = 90.0
# Single-flight: concurrent tile requests share one upstream STAC GET per cache key.
_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}


class StacCatalogRef(NamedTuple):
    """Minimal catalog fields for STAC item fetch (avoids ORM / DB on hot tile paths when cached)."""

    id: str
    stac_api_root_url: str


def stac_item_href(catalog: Union[StacCatalog, StacCatalogRef], collection_id: str, item_id: str) -> str:
    root = catalog.stac_api_root_url.rstrip("/")
    c = quote(collection_id, safe="")
    i = quote(item_id, safe="")
    return f"{root}/collections/{c}/items/{i}"


async def fetch_stac_item_json(catalog: Union[StacCatalog, StacCatalogRef], collection_id: str, item_id: str) -> dict[str, Any]:
    """GET Item JSON from upstream STAC API."""
    url = stac_item_href(catalog, collection_id, item_id)
    timeout = get_settings().stac_search_http_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, headers={"Accept": "application/geo+json, application/json, */*"})
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise ValueError("STAC item response is not an object")
    return data


async def get_stac_item_cached(catalog: Union[StacCatalog, StacCatalogRef], collection_id: str, item_id: str) -> dict[str, Any]:
    key = f"{catalog.id}:{collection_id}:{item_id}"
    now = time.monotonic()
    async with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]
        if key not in _INFLIGHT:

            async def _fetch_and_store() -> dict[str, Any]:
                try:
                    data = await fetch_stac_item_json(catalog, collection_id, item_id)
                except BaseException:
                    async with _CACHE_LOCK:
                        _INFLIGHT.pop(key, None)
                    raise
                async with _CACHE_LOCK:
                    _CACHE[key] = (time.monotonic(), data)
                    _INFLIGHT.pop(key, None)
                return data

            _INFLIGHT[key] = asyncio.create_task(_fetch_and_store())
        task = _INFLIGHT[key]
    return await task


def _looks_private_or_loopback_host(host: str) -> bool:
    h = host.lower()
    if h in ("localhost", "::1", "0.0.0.0"):
        return True
    if h.endswith(".local") or h.endswith(".localhost"):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b, c, d = (int(p) for p in parts)
        if a == 10 or a == 127:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        if a == 169 and b == 254:
            return True
    return False


def assert_https_raster_url(url: str) -> None:
    """Reject obvious SSRF targets; allow public HTTPS imagery (e.g. S3)."""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("Asset URL must use https")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("Asset URL has no host")
    if _looks_private_or_loopback_host(host):
        raise ValueError("Blocked host")


def get_asset_href(item: dict[str, Any], asset_key: str) -> str:
    assets = item.get("assets")
    if not isinstance(assets, dict):
        raise KeyError("no assets")
    meta = assets.get(asset_key)
    if not isinstance(meta, dict):
        raise KeyError(asset_key)
    href = meta.get("href")
    if not isinstance(href, str) or not href.strip():
        raise KeyError(asset_key)
    href = href.strip()
    assert_https_raster_url(href)
    return href


def list_tile_assets(item: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Assets that are reasonable to pass to Titiler as COG/GeoTIFF.
    Each entry: { "key", "title", "type", "roles" }.
    """
    assets = item.get("assets")
    if not isinstance(assets, dict):
        return []
    out: list[dict[str, Any]] = []
    for key, meta in assets.items():
        if not isinstance(meta, dict):
            continue
        href = meta.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        mt = str(meta.get("type") or "").lower()
        roles = meta.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        roles_l = [str(x).lower() for x in roles if x]
        tiffish = (
            "tiff" in mt
            or "geotiff" in mt
            or "cog" in mt
            or "data" in roles_l
            or "visual" in roles_l
            or "overview" in roles_l
        )
        if not tiffish:
            continue
        try:
            assert_https_raster_url(href.strip())
        except ValueError:
            continue
        title = meta.get("title") or key
        out.append({"key": key, "title": str(title), "type": mt, "roles": roles_l})
    priority = {"visual": 0, "true-color": 1, "rgb": 2, "overview": 3, "data": 4}

    def sort_key(a: dict[str, Any]) -> tuple[int, str]:
        k = a["key"].lower()
        return (priority.get(k, 50), k)

    out.sort(key=sort_key)
    return out


def default_tile_asset_key(item: dict[str, Any]) -> str | None:
    lst = list_tile_assets(item)
    return lst[0]["key"] if lst else None


def get_thumbnail_href(item: dict[str, Any]) -> str | None:
    assets = item.get("assets")
    if not isinstance(assets, dict):
        return None
    for pref in ("thumbnail", "preview"):
        meta = assets.get(pref)
        if isinstance(meta, dict):
            href = meta.get("href")
            if isinstance(href, str) and href.startswith("https://"):
                try:
                    assert_https_raster_url(href)
                    return href
                except ValueError:
                    continue
    return None
