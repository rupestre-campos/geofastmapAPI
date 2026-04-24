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


def _stac_client_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    ua = (get_settings().stac_http_user_agent or "").strip() or "GeoFastMap-STAC/1.0"
    h = {
        "User-Agent": ua,
        "Accept": "application/geo+json, application/json",
    }
    if extra:
        h.update(extra)
    return h


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
                headers=_stac_client_headers(),
            )
            if 200 <= r.status_code < 300:
                try:
                    return r.json(), None
                except Exception as e:
                    detail = f"Invalid JSON in response ({e})"
                    logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, detail)
                    return None, detail

            code = r.status_code
            # Some upstream STAC APIs don't support the Query extension and return 400 when a "query"
            # filter is included (even if it's lenient like cloud_cover_max=100). In that case,
            # retry once without the query filter so searches still work.
            if code == 400 and isinstance(req_body.get("query"), dict) and attempt == 0:
                q = req_body.get("query") or {}
                if isinstance(q, dict) and "eo:cloud_cover" in q:
                    try:
                        req_body2 = dict(req_body)
                        req_body2.pop("query", None)
                        r2 = await client.post(
                            url,
                            json=req_body2,
                            headers=_stac_client_headers(),
                        )
                        if 200 <= r2.status_code < 300:
                            try:
                                return r2.json(), None
                            except Exception as e:
                                detail = f"Invalid JSON in response after dropping query ({e})"
                                logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, detail)
                                return None, detail
                    except httpx.RequestError:
                        pass
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

            # Try to include upstream error detail (often JSON with "description"/"detail").
            extra = ""
            try:
                ct = (r.headers.get("content-type") or "").lower()
                if "json" in ct:
                    j = r.json()
                    if isinstance(j, dict):
                        msg = j.get("detail") or j.get("description") or j.get("message")
                        if msg:
                            extra = f": {msg}"
                elif r.text:
                    extra = f": {r.text[:200]}"
            except Exception:
                pass
            detail = f"HTTP {code}" + (f" {r.reason_phrase}" if r.reason_phrase else "") + extra
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
    cat_parallelism = max(1, int(settings.mosaic_stac_catalog_parallelism or 1))
    inflight_budget = max(1, int(settings.mosaic_stac_total_inflight_max or cat_parallelism))
    sem_cat = asyncio.Semaphore(cat_parallelism)
    sem_total = asyncio.Semaphore(inflight_budget)

    async with httpx.AsyncClient(timeout=timeout, headers=_stac_client_headers()) as client:
        async def _run_catalog(c: StacCatalog) -> tuple[dict[str, Any] | None, str | None]:
            async with sem_total:
                async with sem_cat:
                    return await _post_search_with_retries(client, c, stac_body)

        tasks = [_run_catalog(c) for c in catalogs]
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
