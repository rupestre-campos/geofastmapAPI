"""Redis cache for proxied Titiler PNG/JPEG tiles (same URL → fast repeat loads)."""

from __future__ import annotations

import hashlib
import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)

PREFIX = "geofastmap:titiler_tile:v1:"
_client = None


def _redis():
    global _client
    if _client is None:
        import redis

        _client = redis.from_url(get_settings().redis_url, decode_responses=False)
    return _client


def _pack(content_type: str, body: bytes) -> bytes:
    ctb = content_type.encode("utf-8")
    if len(ctb) > 65535:
        ctb = ctb[:65535]
    return len(ctb).to_bytes(2, "big") + ctb + body


def _unpack(raw: bytes) -> tuple[str, bytes] | None:
    if len(raw) < 2:
        return None
    n = int.from_bytes(raw[:2], "big")
    if len(raw) < 2 + n:
        return None
    ct = raw[2 : 2 + n].decode("utf-8", errors="replace")
    body = raw[2 + n :]
    return ct, body


def cache_key_for_titiler_request(
    forward_path: str,
    param_pairs: list[tuple[str, str]],
    key_extra: str | None = None,
) -> str:
    """
    Stable key for a Titiler GET: path + exact query pairs (order matters for repeated `assets`).
    Optional key_extra scopes the key when the same URL can map to different bytes (e.g. mosaic revision).
    """
    lines = [forward_path, *[f"{k}\t{v}" for k, v in param_pairs]]
    if key_extra:
        lines.append(f"extra\t{key_extra}")
    raw = "\n".join(lines).encode("utf-8")
    h = hashlib.sha256(raw).hexdigest()
    return f"{PREFIX}{h}"


def get_cached_tile(cache_key: str) -> tuple[bytes, str] | None:
    """Return (body, content-type) or None on miss / disabled / error."""
    settings = get_settings()
    ttl = settings.titiler_tile_cache_ttl_seconds
    if ttl <= 0:
        return None
    try:
        r = _redis()
        blob = r.get(cache_key)
        if not blob:
            return None
        unpacked = _unpack(blob)
        if unpacked is None:
            return None
        ct, body = unpacked
        return body, ct
    except Exception as e:
        logger.debug("titiler tile cache get failed: %s", e)
        return None


def set_cached_tile(cache_key: str, body: bytes, content_type: str) -> None:
    settings = get_settings()
    ttl = settings.titiler_tile_cache_ttl_seconds
    max_b = settings.titiler_tile_cache_max_body_bytes
    if ttl <= 0 or not body:
        return
    if len(body) > max_b:
        return
    if not (content_type.startswith("image/") or content_type == "application/octet-stream"):
        return
    try:
        blob = _pack(content_type or "image/png", body)
        _redis().setex(cache_key, ttl, blob)
    except Exception as e:
        logger.debug("titiler tile cache set failed: %s", e)
