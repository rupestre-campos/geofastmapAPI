"""Short-lived Redis cache for dynamic MVT tiles (e.g. 1 minute TTL)."""

from __future__ import annotations

import hashlib

from app.core.config import get_settings

DYNAMIC_TILE_CACHE_PREFIX = "geofast:dynamic_tile:"
DYNAMIC_TILE_CACHE_PARAMS_PREFIX = "geofast:dynamic_tile_p:"


def _redis_bytes():
    """Redis client that returns bytes (for binary MVT)."""
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=False)


def _cache_key(collection_id: str, z: int, x: int, y: int) -> str:
    return f"{DYNAMIC_TILE_CACHE_PREFIX}{collection_id}:{z}:{x}:{y}"


def _params_cache_key(collection_id: str, z: int, x: int, y: int, params_key: str) -> str:
    """Cache key for tiles with query params (limit, offset, bbox, etc.). params_key is a short stable hash."""
    return f"{DYNAMIC_TILE_CACHE_PARAMS_PREFIX}{collection_id}:{z}:{x}:{y}:{params_key}"


def _params_key_from_query(ids: str | None, properties: str | None) -> str:
    """Stable short hash of query params for cache key (ids and properties only)."""
    raw = f"ids={ids or ''}&p={properties or ''}"
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
