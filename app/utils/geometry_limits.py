"""Limits on feature geometry size (WKB bytes) for API and bulk import."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings


class GeometryTooLargeError(Exception):
    """Raised when a geometry's WKB serialization exceeds the configured maximum."""

    def __init__(self, wkb_bytes: int, max_bytes: int) -> None:
        self.wkb_bytes = wkb_bytes
        self.max_bytes = max_bytes
        mb = max_bytes / (1024 * 1024)
        super().__init__(
            f"Geometry exceeds maximum size ({mb:.0f} MiB): WKB is {wkb_bytes} bytes, limit is {max_bytes} bytes."
        )


def geometry_wkb_byte_length(geom: Any) -> int:
    """OGC Well-Known Binary length in bytes (same representation PostGIS stores)."""
    if geom is None or getattr(geom, "is_empty", True):
        return 0
    return len(geom.wkb)


def geometry_exceeds_limit(geom: Any, max_bytes: int | None = None) -> bool:
    """True if geom is non-empty and WKB exceeds limit. Disabled limit (<=0) always returns False."""
    if geom is None or getattr(geom, "is_empty", True):
        return False
    max_b = get_settings().features_max_geometry_bytes if max_bytes is None else max_bytes
    if max_b <= 0:
        return False
    return geometry_wkb_byte_length(geom) > max_b


def check_geometry_size_limit(geom: Any, max_bytes: int | None = None) -> None:
    """
    Raise GeometryTooLargeError if geom's WKB exceeds the limit.
    Empty or None geometries are allowed. max_bytes defaults to settings; use 0 to disable.
    """
    if geom is None or getattr(geom, "is_empty", True):
        return
    max_b = get_settings().features_max_geometry_bytes if max_bytes is None else max_bytes
    if max_b <= 0:
        return
    n = geometry_wkb_byte_length(geom)
    if n > max_b:
        raise GeometryTooLargeError(n, max_b)
