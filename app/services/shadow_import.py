"""Shadow replace import: append tagged rows, keep prior data visible until finalize."""

from __future__ import annotations

from app.core.config import get_settings
from app.services.bulk_collection_activity import get_active_bulk_job_ids


def shadow_import_enabled() -> bool:
    return bool(getattr(get_settings(), "bulk_replace_shadow_import", False))


def active_shadow_exclude_job_ids(collection_id: str) -> list[str]:
    """Job ids whose in-flight rows should be hidden from items/tiles reads."""
    if not shadow_import_enabled():
        return []
    return get_active_bulk_job_ids(collection_id)


def shadow_read_where_sql(
    *,
    table_alias: str | None = None,
    param_name: str = "shadow_exclude_jobs",
) -> tuple[str, str]:
    """
    SQL fragment excluding in-flight shadow-import rows.
    Returns (clause, param_name) e.g. ``AND f.bulk_import_job_id IS NULL OR ...``.
    Caller must bind ``param_name`` to a list (use empty list to skip — returns empty clause).
    """
    prefix = f"{table_alias}." if table_alias else ""
    col = f"{prefix}bulk_import_job_id"
    return (
        f"AND ({col} IS NULL OR {col} != ALL(:{param_name}))",
        param_name,
    )


def shadow_distinct_on_order(sortdesc: bool, *, table_alias: str | None = None) -> str:
    """ORDER BY for DISTINCT ON (id): prefer stable (untagged) rows over in-flight import rows."""
    prefix = f"{table_alias}." if table_alias else ""
    id_dir = "DESC" if sortdesc else "ASC"
    part_dir = "DESC" if sortdesc else "ASC"
    return (
        f"id {id_dir}, ({prefix}bulk_import_job_id IS NULL) DESC, "
        f"{prefix}part_index {part_dir}"
    )
