"""SQLAlchemy WHERE clauses for feature property filters (shared by CRUD and sync bulk import)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Float, cast

from app.models.feature import Feature
from app.utils.property_filter import property_value_to_like_pattern
from app.utils.property_filters import PropertyFilter, PropertyOp


def property_filter_clause(key: str, value: str):
    """Build WHERE clause for one legacy attribute filter (exact or LIKE with *)."""
    prop_col = Feature.properties[key].astext
    pattern, use_like = property_value_to_like_pattern(value)
    if use_like and pattern is not None:
        return prop_col.isnot(None) & prop_col.like(pattern, escape="\\")
    return prop_col == value


def structured_filter_clause(f: PropertyFilter):
    """Build WHERE clause for one structured filter (key:op:value)."""
    prop_col = Feature.properties[f.key].astext
    value = f.value
    if f.op == PropertyOp.EQ:
        return prop_col == value
    if f.op == PropertyOp.NE:
        return prop_col != value
    if f.op == PropertyOp.LIKE:
        return prop_col.isnot(None) & prop_col.like(value, escape="\\")
    if f.op == PropertyOp.ILIKE:
        return prop_col.isnot(None) & prop_col.ilike(value, escape="\\")
    try:
        num_val = float(value)
    except ValueError:
        num_val = None
    if num_val is not None and f.op in (PropertyOp.GT, PropertyOp.GTE, PropertyOp.LT, PropertyOp.LTE):
        num_col = cast(prop_col, Float)
        if f.op == PropertyOp.GT:
            return num_col > num_val
        if f.op == PropertyOp.GTE:
            return num_col >= num_val
        if f.op == PropertyOp.LT:
            return num_col < num_val
        if f.op == PropertyOp.LTE:
            return num_col <= num_val
    if f.op == PropertyOp.GT:
        return prop_col > value
    if f.op == PropertyOp.GTE:
        return prop_col >= value
    if f.op == PropertyOp.LT:
        return prop_col < value
    if f.op == PropertyOp.LTE:
        return prop_col <= value
    return prop_col == value


def apply_structured_filters_to_stmt(stmt, filters: Sequence[PropertyFilter]):
    """AND structured filter clauses onto a SQLAlchemy statement."""
    for pf in filters:
        stmt = stmt.where(structured_filter_clause(pf))
    return stmt
