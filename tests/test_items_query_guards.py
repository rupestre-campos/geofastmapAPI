"""Items list query timeout helpers."""

from app.services.items_query_guards import is_items_query_timeout_error


def test_is_items_query_timeout_error_lock():
    class E(Exception):
        pass

    exc = E("canceling statement due to lock timeout")
    assert is_items_query_timeout_error(exc) is True


def test_is_items_query_timeout_error_statement():
    assert is_items_query_timeout_error(Exception("canceling statement due to statement timeout")) is True


def test_is_items_query_timeout_error_other():
    assert is_items_query_timeout_error(Exception("connection refused")) is False
