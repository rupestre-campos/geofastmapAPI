"""Property index fan-out from composite collections to member vector layers."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Engine

from app.services.collection_property_indexes import (
    normalize_property_index_fields,
    sync_collection_property_indexes_sync,
)
from app.services.composite_collections import member_collection_ids, parse_composite_members


def sync_composite_property_indexes_to_members_sync(
    composite_id: str,
    composite_members: list | None,
    old_fields: list[str],
    new_fields: list[str],
    *,
    engine: Engine | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """
    Apply property index field changes to every member vector collection.
    Returns per-member sync results.
    """
    old_norm = normalize_property_index_fields(old_fields)
    new_norm = normalize_property_index_fields(new_fields)
    member_ids = member_collection_ids(parse_composite_members(composite_members))
    results: dict[str, dict[str, list[str]]] = {}
    total = len(member_ids) or 1
    for i, mid in enumerate(member_ids, start=1):
        if on_progress:
            on_progress(
                f"Composite {composite_id}: syncing member {i}/{total} ({mid})…"
            )

        def _member_progress(msg: str, _mid: str = mid) -> None:
            if on_progress:
                on_progress(f"[{_mid}] {msg}")

        results[mid] = sync_collection_property_indexes_sync(
            mid,
            old_norm,
            new_norm,
            engine=engine,
            on_progress=_member_progress if on_progress else None,
        )
    return results
