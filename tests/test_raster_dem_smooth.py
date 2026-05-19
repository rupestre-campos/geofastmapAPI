"""Tests for DEM terrain smooth settings and Titiler param injection."""

from types import SimpleNamespace

from app.services.raster_dem_settings import (
    append_dem_terrain_smooth_titiler_params,
    dem_terrain_smooth_demv,
    dem_terrain_smooth_settings,
)


def test_dem_terrain_smooth_settings_defaults_for_dem_collection():
    c = SimpleNamespace(raster_settings={"is_dem": True})
    s = dem_terrain_smooth_settings(c)
    assert s["enabled"] is True
    assert s["min_zoom"] == 14
    assert s["resampling"] == "bilinear"
    assert s["maxzoom"] == 14


def test_dem_terrain_smooth_settings_parses_override():
    c = SimpleNamespace(
        raster_settings={
            "is_dem": True,
            "dem_terrain_smooth": {
                "enabled": True,
                "min_zoom": 15,
                "resampling": "cubic",
                "reproject": "cubic",
                "padding": 2,
                "maxzoom": 13,
            },
        }
    )
    s = dem_terrain_smooth_settings(c)
    assert s["min_zoom"] == 15
    assert s["resampling"] == "cubic"
    assert s["padding"] == 2
    assert s["maxzoom"] == 13


def test_dem_terrain_smooth_settings_disabled_for_non_dem():
    c = SimpleNamespace(raster_settings={})
    s = dem_terrain_smooth_settings(c, collection_is_dem=False)
    assert s["enabled"] is False


def test_dem_terrain_smooth_demv_changes_with_settings():
    a = dem_terrain_smooth_demv({"enabled": True, "min_zoom": 14, "resampling": "bilinear", "reproject": "bilinear", "padding": 1, "maxzoom": 14})
    b = dem_terrain_smooth_demv({"enabled": True, "min_zoom": 14, "resampling": "cubic", "reproject": "cubic", "padding": 1, "maxzoom": 14})
    assert a != b


def test_append_smooth_params_only_at_or_above_min_zoom():
    smooth = {"enabled": True, "min_zoom": 14, "resampling": "bilinear", "reproject": "bilinear", "padding": 1, "maxzoom": 14}
    params_low: list[tuple[str, str]] = []
    append_dem_terrain_smooth_titiler_params(
        params_low, z=13, kind="tiles", dem_request=True, smooth=smooth
    )
    assert params_low == []

    params_high: list[tuple[str, str]] = []
    append_dem_terrain_smooth_titiler_params(
        params_high, z=14, kind="tiles", dem_request=True, smooth=smooth
    )
    keys = dict(params_high)
    assert keys.get("resampling") == "bilinear"
    assert keys.get("reproject") == "bilinear"
    assert keys.get("padding") == "1"


def test_append_smooth_skips_point_reads():
    smooth = {"enabled": True, "min_zoom": 14, "resampling": "bilinear", "reproject": "bilinear", "padding": 1, "maxzoom": 14}
    params: list[tuple[str, str]] = []
    append_dem_terrain_smooth_titiler_params(
        params, z=16, kind="point", dem_request=True, smooth=smooth
    )
    assert params == []


def test_append_smooth_invalid_resampling_clamped():
    c = SimpleNamespace(
        raster_settings={
            "is_dem": True,
            "dem_terrain_smooth": {"resampling": "not-a-method"},
        }
    )
    s = dem_terrain_smooth_settings(c)
    assert s["resampling"] == "bilinear"
