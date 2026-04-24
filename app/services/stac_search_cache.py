"""Redis cache for federated STAC Item Search and related upstream STAC HTTP responses."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.config import get_settings

STAC_SEARCH_PREFIX = "geofastmap:stac_search:"
STAC_COLLECTIONS_PREFIX = "geofastmap:stac_collections:v1:"
STAC_ITEM_PREFIX = "geofastmap:stac_item:v1:"

_redis_client: Any = None


def _redis_singleton():
    """Single pooled client — avoids new TCP + AUTH per cache read (was very heavy under load)."""
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
    return _redis_client


def cache_key(body: dict, catalog_ids: list[str] | None) -> str:
    """Stable key from canonical JSON + catalog set."""
    payload = {
        "body": body,
        "catalog_ids": sorted(catalog_ids) if catalog_ids else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{STAC_SEARCH_PREFIX}{h}"


def collections_cache_key(catalog_id: str) -> str:
    return f"{STAC_COLLECTIONS_PREFIX}{catalog_id}"


def stac_item_redis_cache_key(catalog_id: str, collection_id: str, item_id: str) -> str:
    return f"{STAC_ITEM_PREFIX}{catalog_id}:{collection_id}:{item_id}"


def cache_get_str(key: str) -> str | None:
    try:
        return _redis_singleton().get(key)
    except Exception:
        return None


def cache_set_str(key: str, value: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    try:
        _redis_singleton().setex(key, ttl_seconds, value)
    except Exception:
        pass


def get_cached(key: str) -> dict | None:
    ttl = get_settings().stac_search_cache_ttl_seconds
    if ttl <= 0:
        return None
    try:
        raw = _redis_singleton().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        return None


def set_cached(key: str, data: dict) -> None:
    ttl = get_settings().stac_search_cache_ttl_seconds
    if ttl <= 0:
        return
    try:
        _redis_singleton().setex(key, ttl, json.dumps(data, separators=(",", ":")))
    except Exception:
        pass
