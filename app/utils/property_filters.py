"""Structured property filters for GET items: key, operator, value. Full-text search (q) is separate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List


class PropertyOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    LIKE = "like"
    ILIKE = "ilike"


@dataclass(frozen=True)
class PropertyFilter:
    key: str
    op: PropertyOp
    value: str


# Allowed operators in query string
OPS = {e.value for e in PropertyOp}


def safe_json_key(s: str) -> str:
    """Return a safe substring for use as JSON key in SQL (alphanumeric + underscore only)."""
    return "".join(c for c in (s or "") if c.isalnum() or c == "_")[:200]


def parse_filter_param(filters: list[str] | None) -> list[PropertyFilter]:
    """
    Parse repeated filter=key:op:value into PropertyFilter list.
    Value may contain colons; only first two colons split key:op:value.
    Invalid entries are skipped.
    """
    if not filters:
        return []
    out: list[PropertyFilter] = []
    for s in filters:
        s = (s or "").strip()
        if not s:
            continue
        parts = s.split(":", 2)  # max 2 splits so value can contain ":"
        if len(parts) != 3:
            continue
        key, op, value = parts[0].strip(), parts[1].strip().lower(), parts[2]
        if not key or op not in OPS:
            continue
        try:
            out.append(PropertyFilter(key=key, op=PropertyOp(op), value=value))
        except ValueError:
            continue
    return out
