"""Ensure search result GeoJSON is in Redis so tiler workers can read from cache (no DB)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dynamic_tile_cache import (
    get_search_result,
    set_search_result,
)
from app.services.dynamic_tile_geojson import get_search_result_geojson

# Single-flight: only one coroutine runs the DB query per (collection_id, params_key).
# Bounded so we don't grow memory; evict oldest when full.
_MAX_CACHE_LOCKS = 256
_cache_locks: OrderedDict[tuple[str, str], asyncio.Lock] = OrderedDict()
_cache_locks_guard = asyncio.Lock()


async def _get_or_create_lock(collection_id: str, params_key: str) -> asyncio.Lock:
    key = (collection_id, params_key)
    async with _cache_locks_guard:
        if key in _cache_locks:
            _cache_locks.move_to_end(key)
            return _cache_locks[key]
        while len(_cache_locks) >= _MAX_CACHE_LOCKS and _cache_locks:
            _cache_locks.popitem(last=False)
        lock = asyncio.Lock()
        _cache_locks[key] = lock
        return lock


async def ensure_search_result_cached(
    db: AsyncSession,
    collection_id: str,
    params_key: str,
    *,
    limit: int,
    offset: int = 0,
    sortby: str | None = None,
    sortdesc: bool = False,
    bbox: str | None = None,
    datetime_param: str | None = None,
    filter_param: list[str] | None = None,
    q: str | None = None,
    ids: str | None = None,
) -> bool:
    """
    If the search result for this param set is not in Redis, run the query once,
    store GeoJSON in Redis, and return True. If already cached, return True.
    Single-flight: concurrent requests for the same (collection_id, params_key)
    wait on one DB query instead of each running it (avoids thundering herd).
    """
    if get_search_result(collection_id, params_key) is not None:
        return True
    lock = await _get_or_create_lock(collection_id, params_key)
    async with lock:
        # Double-check after acquiring lock (another coroutine may have filled cache)
        if get_search_result(collection_id, params_key) is not None:
            return True
        bbox_tuple = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            if len(parts) == 4:
                try:
                    bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
                except ValueError:
                    pass
        ids_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
        try:
            geojson_bytes = await get_search_result_geojson(
                db,
                collection_id,
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
            set_search_result(collection_id, params_key, geojson_bytes)
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception(
                "ensure_search_result_cached failed: %s", e, extra={"collection_id": collection_id, "params_key": params_key}
            )
            return False
