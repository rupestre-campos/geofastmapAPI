from app.api.routes import rasters as rasters_route


def test_normalize_dem_encoding_defaults_and_clamps():
    assert rasters_route._normalize_dem_encoding(None) == "terrainrgb"
    assert rasters_route._normalize_dem_encoding("terrainrgb") == "terrainrgb"
    assert rasters_route._normalize_dem_encoding("terrarium") == "terrarium"
    assert rasters_route._normalize_dem_encoding("invalid") == "terrainrgb"


def test_maplibre_encoding_mapping():
    assert rasters_route._maplibre_dem_encoding("terrainrgb") == "mapbox"
    assert rasters_route._maplibre_dem_encoding("terrarium") == "terrarium"


def test_collection_dem_settings_defaults_and_values():
    class _C:
        def __init__(self, raster_settings):
            self.raster_settings = raster_settings

    assert rasters_route._collection_dem_settings(_C(None)) == (False, "terrainrgb")
    assert rasters_route._collection_dem_settings(_C({})) == (False, "terrainrgb")
    assert rasters_route._collection_dem_settings(_C({"is_dem": True, "dem_encoding": "terrarium"})) == (
        True,
        "terrarium",
    )
