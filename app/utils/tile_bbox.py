"""Web Mercator tile index (z, x, y) to WGS84 and EPSG:3857 bounding box."""

from __future__ import annotations

import math

# Web Mercator (EPSG:3857) earth radius in meters
_EARTH_RADIUS = 6378137.0
_ORIGIN_SHIFT = math.pi * _EARTH_RADIUS


def _lon_to_mercator_x(lon: float) -> float:
    return lon * _ORIGIN_SHIFT / 180.0


def _lat_to_mercator_y(lat: float) -> float:
    lat_rad = math.radians(lat)
    return math.log(math.tan(math.pi / 4.0 + lat_rad / 2.0)) * _EARTH_RADIUS


def tile_bbox_wgs84(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """
    Return (minx, miny, maxx, maxy) in WGS84 (lon, lat) for the given
    web mercator tile (z, x, y). XYZ / TMS-style: y=0 at top.
    """
    n = 1 << z
    minx = x / n * 360.0 - 180.0
    maxx = (x + 1) / n * 360.0 - 180.0

    def lat(y_frac: float) -> float:
        rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_frac)))
        return math.degrees(rad)

    maxy = lat(y / n)
    miny = lat((y + 1) / n)

    return (minx, miny, maxx, maxy)


def tile_bbox_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (minx, miny, maxx, maxy) in Web Mercator (EPSG:3857) for the tile.
    Standard bbox: miny = south (smaller mercator Y), maxy = north (larger mercator Y).
    """
    minx, miny, maxx, maxy = tile_bbox_wgs84(z, x, y)
    south_merc = _lat_to_mercator_y(miny)
    north_merc = _lat_to_mercator_y(maxy)
    return (
        _lon_to_mercator_x(minx),
        south_merc,
        _lon_to_mercator_x(maxx),
        north_merc,
    )
