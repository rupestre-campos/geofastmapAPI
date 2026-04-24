"""Fetch STAC /collections listings from registered catalog roots (with pagination)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.config import get_settings
from app.models.stac_catalog import StacCatalog
from app.services.stac_search_cache import cache_get_str, cache_set_str, collections_cache_key

logger = logging.getLogger(__name__)


def _normalize_root(url: str) -> str:
    return url.rstrip("/")


def _collections_page_url(root: str, href: str | None) -> str:
    if not href:
        return f"{_normalize_root(root)}/collections"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(_normalize_root(root) + "/", href.lstrip("/"))


async def fetch_collections_for_catalog(client: httpx.AsyncClient, catalog: StacCatalog) -> list[dict[str, str]]:
    """
    Return [{ "id": str, "title": str }] for one STAC catalog (follows rel=next).
    """
    settings0 = get_settings()
    ttl = settings0.stac_collections_cache_ttl_seconds
    ck = collections_cache_key(catalog.id) if ttl > 0 else ""
    if ttl > 0:
        raw = cache_get_str(ck)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
            except Exception:
                pass

    root = _normalize_root(catalog.stac_api_root_url)
    url: str | None = f"{root}/collections"
    out: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    timeout = get_settings().stac_search_http_timeout_seconds

    while url and url not in seen_urls:
        seen_urls.add(url)
        try:
            r = await client.get(
                url,
                headers={"Accept": "application/geo+json, application/json"},
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("STAC collections list failed for %s (%s): %s", catalog.id, url, e)
            break

        coll = data.get("collections") if isinstance(data, dict) else None
        if isinstance(coll, list):
            for item in coll:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                if not cid:
                    continue
                title = item.get("title") or item.get("description") or cid
                if not isinstance(title, str):
                    title = str(cid)
                out.append({"id": str(cid), "title": title})

        next_url: str | None = None
        if isinstance(data, dict):
            for link in data.get("links") or []:
                if not isinstance(link, dict):
                    continue
                if link.get("rel") == "next" and link.get("href"):
                    next_url = _collections_page_url(root, str(link["href"]))
                    break
        url = next_url

    if ttl > 0 and out:
        cache_set_str(ck, json.dumps(out, separators=(",", ":")), ttl)

    return out


async def fetch_collections_grouped(catalogs: list[StacCatalog]) -> list[dict[str, Any]]:
    """
    For UI optgroups: [{ "catalog_id": str, "catalog_title": str, "collections": [{ "id", "title" }, ...] }].
    """
    if not catalogs:
        return []

    settings = get_settings()
    timeout = settings.stac_search_http_timeout_seconds
    ua = (settings.stac_http_user_agent or "").strip() or "GeoFastMap-STAC/1.0"
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": ua, "Accept": "application/geo+json, application/json"},
    ) as client:
        tasks = [fetch_collections_for_catalog(client, c) for c in catalogs]
        parts = await asyncio.gather(*tasks)

    grouped: list[dict[str, Any]] = []
    for cat, cols in zip(catalogs, parts):
        grouped.append(
            {
                "catalog_id": cat.id,
                "catalog_title": cat.title,
                "stac_api_root_url": cat.stac_api_root_url,
                "collections": cols,
            }
        )
    return grouped
