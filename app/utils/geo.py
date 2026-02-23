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
