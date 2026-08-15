import pytest

from app.crud.features import exact_search_property_keys, is_exact_search_token
from tests.fake_db import FakeFeaturesCrud, Store


def test_car_code_is_exact_search_token():
    assert is_exact_search_token("MG-3114600-1598E0BC6C984CA9B36220D2041B2C87")
    assert not is_exact_search_token("OTHER")
    assert not is_exact_search_token("foo bar")


def test_exact_search_keys_always_include_car_aliases():
    keys = exact_search_property_keys(["municipio"])
    assert keys[0] == "municipio"
    assert "cod_imovel" in keys
    assert "car_code" in keys
    assert "COD_IMOVEL" in keys


@pytest.mark.asyncio
async def test_fake_exact_search_finds_cod_imovel():
    from datetime import datetime, timezone

    from app.models.feature import Feature
    from app.utils.geo import geojson_to_wkt_element

    now = datetime.now(timezone.utc)
    store = Store()
    code = "MG-3114600-1598E0BC6C984CA9B36220D2041B2C87"
    store.features[("car_mg", "feat-1")] = Feature(
        id="feat-1",
        collection_id="car_mg",
        part_index=0,
        geometry=geojson_to_wkt_element({"type": "Point", "coordinates": [0, 0]}),
        properties={"cod_imovel": code},
        created_at=now,
        updated_at=now,
    )
    crud = FakeFeaturesCrud(store)
    ids = await crud.resolve_exact_search_feature_ids(None, "car_mg", code)
    assert ids == ["feat-1"]


def test_car_code_is_exact_search_token():
    assert is_exact_search_token("MG-3114600-1598E0BC6C984CA9B36220D2041B2C87")
    assert not is_exact_search_token("OTHER")
    assert not is_exact_search_token("foo bar")


def test_exact_search_keys_always_include_car_aliases():
    keys = exact_search_property_keys(["municipio"])
    assert keys[0] == "municipio"
    assert "cod_imovel" in keys
    assert "car_code" in keys
    assert "COD_IMOVEL" in keys
