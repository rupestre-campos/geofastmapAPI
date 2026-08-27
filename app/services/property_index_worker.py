"""Run property-index sync jobs with progress updates on the shared job store."""

from __future__ import annotations

from typing import Callable

from app.services.collection_property_indexes import (
    normalize_property_index_fields,
    sync_collection_property_indexes_sync,
)
from app.services.composite_property_indexes import sync_composite_property_indexes_to_members_sync
from app.services.job_store import get_job, update_job
from app.services.property_index_queue import PropertyIndexPayload


ProgressFn = Callable[[str], None]


def run_property_index_job_sync(payload: PropertyIndexPayload) -> None:
    """Execute CREATE/DROP index work; updates job status/messages for the Jobs UI."""
    job = get_job(payload.job_id)
    if job and job.status == "cancelled":
        return

    old_fields = normalize_property_index_fields(payload.old_fields)
    new_fields = normalize_property_index_fields(payload.new_fields)
    # Mirror sync_collection_property_indexes_sync: ensure all desired fields.
    to_ensure = list(new_fields)
    to_drop = [f for f in old_fields if f not in set(new_fields)]

    update_job(
        payload.job_id,
        status="running",
        message=(
            f"Ensuring property indexes on {payload.collection_id}: "
            f"ensure {len(to_ensure)}, drop {len(to_drop)}"
            + (" (composite → members)" if payload.is_composite else "")
        ),
        items_in=len(to_ensure) + len(to_drop),
        items_created=0,
        items_failed=0,
    )

    def on_progress(msg: str) -> None:
        j = get_job(payload.job_id)
        if j and j.status == "cancelled":
            raise RuntimeError("cancelled")
        update_job(payload.job_id, message=msg)

    try:
        if payload.is_composite:
            results = sync_composite_property_indexes_to_members_sync(
                payload.collection_id,
                payload.composite_members,
                old_fields,
                new_fields,
                on_progress=on_progress,
            )
            created = sum(len(r.get("created") or []) for r in results.values())
            dropped = sum(len(r.get("dropped") or []) for r in results.values())
            members_n = len(results)
            update_job(
                payload.job_id,
                status="completed",
                message=(
                    f"Property indexes synced on {members_n} member(s): "
                    f"ensured {created}, dropped {dropped}"
                ),
                items_created=created,
                items_failed=0,
            )
        else:
            result = sync_collection_property_indexes_sync(
                payload.collection_id,
                old_fields,
                new_fields,
                on_progress=on_progress,
            )
            created = len(result.get("created") or [])
            dropped = len(result.get("dropped") or [])
            update_job(
                payload.job_id,
                status="completed",
                message=f"Property indexes synced: ensured {created}, dropped {dropped}",
                items_created=created,
                items_failed=0,
            )
    except RuntimeError as e:
        if str(e) == "cancelled":
            update_job(payload.job_id, status="cancelled", message="Property index sync cancelled")
            return
        update_job(payload.job_id, status="failed", message=str(e))
        raise
    except Exception as e:
        update_job(payload.job_id, status="failed", message=f"Property index sync failed: {e}")
        raise
