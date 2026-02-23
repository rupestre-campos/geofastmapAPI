"""Process a single bulk import job (used by in-process consumer or standalone worker)."""

from __future__ import annotations

from app.services.bulk_import import run_bulk_import_sync
from app.services.bulk_queue import BulkJobPayload
from app.services.bulk_storage import get_bulk_storage
from app.services.job_store import update_job


def process_bulk_job(payload: BulkJobPayload) -> None:
    """Load file from storage, run import, update job status, delete file."""
    storage = get_bulk_storage()
    path = storage.get_path_or_uri(payload.storage_key)

    def on_progress(status: str, items_created: int, _total: int | None) -> None:
        update_job(payload.job_id, status=status, items_created=items_created)

    try:
        update_job(payload.job_id, status="running")
        created, failed, err = run_bulk_import_sync(
            path,
            payload.collection_id,
            payload.mode,
            payload.batch_size,
            on_progress=on_progress,
        )
        if err:
            update_job(
                payload.job_id,
                status="failed",
                message=err,
                items_created=created,
                items_failed=failed,
            )
        else:
            update_job(
                payload.job_id,
                status="completed",
                message=f"Imported {created} features." + (f" {failed} failed." if failed else ""),
                items_created=created,
                items_failed=failed,
            )
    except Exception as e:
        update_job(payload.job_id, status="failed", message=str(e))
    finally:
        # Always remove uploaded file after processing (success or failure)
        try:
            storage.delete(payload.storage_key)
        except Exception:
            pass
