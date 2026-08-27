"""Helpers for GeoFastMap tile URL templates (absolute URLs in map definitions)."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

_GEOFAST_TILE_PATH_MARKERS = (
    "/titiler/tiles/",
    "/rasters/tiles/",
    "/tiles/dynamic/",
    "/tiles/static/",
    "/stac/",
)


def is_geofast_tile_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        path = urlparse(url).path or ""
    except Exception:
        return False
    return any(marker in path for marker in _GEOFAST_TILE_PATH_MARKERS)


def rewrite_tiles_url_to_base(tiles_url: str, base: str) -> str:
    """Rewrite a same-service tile URL to the current request base (IP vs domain, http vs https)."""
    if not tiles_url or not base or not is_geofast_tile_url(tiles_url):
        return tiles_url
    try:
        parsed = urlparse(tiles_url.strip())
        base_parsed = urlparse(base.rstrip("/") + "/")
        rewritten = parsed._replace(scheme=base_parsed.scheme, netloc=base_parsed.netloc)
        return urlunparse(rewritten)
    except Exception:
        return tiles_url
