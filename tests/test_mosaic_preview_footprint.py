"""Tests for UI footprint refinement from preview images."""

import io

import numpy as np
from PIL import Image
from shapely.geometry import shape

from app.services.mosaic_preview_footprint import (
    _simplified_extrema_quad_mapping,
    footprint_display_geojson_from_bytes,
    footprint_display_geojson_from_rgba,
)


def _rgba_with_border(w: int, h: int, border_px: int) -> Image.Image:
    """White background (made transparent by mask), colored square inset = content."""
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :] = (255, 255, 255, 255)
    arr[border_px : h - border_px, border_px : w - border_px] = (80, 120, 200, 255)
    return Image.fromarray(arr, "RGBA")


def test_footprint_display_maps_fractions_to_bbox():
    im = _rgba_with_border(100, 100, 20)
    bbox = [-10.0, -20.0, 10.0, 20.0]  # west, south, east, north
    geo = footprint_display_geojson_from_rgba(im, bbox)
    assert geo is not None
    poly = shape(geo)
    b = poly.bounds
    # Content is central 60% of rows/cols → ~60% of lon/lat span
    assert b[0] > -10.0 and b[2] < 10.0
    assert b[1] > -20.0 and b[3] < 20.0
    assert (b[2] - b[0]) < 19.0
    assert (b[3] - b[1]) < 38.0


def test_footprint_display_from_png_bytes():
    im = _rgba_with_border(40, 40, 8)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    geo = footprint_display_geojson_from_bytes([-1.0, -1.0, 1.0, 1.0], buf.getvalue())
    assert geo is not None
    poly = shape(geo)
    assert not poly.is_empty


def test_footprint_display_all_masked_returns_none():
    im = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
    assert footprint_display_geojson_from_rgba(im, [0, 0, 1, 1]) is None


def test_footprint_ignores_small_corner_blob():
    """Largest connected component drops a tiny bright patch; flat-bright cloud pixels masked."""
    w, h = 80, 80
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :] = (255, 255, 255, 255)
    # Main scene (saturated blue, not flat-bright mask)
    arr[15:65, 15:65] = (40, 80, 200, 255)
    # Small corner "cloud" — flat bright white, should not expand footprint
    arr[2:10, 2:10] = (250, 248, 252, 255)
    im = Image.fromarray(arr, "RGBA")
    geo = footprint_display_geojson_from_rgba(im, [-10.0, -10.0, 10.0, 10.0])
    assert geo is not None
    poly = shape(geo)
    # Hull should stay near main block, not full image
    b = poly.bounds
    assert (b[2] - b[0]) < 18.0
    assert (b[3] - b[1]) < 18.0


def test_footprint_fills_interior_hole():
    """Interior nodata (transparent) inside scene is filled — hull covers outer extent."""
    w, h = 100, 100
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :] = (255, 255, 255, 255)
    # Ring: data only in frame, hole in middle (still white border outside ring)
    arr[10:90, 10:90] = (50, 100, 180, 255)
    arr[40:60, 40:60] = (255, 255, 255, 255)
    im = Image.fromarray(arr, "RGBA")
    geo = footprint_display_geojson_from_rgba(im, [0.0, 0.0, 1.0, 1.0])
    assert geo is not None
    poly = shape(geo)
    b = poly.bounds
    # Filled hole → hull spans most of the ring's outer square (~0.8 in x/y)
    assert (b[2] - b[0]) > 0.65
    assert (b[3] - b[1]) > 0.65
    assert poly.geom_type == "Polygon"
    assert len(poly.interiors) == 0


def test_footprint_is_simplified_to_extrema_quad():
    """Footprint output is a simplified 4-point polygon (plus closing coordinate)."""
    w, h = 120, 120
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :] = (255, 255, 255, 255)
    # Irregular non-rectangular blob.
    arr[20:95, 25:90] = (70, 120, 190, 255)
    arr[30:80, 10:25] = (70, 120, 190, 255)
    arr[65:105, 90:108] = (70, 120, 190, 255)
    im = Image.fromarray(arr, "RGBA")
    geo = footprint_display_geojson_from_rgba(im, [0.0, 0.0, 1.0, 1.0])
    assert geo is not None
    poly = shape(geo)
    coords = list(poly.exterior.coords)
    # 4 points + closing point
    assert len(coords) <= 5
    assert len(poly.interiors) == 0


def test_extrema_quad_finds_upper_left_when_not_min_x():
    """
    Top-left may not be on absolute min-x edge for slanted footprints.
    We still want the upper-left-most corner from polygon vertices.
    """
    poly = shape(
        {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0],   # lower-left
                [-1.0, 2.0],  # far-left mid (not top-left)
                [0.2, 4.0],   # actual upper-left-ish
                [4.0, 4.2],   # upper-right
                [4.5, 0.3],   # lower-right
                [0.0, 0.0],
            ]],
        }
    )
    geo = _simplified_extrema_quad_mapping(poly)
    assert geo is not None
    out = shape(geo)
    coords = list(out.exterior.coords)[:-1]
    assert len(coords) == 4
    # UL must be close to the highest-left candidate, not the far-left mid point.
    ul = coords[1]
    assert ul[1] >= 3.5
