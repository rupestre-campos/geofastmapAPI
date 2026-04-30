from app.api.routes import rasters as rasters_route


def test_normalize_dem_encoding_defaults_and_clamps():
    assert rasters_route._normalize_dem_encoding(None) == "terrainrgb"
    assert rasters_route._normalize_dem_encoding("terrainrgb") == "terrainrgb"
    assert rasters_route._normalize_dem_encoding("terrarium") == "terrarium"
    assert rasters_route._normalize_dem_encoding("invalid") == "terrainrgb"


def test_maplibre_encoding_mapping():
    assert rasters_route._maplibre_dem_encoding("terrainrgb") == "mapbox"
    assert rasters_route._maplibre_dem_encoding("terrarium") == "terrarium"
