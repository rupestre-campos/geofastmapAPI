"""Convert between GeoJSON dict and GeoAlchemy2 / PostGIS geometry."""

from __future__ import annotations

from typing import Any

from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping, shape


def geojson_to_wkt_element(geojson: dict[str, Any] | None, srid: int = 4326) -> WKTElement | None:
    """Convert a GeoJSON geometry dict to a WKTElement for storing in PostGIS."""
    if geojson is None:
        return None
    geom = shape(geojson)
    return WKTElement(geom.wkt, srid=srid)


def geometry_to_geojson(geom: Any) -> dict[str, Any] | None:
    """Convert a GeoAlchemy2 geometry (e.g. WKBElement) to a GeoJSON geometry dict."""
    if geom is None:
        return None
    shp = to_shape(geom)
    return mapping(shp)
