"""Tests for app.utils.datetime_parse."""
import pytest
from datetime import datetime, timezone

from app.utils.datetime_parse import parse_datetime_param


def test_parse_instant_date_only():
    start, end = parse_datetime_param("2024-01-15")
    assert start == end
    assert start.year == 2024 and start.month == 1 and start.day == 15
    assert start.tzinfo == timezone.utc


def test_parse_instant_with_time():
    start, end = parse_datetime_param("2024-01-15T12:30:00")
    assert start == end
    assert start.hour == 12 and start.minute == 30


def test_parse_instant_with_z():
    start, end = parse_datetime_param("2024-01-15T12:00:00Z")
    assert start.tzinfo is not None


def test_parse_range():
    start, end = parse_datetime_param("2024-01-01/2024-12-31")
    assert start is not None and end is not None
    assert start < end
    assert start.year == 2024 and start.month == 1
    assert end.year == 2024 and end.month == 12


def test_parse_range_with_whitespace():
    start, end = parse_datetime_param("  2024-06-01  /  2024-06-30  ")
    assert start is not None and end is not None
    assert start.month == 6 and end.month == 6


def test_parse_invalid_returns_none_none():
    assert parse_datetime_param("not-a-date") == (None, None)
    assert parse_datetime_param("") == (None, None)
    assert parse_datetime_param("   ") == (None, None)


def test_parse_range_invalid_end_returns_none_none():
    assert parse_datetime_param("2024-01-01/invalid") == (None, None)
    assert parse_datetime_param("invalid/2024-12-31") == (None, None)


def test_parse_with_microseconds():
    start, end = parse_datetime_param("2024-01-01T00:00:00.123456Z")
    assert start is not None
    assert start.microsecond == 123456
