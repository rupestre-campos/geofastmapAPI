"""Short-lived Redis cache for dynamic MVT tiles and search results (GeoJSON page)."""

from __future__ import annotations

import hashlib
import json

from app.core.config import get_settings

DYNAMIC_TILE_CACHE_PREFIX = "geofastmap:dynamic_tile:"
DYNAMIC_TILE_CACHE_PARAMS_PREFIX = "geofastmap:dynamic_tile_p:"
SEARCH_RESULT_PREFIX = "geofastmap:search_result:"
TILE_JOBS_QUEUE_KEY = "geofastmap:tile_jobs"


def _redis_bytes():
    """Redis client that returns bytes (for binary MVT)."""
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=False)


def _cache_key(collection_id: str, z: int, x: int, y: int) -> str:
    return f"{DYNAMIC_TILE_CACHE_PREFIX}{collection_id}:{z}:{x}:{y}"


def _params_cache_key(collection_id: str, z: int, x: int, y: int, params_key: str) -> str:
    """Cache key for tiles with query params (limit, offset, bbox, etc.). params_key is a short stable hash."""
    return f"{DYNAMIC_TILE_CACHE_PARAMS_PREFIX}{collection_id}:{z}:{x}:{y}:{params_key}"


def _params_key_from_query(
    limit: int | None = None,
    offset: int = 0,
    sortby: str | None = None,
    sortdesc: bool = False,
    bbox: str | None = None,
    datetime_param: str | None = None,
    filter_param: list[str] | None = None,
    q: str | None = None,
    ids: str | None = None,
    properties: str | None = None,
) -> str:
    """Stable short hash of query params for cache key (same as GET items + ids)."""
    parts = [
        f"l={limit}",
        f"o={offset}",
        f"s={sortby or ''}",
        f"d={sortdesc}",
        f"b={bbox or ''}",
        f"dt={datetime_param or ''}",
        f"f={','.join(filter_param or [])}",
        f"q={q or ''}",
        f"ids={ids or ''}",
        f"p={properties or ''}",
    ]
    raw = "&".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def get_tile(collection_id: str, z: int, x: int, y: int) -> bytes | None:
    """
    Return cached MVT bytes if present, else None.
    None means cache miss (or Redis unavailable); empty bytes are a valid cached tile.
    """
    settings = get_settings()
    if settings.tiles_dynamic_cache_ttl_seconds <= 0:
        return None
    try:
        r = _redis_bytes()
        key = _cache_key(collection_id, z, x, y)
        raw = r.get(key)
        return raw  # bytes or None
    except Exception:
        return None


def set_tile(collection_id: str, z: int, x: int, y: int, payload: bytes) -> None:
    """Store MVT bytes in Redis with configured TTL. No-op if TTL is 0 or Redis fails."""
    settings = get_settings()
    if settings.tiles_dynamic_cache_ttl_seconds <= 0:
        return
    try:
        r = _redis_bytes()
        key = _cache_key(collection_id, z, x, y)
        r.set(key, payload, ex=settings.tiles_dynamic_cache_ttl_seconds)
    except Exception:
        pass


def get_tile_with_params(
    collection_id: str, z: int, x: int, y: int, params_key: str
) -> bytes | None:
    """Return cached MVT for a parametrized request (limit, offset, bbox, etc.) if present."""
    settings = get_settings()
    if not getattr(settings, "tiles_dynamic_cache_with_params", True):
        return None
    if settings.tiles_dynamic_cache_params_ttl_seconds <= 0:
        return None
    try:
        r = _redis_bytes()
        key = _params_cache_key(collection_id, z, x, y, params_key)
        raw = r.get(key)
        return raw  # bytes or None
    except Exception:
        return None


def set_tile_with_params(
    collection_id: str, z: int, x: int, y: int, params_key: str, payload: bytes
) -> None:
    """Store MVT for a parametrized request in Redis. No-op if disabled or Redis fails."""
    settings = get_settings()
    if not getattr(settings, "tiles_dynamic_cache_with_params", True):
        return
    if settings.tiles_dynamic_cache_params_ttl_seconds <= 0:
        return
    try:
        r = _redis_bytes()
        key = _params_cache_key(collection_id, z, x, y, params_key)
        r.set(key, payload, ex=settings.tiles_dynamic_cache_params_ttl_seconds)
    except Exception:
        pass


def _search_result_key(collection_id: str, params_key: str) -> str:
    return f"{SEARCH_RESULT_PREFIX}{collection_id}:{params_key}"


def get_search_result(collection_id: str, params_key: str) -> bytes | None:
    """Return cached GeoJSON FeatureCollection bytes for this search (limit, offset, filters)."""
    settings = get_settings()
    ttl = getattr(settings, "tiles_search_result_cache_ttl_seconds", 300)
    if ttl <= 0:
        return None
    try:
        r = _redis_bytes()
        key = _search_result_key(collection_id, params_key)
        raw = r.get(key)
        return raw  # bytes or None
    except Exception:
        return None


def set_search_result(collection_id: str, params_key: str, payload: bytes) -> None:
    """Store GeoJSON FeatureCollection for this search so workers can build tiles without DB."""
    settings = get_settings()
    ttl = getattr(settings, "tiles_search_result_cache_ttl_seconds", 300)
    if ttl <= 0:
        return
    try:
        r = _redis_bytes()
        key = _search_result_key(collection_id, params_key)
        r.set(key, payload, ex=ttl)
    except Exception:
        pass


def push_tile_job(collection_id: str, params_key: str, z: int, x: int, y: int) -> None:
    """Enqueue a tile build job for workers. Payload: json dict with collection_id, params_key, z, x, y."""
    try:
        r = _redis_bytes()
        # Use decode_responses=True for RPUSH of string
        import redis
        r_str = redis.from_url(get_settings().redis_url, decode_responses=True)
        payload = json.dumps({"collection_id": collection_id, "params_key": params_key, "z": z, "x": x, "y": y})
        r_str.rpush(TILE_JOBS_QUEUE_KEY, payload)
    except Exception:
        pass


def pop_tile_job(timeout: int = 5) -> dict | None:
    """Block until a tile job is available (for workers). Returns dict with collection_id, params_key, z, x, y."""
    try:
        import redis
        r = redis.from_url(get_settings().redis_url, decode_responses=True)
        result = r.blpop(TILE_JOBS_QUEUE_KEY, timeout=timeout)
        if not result:
            return None
        _, payload = result
        return json.loads(payload)
    except Exception:
        return None


def invalidate_collection_cache(collection_id: str) -> None:
    """
    Delete all dynamic tile and search-result cache entries for this collection.
    Call after static tile build completes so clients do not see stale dynamic tiles.
    """
    try:
        r = _redis_bytes()
        # Redis client with decode_responses=False returns bytes keys; use string pattern for scan
        patterns = [
            f"{DYNAMIC_TILE_CACHE_PREFIX}{collection_id}:*",
            f"{DYNAMIC_TILE_CACHE_PARAMS_PREFIX}{collection_id}:*",
            f"{SEARCH_RESULT_PREFIX}{collection_id}:*",
        ]
        for pattern in patterns:
            for key in r.scan_iter(match=pattern):
                r.delete(key)
    except Exception:
        pass
