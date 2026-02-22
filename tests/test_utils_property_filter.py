"""Tests for app.utils.property_filter (legacy LIKE pattern)."""
import pytest
from app.utils.property_filter import property_value_to_like_pattern


def test_exact_match():
    pattern, use_like = property_value_to_like_pattern("Alpha")
    assert use_like is False
    assert pattern == "Alpha"


def test_prefix_wildcard():
    pattern, use_like = property_value_to_like_pattern("Al*")
    assert use_like is True
    assert pattern == "Al%"


def test_suffix_wildcard():
    pattern, use_like = property_value_to_like_pattern("*pha")
    assert use_like is True
    assert pattern == "%pha"


def test_contains_wildcard():
    pattern, use_like = property_value_to_like_pattern("*lp*")
    assert use_like is True
    assert pattern == "%lp%"


def test_escape_percent_and_underscore():
    pattern, use_like = property_value_to_like_pattern("*50%*")
    assert use_like is True
    assert "\\%" in pattern

