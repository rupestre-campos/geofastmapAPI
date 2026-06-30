"""Property index fan-out from composite collections to member vector layers."""
from __future__ import annotations

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
) -> dict[str, dict[str, list[str]]]:
    """
    Apply property index field changes to every member vector collection.
    Returns per-member sync results.
    """
    old_norm = normalize_property_index_fields(old_fields)
    new_norm = normalize_property_index_fields(new_fields)
    member_ids = member_collection_ids(parse_composite_members(composite_members))
    results: dict[str, dict[str, list[str]]] = {}
    for mid in member_ids:
        results[mid] = sync_collection_property_indexes_sync(
            mid,
            old_norm,
            new_norm,
            engine=engine,
        )
    return results
