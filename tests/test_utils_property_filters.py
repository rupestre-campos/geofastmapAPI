"""Tests for app.utils.property_filters."""
import pytest
from app.utils.property_filters import PropertyOp, PropertyFilter, parse_filter_param, OPS


def test_ops_contains_all_operators():
    assert "eq" in OPS
    assert "like" in OPS
    assert "ilike" in OPS
    assert "gte" in OPS


def test_parse_filter_single():
    out = parse_filter_param(["car_code:eq:GO-520"])
    assert len(out) == 1
    assert out[0].key == "car_code"
    assert out[0].op == PropertyOp.EQ
    assert out[0].value == "GO-520"


def test_parse_filter_value_with_colons():
    out = parse_filter_param(["key:eq:value:with:colons"])
    assert len(out) == 1
    assert out[0].value == "value:with:colons"


def test_parse_filter_multiple():
    out = parse_filter_param(["a:eq:1", "b:gte:2"])
    assert len(out) == 2
    assert out[0].key == "a" and out[0].value == "1"
    assert out[1].key == "b" and out[1].op == PropertyOp.GTE and out[1].value == "2"


def test_parse_filter_none_or_empty():
    assert parse_filter_param(None) == []
    assert parse_filter_param([]) == []


def test_parse_filter_skips_invalid():
    out = parse_filter_param(["good:eq:yes", "bad", "x:unknown:y", ":eq:val", "key::value"])
    assert len(out) == 1
    assert out[0].key == "good"


def test_parse_filter_case_insensitive_op():
    out = parse_filter_param(["x:EQ:y", "z:GTE:0"])
    assert out[0].op == PropertyOp.EQ
    assert out[1].op == PropertyOp.GTE


def test_parse_filter_skips_empty_strings():
    out = parse_filter_param(["", "  ", "a:eq:1"])
    assert len(out) == 1
    assert out[0].key == "a"


def test_parse_filter_skips_empty_key():
    out = parse_filter_param([":eq:val"])
    assert len(out) == 0
