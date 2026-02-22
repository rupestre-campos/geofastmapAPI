"""Convert OGC-style property filter value to SQL LIKE pattern (for * partial match)."""


def property_value_to_like_pattern(value: str) -> tuple[str | None, bool]:
    """
    Convert filter value to PostgreSQL LIKE pattern.
    Returns (pattern, use_like): if use_like is False, use equality with value.
    - No *: exact match -> (value, False).
    - * at end: prefix match -> ('value%', True)
    - * at start: suffix match -> ('%value', True)
    - * both: contains -> ('%value%', True)
    Escape % and _ for literal use in LIKE.
    """
    if "*" not in value:
        return (value, False)
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = escaped.replace("*", "%")
    return (pattern, True)
