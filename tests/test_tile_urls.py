"""Tile URL rewriting for map definitions."""

from app.utils.tile_urls import is_geofast_tile_url, rewrite_tiles_url_to_base


def test_is_geofast_tile_url():
    assert is_geofast_tile_url(
        "https://geofast.example.com/collections/c1/rasters/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    )
    assert is_geofast_tile_url(
        "https://geofast.example.com/raster-views/abc/titiler/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    )
    assert not is_geofast_tile_url("https://tile.openstreetmap.org/{z}/{x}/{y}.png")


def test_rewrite_tiles_url_to_base_changes_origin():
    src = (
        "https://geofast.example.com/collections/c1/rasters/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        "?mode=mosaic&mv=abc"
    )
    out = rewrite_tiles_url_to_base(src, "http://192.168.8.113:8000")
    assert out.startswith("http://192.168.8.113:8000/collections/c1/rasters/tiles/")
    assert "mode=mosaic" in out


def test_rewrite_tiles_url_to_base_keeps_external_tiles():
    src = "https://tile.openstreetmap.org/1/2/3.png"
    assert rewrite_tiles_url_to_base(src, "http://192.168.8.113:8000") == src
