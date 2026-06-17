"""In-process MVT encoding from GeoJSON. No subprocess—blazing fast for small result sets."""

from __future__ import annotations

import json
import math
from typing import Any

import mapbox_vector_tile
from shapely.geometry import box as shapely_box
from shapely.geometry import shape
from shapely.ops import transform

from app.utils.geo import mvt_layer_name
from app.utils.tile_bbox import tile_bbox_mercator

_EARTH_RADIUS = 6378137.0
_ORIGIN_SHIFT = math.pi * _EARTH_RADIUS
_MVT_EXTENT = 4096
# Buffer in "tile pixels" beyond the tile edge. This prevents visible seams/cracks
# on shared edges due to clipping + quantization rounding.
_MVT_BUFFER_PX = 256


def _wgs84_to_mercator(x: float, y: float, z: float | None = None) -> tuple[float, float]:
    """Transform WGS84 (lon, lat) to Web Mercator (x, y)."""
    lon, lat = x, y
    mx = lon * _ORIGIN_SHIFT / 180.0
    lat_rad = math.radians(lat)
    my = math.log(math.tan(math.pi / 4.0 + lat_rad / 2.0)) * _EARTH_RADIUS
    return (mx, my)


def _sanitize_properties(props: dict[str, Any]) -> dict[str, Any]:
    """MVT supports string, int, float, bool. Coerce for encoding."""
    out: dict[str, Any] = {}
    for k, v in props.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def encode_geojson_to_mvt(
    geojson_bytes: bytes,
    collection_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """
    Encode a GeoJSON FeatureCollection to a single MVT tile (z, x, y) in-process.
    No subprocess—typically &lt;10ms for &lt;200 features. Use this instead of tippecanoe
    for query-result / cached search tiles.
    """
    tile_merc = tile_bbox_mercator(z, x, y)
    tile_minx, tile_miny, tile_maxx, tile_maxy = tile_merc
    # Expand *clip* bounds to include a buffer outside the tile.
    # Important: keep quantize_bounds as the exact tile bounds.
    # If you expand quantize_bounds, each tile uses a slightly different affine
    # mapping and adjacent tiles can crack at seams. Tippecanoe/PostGIS buffer by
    # clipping to a buffered envelope but quantizing to the real tile envelope.
    tile_w = tile_maxx - tile_minx
    buf = tile_w * (_MVT_BUFFER_PX / _MVT_EXTENT)
    clip_minx = tile_minx - buf
    clip_miny = tile_miny - buf
    clip_maxx = tile_maxx + buf
    clip_maxy = tile_maxy + buf
    clip_box = shapely_box(clip_minx, clip_miny, clip_maxx, clip_maxy)

    data = json.loads(geojson_bytes.decode("utf-8"))
    features_in = data.get("features") or []

    mvt_features: list[dict[str, Any]] = []
    for f in features_in:
        geom_spec = f.get("geometry")
        if not geom_spec:
            continue
        try:
            shp = shape(geom_spec)
            if shp.is_empty:
                continue
            # WGS84 -> Web Mercator
            merc = transform(_wgs84_to_mercator, shp)
            if merc.is_empty:
                continue
            # Clip to buffered tile envelope (MVT buffer)
            clipped = merc.intersection(clip_box)
            if clipped.is_empty:
                continue
            props = _sanitize_properties(f.get("properties") or {})
            if f.get("id") is not None and "id" not in props:
                props["id"] = str(f["id"])
            # One MVT feature per geometry (explode GeometryCollection/Multi*)
            if clipped.geom_type == "GeometryCollection":
                for sub in clipped.geoms:
                    if sub.is_empty:
                        continue
                    mvt_features.append({"geometry": sub, "properties": props})
            else:
                mvt_features.append({"geometry": clipped, "properties": props})
        except Exception:
            continue

    if not mvt_features:
        return b""

    layer = {"name": mvt_layer_name(collection_id), "features": mvt_features}
    return mapbox_vector_tile.encode(
        [layer],
        default_options={
            "extents": _MVT_EXTENT,
            "quantize_bounds": (tile_minx, tile_miny, tile_maxx, tile_maxy),
        },
    )
