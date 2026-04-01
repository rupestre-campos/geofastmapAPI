"""Redis cache for federated STAC Item Search responses."""

from __future__ import annotations

import hashlib
import json

from app.core.config import get_settings

STAC_SEARCH_PREFIX = "geofastmap:stac_search:"


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def cache_key(body: dict, catalog_ids: list[str] | None) -> str:
    """Stable key from canonical JSON + catalog set."""
    payload = {
        "body": body,
        "catalog_ids": sorted(catalog_ids) if catalog_ids else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{STAC_SEARCH_PREFIX}{h}"


def get_cached(key: str) -> dict | None:
    ttl = get_settings().stac_search_cache_ttl_seconds
    if ttl <= 0:
        return None
    try:
        r = _redis()
        raw = r.get(key)
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
        r = _redis()
        r.setex(key, ttl, json.dumps(data, separators=(",", ":")))
    except Exception:
        pass
