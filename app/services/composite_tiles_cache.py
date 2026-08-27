"""Redis cache for merged composite static MVT tiles."""
from __future__ import annotations

from app.core.config import get_settings

COMPOSITE_TILE_CACHE_PREFIX = "geofastmap:composite_tile:"


def _redis_bytes():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=False)


def _cache_key(composite_id: str, z: int, x: int, y: int, revision: str) -> str:
    return f"{COMPOSITE_TILE_CACHE_PREFIX}{composite_id}:{revision}:{z}:{x}:{y}"


def get_composite_tile(
    composite_id: str,
    z: int,
    x: int,
    y: int,
    revision: str,
) -> bytes | None:
    settings = get_settings()
    ttl = int(getattr(settings, "composite_tiles_cache_ttl_seconds", 3600) or 0)
    if ttl <= 0:
        return None
    try:
        r = _redis_bytes()
        return r.get(_cache_key(composite_id, z, x, y, revision))
    except Exception:
        return None


def set_composite_tile(
    composite_id: str,
    z: int,
    x: int,
    y: int,
    revision: str,
    payload: bytes,
) -> None:
    settings = get_settings()
    ttl = int(getattr(settings, "composite_tiles_cache_ttl_seconds", 3600) or 0)
    if ttl <= 0:
        return
    try:
        r = _redis_bytes()
        r.set(_cache_key(composite_id, z, x, y, revision), payload, ex=ttl)
    except Exception:
        pass


def invalidate_composite_tiles_cache(composite_id: str) -> None:
    """Drop all cached merged tiles for a composite collection."""
    settings = get_settings()
    if int(getattr(settings, "composite_tiles_cache_ttl_seconds", 3600) or 0) <= 0:
        return
    try:
        r = _redis_bytes()
        pattern = f"{COMPOSITE_TILE_CACHE_PREFIX}{composite_id}:*"
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=200)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
