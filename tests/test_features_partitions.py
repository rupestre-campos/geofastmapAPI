"""Tests for app.db.features_partitions (unit tests, no DB)."""
import pytest

from app.db.features_partitions import _safe_partition_name


def test_safe_partition_name_basic():
    """Alphanumeric and underscore are kept; length and hash applied."""
    name = _safe_partition_name("my_collection")
    assert name.startswith("features_")
    assert "my_collection" in name or "my_collection" in name.replace("_", "")
    assert len(name) <= 63
    assert name.isascii() and all(c.isalnum() or c == "_" for c in name)


def test_safe_partition_name_sanitizes_special_chars():
    """Special characters are replaced with underscore."""
    name = _safe_partition_name("a-b.c d")
    assert " " not in name and "-" not in name and "." not in name
    assert name.startswith("features_")


def test_safe_partition_name_truncates_long_ids():
    """Long collection_id is truncated to 45 chars (before hash)."""
    long_id = "a" * 100
    name = _safe_partition_name(long_id)
    # features_ + up to 45 chars + _ + 8 hex
    assert len(name) <= 63
    assert name.startswith("features_")


def test_safe_partition_name_deterministic():
    """Same collection_id produces same partition name."""
    assert _safe_partition_name("x") == _safe_partition_name("x")


def test_safe_partition_name_different_ids_different_names():
    """Different collection_ids produce different partition names."""
    a = _safe_partition_name("coll_a")
    b = _safe_partition_name("coll_b")
    assert a != b
