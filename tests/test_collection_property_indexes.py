"""Tests for per-collection property index helpers."""

import pytest

from app.services.collection_property_indexes import (
    _create_index_sql,
    normalize_property_index_fields,
    property_index_name,
    validate_property_index_field,
)


def test_validate_property_index_field_ok():
    assert validate_property_index_field("state_code") == "state_code"


def test_validate_property_index_field_rejects_invalid():
    with pytest.raises(ValueError):
        validate_property_index_field("bad-key")


def test_normalize_dedupes():
    assert normalize_property_index_fields(["state_code", "state_code", "uf"]) == [
        "state_code",
        "uf",
    ]


def test_property_index_name_deterministic():
    a = property_index_name("car-area_imovel", "state_code")
    b = property_index_name("car-area_imovel", "state_code")
    c = property_index_name("car-area_imovel", "uf")
    assert a == b
    assert a != c
    assert a.startswith("idx_fp_")


def test_create_index_sql_uses_concurrently_on_leaf():
    sql, params = _create_index_sql(
        "features_car_restricted_use_sc_abcd1234",
        "car-restricted_use-sc",
        "car_code",
        include_collection_predicate=False,
    )
    text_sql = str(sql)
    assert "CREATE INDEX CONCURRENTLY" in text_sql
    assert "ON \"features_car_restricted_use_sc_abcd1234\"" in text_sql
    assert "ON features " not in text_sql  # never the partitioned parent
    assert "collection_id" not in text_sql
    assert params["field_key"] == "car_code"


def test_create_index_sql_default_partition_keeps_cid():
    sql, params = _create_index_sql(
        "features_default",
        "car-restricted_use-sc",
        "car_code",
        include_collection_predicate=True,
    )
    text_sql = str(sql)
    assert "ON \"features_default\"" in text_sql
    assert "collection_id = :cid" in text_sql
    assert params["cid"] == "car-restricted_use-sc"
