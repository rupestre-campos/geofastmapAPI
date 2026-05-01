"""Tests for raster COG conversion CRS handling (missing CRS, reproject)."""

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from app.services.coverages import convert_geotiff_to_cog_4326, parse_source_crs


def test_parse_source_crs_empty():
    assert parse_source_crs(None) is None
    assert parse_source_crs("") is None
    assert parse_source_crs("   ") is None


def test_parse_source_crs_epsg():
    c = parse_source_crs("EPSG:4326")
    assert c is not None
    assert c.to_epsg() == 4326


def test_parse_source_crs_invalid():
    with pytest.raises(ValueError, match="Invalid source_crs"):
        parse_source_crs("not-a-crs")


def test_missing_crs_assumed_wgs84_no_resample(tmp_path: Path):
    rows, cols = 4, 6
    data = np.ones((1, rows, cols), dtype=np.float32)
    transform = from_bounds(-45.0, -22.0, -43.5, -21.0, cols, rows)
    src_path = tmp_path / "in.tif"
    with rasterio.open(
        src_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=np.float32,
        crs=None,
        transform=transform,
    ) as dst:
        dst.write(data)
    dst_path = tmp_path / "out.tif"
    convert_geotiff_to_cog_4326(src_path, dst_path)
    with rasterio.open(dst_path) as out:
        assert out.crs.to_epsg() == 4326
        assert out.width == cols and out.height == rows


def test_embedded_epsg4326_rio_copy(tmp_path: Path):
    rows, cols = 3, 3
    data = np.zeros((1, rows, cols), dtype=np.float32)
    transform = from_bounds(-1.0, -1.0, 1.0, 1.0, cols, rows)
    src_path = tmp_path / "in4326.tif"
    with rasterio.open(
        src_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=np.float32,
        crs=CRS.from_epsg(4326),
        transform=transform,
    ) as dst:
        dst.write(data)
    dst_path = tmp_path / "cog4326.tif"
    convert_geotiff_to_cog_4326(src_path, dst_path)
    with rasterio.open(dst_path) as out:
        assert out.crs.to_epsg() == 4326


def test_missing_crs_with_explicit_source_reprojects(tmp_path: Path):
    rows, cols = 16, 16
    data = np.ones((1, rows, cols), dtype=np.float32)
    transform = from_bounds(-1000.0, -1000.0, 1000.0, 1000.0, cols, rows)
    src_path = tmp_path / "no_crs_merc.tif"
    with rasterio.open(
        src_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=np.float32,
        crs=None,
        transform=transform,
    ) as dst:
        dst.write(data)
    dst_path = tmp_path / "out_override.tif"
    convert_geotiff_to_cog_4326(src_path, dst_path, source_crs="EPSG:3857")
    with rasterio.open(dst_path) as out:
        assert out.crs.to_epsg() == 4326


def test_reproject_web_mercator_to_4326(tmp_path: Path):
    rows, cols = 16, 16
    data = np.random.rand(1, rows, cols).astype(np.float32)
    transform = from_bounds(-1000.0, -1000.0, 1000.0, 1000.0, cols, rows)
    src_path = tmp_path / "merc.tif"
    with rasterio.open(
        src_path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=np.float32,
        crs=CRS.from_epsg(3857),
        transform=transform,
    ) as dst:
        dst.write(data)
    dst_path = tmp_path / "out_cog.tif"
    convert_geotiff_to_cog_4326(src_path, dst_path)
    with rasterio.open(dst_path) as out:
        assert out.crs.to_epsg() == 4326
        assert out.width > 0 and out.height > 0
