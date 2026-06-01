"""Tests for shared feature property filter SQL clauses."""

from app.db.feature_property_filters import structured_filter_clause
from app.utils.property_filters import PropertyFilter, PropertyOp


def test_structured_filter_eq_clause():
    f = PropertyFilter(key="state_id", op=PropertyOp.EQ, value="12")
    clause = structured_filter_clause(f)
    assert clause is not None
