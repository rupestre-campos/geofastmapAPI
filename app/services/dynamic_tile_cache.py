"""Short-lived Redis cache for dynamic MVT tiles (e.g. 1 minute TTL)."""

from __future__ import annotations

from app.core.config import get_settings

DYNAMIC_TILE_CACHE_PREFIX = "geofast:dynamic_tile:"


def _redis_bytes():
    """Redis client that returns bytes (for binary MVT)."""
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=False)


def _cache_key(collection_id: str, z: int, x: int, y: int) -> str:
    return f"{DYNAMIC_TILE_CACHE_PREFIX}{collection_id}:{z}:{x}:{y}"


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
