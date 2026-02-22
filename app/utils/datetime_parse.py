"""Parse OGC API Features datetime query parameter (instant or range)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Tuple

# Supported: "2024-01-01", "2024-01-01T12:00:00", "2024-01-01T12:00:00Z", "2024-01-01/2024-12-31"


def parse_datetime_param(value: str) -> Tuple[datetime | None, datetime | None]:
    """
    Parse OGC datetime query value. Returns (start, end) for filtering created_at.
    - Instant: "2024-01-01" or "2024-01-01T12:00:00Z" -> (start, end) same moment (inclusive).
    - Range: "2024-01-01/2024-12-31" -> (start, end) inclusive.
    Returns (None, None) if parsing fails.
    """
    value = value.strip()
    if "/" in value:
        begin, end = value.split("/", 1)
        start = _parse_instant(begin.strip())
        end_dt = _parse_instant(end.strip())
        if start is None or end_dt is None:
            return (None, None)
        return (start, end_dt)
    start = _parse_instant(value)
    if start is None:
        return (None, None)
    return (start, start)


def _parse_instant(s: str) -> datetime | None:
    """Parse a single instant; return timezone-aware datetime."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None
