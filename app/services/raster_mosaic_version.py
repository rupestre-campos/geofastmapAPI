"""Mosaic version id for raster collection tiles: content-addressed, changes when items are added/removed."""

from __future__ import annotations

import hashlib

# Long-lived HTTP cache is safe for tile URLs that include a matching mv (see rasters route).
MOSAIC_TILE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# When clients omit mv (legacy), avoid caching wrong pixels for a long time.
MOSAIC_TILE_CACHE_CONTROL_LEGACY = "public, max-age=300"


def compute_mosaic_version_id(collection_id: str, item_ids: list[str]) -> str | None:
    """
    Deterministic id for the set of raster feature ids in a collection.

    Uses the same join order as callers that query ``ORDER BY id`` (maps, list rasters, tiles).
    Any add/remove of a feature id changes the digest, so cache keys in ``?mv=`` invalidate.
    """
    if not item_ids:
        return None
    raw = f"{collection_id}:{','.join(item_ids)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mosaic_mv_matches_request(client_mv: str | None, expected_full: str) -> bool:
    """True if the client mv matches the server mosaic version (full hex or legacy 16-char prefix)."""
    if client_mv is None or not expected_full:
        return False
    if client_mv == expected_full:
        return True
    return len(client_mv) == 16 and client_mv == expected_full[:16]
