"""Federated STAC Item Search: POST /search to each registered catalog and merge results."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.stac_catalog import StacCatalog

logger = logging.getLogger(__name__)

# Mosaic subtasks call federated_search with a higher catalog fan-out without changing API/search defaults.
_mosaic_subtask_catalog_parallelism: ContextVar[int | None] = ContextVar(
    "_mosaic_subtask_catalog_parallelism", default=None
)


@contextmanager
def mosaic_subtask_federation_catalog_parallelism(catalog_parallelism: int):
    token = _mosaic_subtask_catalog_parallelism.set(max(1, int(catalog_parallelism)))
    try:
        yield
    finally:
        _mosaic_subtask_catalog_parallelism.reset(token)

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


def _retry_wait_seconds(
    *,
    base_backoff: float,
    attempt: int,
    max_backoff: float,
    retry_after_seconds: float | None = None,
) -> float:
    """
    Exponential backoff with optional Retry-After hint, capped by max_backoff.
    """
    base = max(0.0, float(base_backoff))
    cap = max(0.0, float(max_backoff))
    wait = base * (2**max(0, int(attempt)))
    if retry_after_seconds is not None:
        try:
            wait = max(wait, float(retry_after_seconds))
        except (TypeError, ValueError):
            pass
    return min(cap, wait)


def _extract_next_link(part: dict[str, Any]) -> dict[str, Any] | None:
    links = part.get("links")
    if not isinstance(links, list):
        return None
    for l in links:
        if not isinstance(l, dict):
            continue
        if str(l.get("rel") or "").lower() != "next":
            continue
        href = str(l.get("href") or "").strip()
        if not href:
            continue
        method = str(l.get("method") or "GET").upper()
        body = l.get("body")
        if method not in ("GET", "POST"):
            method = "GET"
        return {"href": href, "method": method, "body": body if isinstance(body, dict) else None}
    return None


async def _request_json_with_retries(
    client: httpx.AsyncClient,
    *,
    catalog: StacCatalog,
    method: str,
    url: str,
    body: dict[str, Any] | None,
    max_attempts: int,
    backoff: float,
    max_backoff: float,
) -> tuple[dict[str, Any] | None, str | None, int]:
    for attempt in range(max_attempts):
        try:
            if method == "POST":
                r = await client.post(url, json=body or {}, headers=_stac_client_headers())
            else:
                r = await client.get(url, headers=_stac_client_headers())
            if 200 <= r.status_code < 300:
                try:
                    return r.json(), None, r.status_code
                except Exception as e:
                    return None, f"Invalid JSON in response ({e})", r.status_code
            code = r.status_code
            if code in _RETRYABLE_HTTP_STATUS and attempt < max_attempts - 1:
                retry_after_sec: float | None = None
                if code == 429:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            retry_after_sec = float(ra)
                        except ValueError:
                            pass
                wait = _retry_wait_seconds(
                    base_backoff=backoff,
                    attempt=attempt,
                    max_backoff=max_backoff,
                    retry_after_seconds=retry_after_sec,
                )
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
            return None, detail, code
        except httpx.RequestError as e:
            if attempt < max_attempts - 1:
                wait = _retry_wait_seconds(
                    base_backoff=backoff,
                    attempt=attempt,
                    max_backoff=max_backoff,
                    retry_after_seconds=None,
                )
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
            return None, str(e) or type(e).__name__, 0
    return None, "exhausted retries", 0


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
    max_backoff = max(1.0, float(getattr(settings, "stac_search_http_retry_backoff_max_seconds", 300.0) or 300.0))

    part, err, code = await _request_json_with_retries(
        client,
        catalog=catalog,
        method="POST",
        url=url,
        body=req_body,
        max_attempts=max_attempts,
        backoff=backoff,
        max_backoff=max_backoff,
    )
    if part is not None:
        page_max = max(1, int(getattr(settings, "stac_search_http_max_pages", 1) or 1))
        page_delay = max(0.0, float(getattr(settings, "stac_search_http_page_delay_seconds", 0.0) or 0.0))
        merged_feats = list(part.get("features") or [])
        cur = part
        page_num = 1
        while page_num < page_max:
            nxt = _extract_next_link(cur)
            if not nxt:
                break
            if page_delay > 0:
                await asyncio.sleep(page_delay)
            n_part, n_err, _ncode = await _request_json_with_retries(
                client,
                catalog=catalog,
                method=str(nxt["method"]),
                url=str(nxt["href"]),
                body=nxt.get("body"),
                max_attempts=max_attempts,
                backoff=backoff,
                max_backoff=max_backoff,
            )
            if n_part is None:
                logger.warning(
                    "STAC next-page fetch failed for catalog %s (%s): %s",
                    catalog.id,
                    nxt.get("href"),
                    n_err or "unknown",
                )
                break
            merged_feats.extend(list(n_part.get("features") or []))
            cur = n_part
            page_num += 1
        out = dict(part)
        out["features"] = merged_feats
        out["numberReturned"] = len(merged_feats)
        return out, None
    # Some upstream STAC APIs don't support the Query extension and return HTTP 400
    # when a "query" filter is present. Retry once without cloud query in that case.
    if code == 400 and isinstance(req_body.get("query"), dict):
        q = req_body.get("query") or {}
        if isinstance(q, dict) and "eo:cloud_cover" in q:
            req_body2 = dict(req_body)
            req_body2.pop("query", None)
            p2, e2, _c2 = await _request_json_with_retries(
                client,
                catalog=catalog,
                method="POST",
                url=url,
                body=req_body2,
                max_attempts=max_attempts,
                backoff=backoff,
                max_backoff=max_backoff,
            )
            if p2 is not None:
                return p2, None
            if e2:
                logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, e2)
                return None, e2
    if err:
        if code in _RETRYABLE_HTTP_STATUS:
            logger.warning(
                "STAC search failed for catalog %s (%s): %s after %s attempts",
                catalog.id,
                url,
                err,
                max_attempts,
            )
        else:
            logger.warning("STAC search failed for catalog %s (%s): %s", catalog.id, url, err)
    return None, err or "exhausted retries"


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
    cat_override = _mosaic_subtask_catalog_parallelism.get()
    cat_parallelism = max(
        1,
        int(cat_override if cat_override is not None else settings.mosaic_stac_catalog_parallelism or 1),
    )
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
