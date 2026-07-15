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


def test_create_index_sql_uses_concurrently():
    sql, params = _create_index_sql("car-area_imovel", "state_code")
    text_sql = str(sql)
    assert "CREATE INDEX CONCURRENTLY" in text_sql
    assert "IF NOT EXISTS" in text_sql
    assert params["cid"] == "car-area_imovel"
    assert params["field_key"] == "state_code"
