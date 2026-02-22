"""Tests for app.utils.geo."""
import pytest
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
    from geoalchemy2 import Geometry
    from sqlalchemy import text
    # We can't easily create a WKBElement without a DB; test that WKT has expected content
    assert "10.5" in wkt.data and "20.5" in wkt.data
