"""Federated STAC Item Search: POST /search to each registered catalog and merge results."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.stac_catalog import StacCatalog

logger = logging.getLogger(__name__)

# Transient upstream failures — retry before giving up (reduces noise from occasional 502/503).
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504, 429})


def _normalize_root(url: str) -> str:
    return url.rstrip("/")


def _search_url(catalog: StacCatalog) -> str:
    return f"{_normalize_root(catalog.stac_api_root_url)}/search"


def _merge_item_collections(parts: list[dict[str, Any]], *, catalog_labels: list[str]) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, part in enumerate(parts):
        cat_id = catalog_labels[i] if i < len(catalog_labels) else "unknown"
        if not part:
            continue
        feats = part.get("features") or []
        for f in feats:
            if not isinstance(f, dict):
                continue
            fid = f.get("id") or ""
            dedupe_key = f"{cat_id}:{fid}"
            props = f.get("properties")
            if not isinstance(props, dict):
                props = {}
            # Copy key fields into properties so MapLibre popups (GeoJSON source features)
            # can access them consistently (it doesn't preserve arbitrary top-level members).
            if f.get("collection") and "collection" not in props:
                props = {**props, "collection": f.get("collection")}
            props = {**props, "geofast:sourceCatalog": cat_id}
            f = {**f, "properties": props}
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            features.append(f)
    return {
        "type": "FeatureCollection",
        "features": features,
        "numberMatched": len(features),
        "numberReturned": len(features),
    }


async def _post_search_with_retries(
    client: httpx.AsyncClient,
    catalog: StacCatalog,
    body: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """
    POST Item Search to one catalog. Retries on transient HTTP statuses and connection errors.
    Returns (json, None) on success, (None, short_error) on failure.
    """
    url = _search_url(catalog)
    req_body = dict(body)
    dc = catalog.default_collections
    if dc and isinstance(dc, list) and "collections" not in req_body:
        req_body["collections"] = dc

    settings = get_settings()
    max_retries = max(0, settings.stac_search_http_max_retries)
    max_attempts = 1 + max_retries
    backoff = settings.stac_search_http_retry_backoff_seconds

    for attempt in range(max_attempts):
        try:
            r = await client.post(
                url,
                json=req_body,
                headers={"Accept": "application/geo+json, application/json"},
            )
            if 200 <= r.status_code < 300:
                try:
                    return r.json(), None
                except Exception as e:
                    detail = f"Invalid JSON in response ({e})"
                    logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, detail)
                    return None, detail

            code = r.status_code
            if code in _RETRYABLE_HTTP_STATUS and attempt < max_attempts - 1:
                wait = backoff * (2**attempt)
                if code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            wait = max(wait, float(ra))
                        except ValueError:
                            pass
                logger.info(
                    "STAC upstream HTTP %s for catalog %s; retry %s/%s in %.1fs (%s)",
                    code,
                    catalog.id,
                    attempt + 1,
                    max_attempts,
                    wait,
                    url,
                )
                await asyncio.sleep(wait)
                continue

            detail = f"HTTP {code}" + (f" {r.reason_phrase}" if r.reason_phrase else "")
            if code in _RETRYABLE_HTTP_STATUS:
                logger.warning(
                    "STAC search failed for catalog %s (%s): %s after %s attempts",
                    catalog.id,
                    url,
                    detail,
                    max_attempts,
                )
            else:
                logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, detail)
            return None, detail

        except httpx.RequestError as e:
            if attempt < max_attempts - 1:
                wait = backoff * (2**attempt)
                logger.info(
                    "STAC search request error for catalog %s (%s): %s; retry %s/%s in %.1fs",
                    catalog.id,
                    url,
                    e,
                    attempt + 1,
                    max_attempts,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, e)
            return None, str(e) or type(e).__name__

    return None, "exhausted retries"


async def federated_search(
    catalogs: list[StacCatalog],
    body: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """
    POST Item Search to each catalog in parallel; merge FeatureCollections.

    Returns (merged_item_collection, catalog_errors) where catalog_errors entries are
    {"catalog_id": "...", "detail": "..."} for catalogs that returned no data.
    """
    settings = get_settings()
    if not catalogs:
        return (
            {"type": "FeatureCollection", "features": [], "numberMatched": 0, "numberReturned": 0},
            [],
        )

    stac_body = {k: v for k, v in body.items() if k not in ("catalog_ids", "geofast_catalog_ids")}
    timeout = settings.stac_search_http_timeout_seconds

    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [_post_search_with_retries(client, c, stac_body) for c in catalogs]
        results = await asyncio.gather(*tasks)

    labels = [c.id for c in catalogs]
    parts: list[dict[str, Any] | None] = []
    errors: list[dict[str, str]] = []
    for cat, (part, err) in zip(catalogs, results):
        parts.append(part)
        if err:
            errors.append({"catalog_id": cat.id, "detail": err})

    merged = _merge_item_collections([p for p in parts], catalog_labels=labels)
    return merged, errors
