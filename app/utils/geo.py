"""Convert between GeoJSON dict and GeoAlchemy2 / PostGIS geometry."""

from __future__ import annotations

from typing import Any

from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping, shape
from shapely import wkb


def geojson_to_wkt_element(geojson: dict[str, Any] | None, srid: int = 4326) -> WKTElement | None:
    """Convert a GeoJSON geometry dict to a WKTElement for storing in PostGIS."""
    if geojson is None:
        return None
    geom = shape(geojson)
    return WKTElement(geom.wkt, srid=srid)


def _flatten_coords(coords: Any, out: list[float]) -> None:
    """Flatten GeoJSON coordinates to a list of [x, y, ...] values (pairs for 2D)."""
    if not isinstance(coords, (list, tuple)):
        return
    if len(coords) >= 2 and isinstance(coords[0], (int, float)):
        out.extend(coords[:2])  # x, y
        return
    for item in coords:
        _flatten_coords(item, out)


def bbox_from_geometries(geometries: list[dict[str, Any] | None]) -> list[float] | None:
    """Compute GeoJSON bbox [minx, miny, maxx, maxy] from a list of GeoJSON geometry dicts.
    Returns None if no valid coordinates. Follows RFC 7946 (GeoJSON) bbox format."""
    xs: list[float] = []
    ys: list[float] = []
    for g in geometries:
        if not g or "coordinates" not in g:
            continue
        flat: list[float] = []
        _flatten_coords(g.get("coordinates"), flat)
        for i in range(0, len(flat), 2):
            if i + 1 < len(flat):
                xs.append(flat[i])
                ys.append(flat[i + 1])
    if not xs or not ys:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def geometry_to_geojson(geom: Any) -> dict[str, Any] | None:
    """Convert a GeoAlchemy2 geometry (WKBElement/WKTElement), raw WKB bytes, or hex WKB str to a GeoJSON geometry dict."""
    if geom is None:
        return None
    # Raw SQL (e.g. tile_builder with stream_results): PostGIS often returns hex-encoded WKB as str
    if isinstance(geom, str):
        shp = wkb.loads(geom, hex=True)
        return mapping(shp)
    # Binary WKB from driver
    if isinstance(geom, (bytes, memoryview, bytearray)):
        shp = wkb.loads(bytes(geom))
        return mapping(shp)
    try:
        shp = to_shape(geom)
        return mapping(shp)
    except TypeError:
        # Driver may return a bytes-like that isn't bytes/memoryview (e.g. buffer)
        shp = wkb.loads(bytes(geom))
        return mapping(shp)
