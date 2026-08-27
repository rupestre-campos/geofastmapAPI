"""Tests for app.utils.geo."""
from unittest.mock import patch

from shapely import wkb
from shapely.geometry import shape

from app.utils.geo import geojson_to_wkt_element, geometry_to_geojson
from geoalchemy2.elements import WKTElement


def test_geojson_to_wkt_element_point():
    geojson = {"type": "Point", "coordinates": [1.0, 2.0]}
    wkt = geojson_to_wkt_element(geojson)
    assert wkt is not None
    assert isinstance(wkt, WKTElement)
    assert "POINT" in wkt.data.upper()
    assert wkt.srid == 4326


def test_geojson_to_wkt_element_none():
    assert geojson_to_wkt_element(None) is None


def test_geojson_to_wkt_element_srid():
    geojson = {"type": "Point", "coordinates": [0, 0]}
    wkt = geojson_to_wkt_element(geojson, srid=3857)
    assert wkt.srid == 3857


def test_geometry_to_geojson_none():
    assert geometry_to_geojson(None) is None


def test_geometry_to_geojson_roundtrip():
    geojson = {"type": "Point", "coordinates": [10.5, 20.5]}
    wkt = geojson_to_wkt_element(geojson)
    # We can't easily create a WKBElement without a DB; test that WKT has expected content
    assert "10.5" in wkt.data and "20.5" in wkt.data


def test_geometry_to_geojson_hex_wkb_str():
    """geometry_to_geojson accepts hex-encoded WKB string (e.g. from PostGIS)."""
    geojson = {"type": "Point", "coordinates": [1.0, 2.0]}
    shp = shape(geojson)
    hex_wkb = wkb.dumps(shp, hex=True)
    assert isinstance(hex_wkb, str)
    out = geometry_to_geojson(hex_wkb)
    assert out is not None
    assert out["type"] == "Point"
    assert list(out["coordinates"]) == [1.0, 2.0]


def test_geometry_to_geojson_raw_wkb_bytes():
    """geometry_to_geojson accepts raw WKB bytes (e.g. from driver)."""
    geojson = {"type": "Point", "coordinates": [3.0, 4.0]}
    shp = shape(geojson)
    raw_wkb = wkb.dumps(shp)
    assert isinstance(raw_wkb, bytes)
    out = geometry_to_geojson(raw_wkb)
    assert out is not None
    assert out["type"] == "Point"
    assert list(out["coordinates"]) == [3.0, 4.0]


def test_geometry_to_geojson_bytearray():
    """geometry_to_geojson accepts bytearray WKB."""
    geojson = {"type": "Point", "coordinates": [5.0, 6.0]}
    shp = shape(geojson)
    raw = bytearray(wkb.dumps(shp))
    out = geometry_to_geojson(raw)
    assert out is not None
    assert out["type"] == "Point"
    assert list(out["coordinates"]) == [5.0, 6.0]


def test_shapely_to_api_geojson_homogenizes_polygon_collection():
    from shapely.geometry import GeometryCollection, Polygon

    from app.utils.geo import shapely_to_api_geojson

    a = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    b = Polygon([(2, 2), (3, 2), (3, 3), (2, 3), (2, 2)])
    out = shapely_to_api_geojson(GeometryCollection([a, b]))
    assert out is not None
    assert out["type"] == "MultiPolygon"
    assert "coordinates" in out


def test_feature_to_read_geometry_collection():
    from datetime import datetime, timezone

    from app.api.routes.items import _feature_to_read
    from app.models.feature import Feature

    now = datetime.now(timezone.utc)
    f = Feature(
        id="gc-1",
        collection_id="gc_items",
        part_index=0,
        geometry=None,
        properties={"name": "gc"},
        created_at=now,
        updated_at=now,
    )
    f.geometry_geojson = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            {"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]]},
        ],
    }
    read = _feature_to_read(f)
    assert read.geometry is not None
    assert read.geometry.type == "MultiPolygon"
    dumped = read.geometry.model_dump()
    assert "coordinates" in dumped
    assert dumped.get("geometries") is None


def test_geometry_schema_accepts_geometry_collection():
    from app.schemas.feature import Geometry

    g = Geometry.model_validate(
        {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "Point", "coordinates": [-44.15, -21.43]},
            ],
        }
    )
    assert g.type == "GeometryCollection"
    assert g.geometries
    assert "coordinates" not in g.model_dump()


def test_geometry_to_geojson_typeerror_fallback():
    """When to_shape raises TypeError, fallback uses bytes(geom) and wkb.loads."""
    geojson = {"type": "Point", "coordinates": [7.0, 8.0]}
    shp = shape(geojson)
    wkb_bytes = wkb.dumps(shp)

    class BytesLike:
        __bytes__ = lambda self: wkb_bytes  # noqa: E731

    with patch("app.utils.geo.to_shape", side_effect=TypeError("not a geo type")):
        out = geometry_to_geojson(BytesLike())
    assert out is not None
    assert out["type"] == "Point"
    assert list(out["coordinates"]) == [7.0, 8.0]
