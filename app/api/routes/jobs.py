"""Job status for bulk import and tile/process jobs. Users see only their jobs; admins see all and owner."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_required
from app.core.html import html_response, wants_html
from app.core.permissions import can_see_collection
from app.crud import collections as collections_crud
from app.crud import user as user_crud
from app.db.session import get_db
from app.models.user import User
from app.services.bulk_finalize_queue import (
    clear_finalize_pending,
    remove_finalize_from_queue,
)
from app.services.bulk_queue import (
    get_bulk_import_storage_key,
    is_registered_bulk_import_job,
    remove_bulk_job_from_redis_queue,
    unregister_bulk_import_job,
)
from app.services.bulk_staging import drop_staging_table_sync
from app.services.bulk_storage import get_bulk_storage
from app.services.job_store import get_job, list_all_jobs, list_jobs_for_collection, update_job
from app.services.process_queue import get_process_job_meta
from app.services.tile_build_queue import clear_pending, get_latest_tile_build_job, update_tile_build_job
from app.utils.job_display import build_job_view_dict

router = APIRouter()

_ACTIVE_JOB_STATUSES = frozenset({"pending", "running", "replacing", "finalizing"})


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _can_see_job(job_owner_id: int | None, user: User) -> bool:
    """Only owner or admin can see a job. Legacy (owner_id None) only admin."""
    if job_owner_id is None:
        return user.is_admin
    return user.id == job_owner_id or user.is_admin


def _is_latest_tile_build_job(job_id: str, collection_id: str | None) -> bool:
    if not collection_id:
        return False
    try:
        latest = get_latest_tile_build_job(collection_id)
        return latest is not None and latest.job_id == job_id
    except Exception:
        return False


def cancel_job_record(job) -> dict:
    """
    Cancel a single job when allowed. Returns a result dict with cancelled=True/False.
    Does not raise HTTPException for non-cancellable jobs (returns cancelled=False instead).
    """
    job_id = job.job_id
    if job.status not in _ACTIVE_JOB_STATUSES:
        return {
            "job_id": job_id,
            "cancelled": False,
            "status": job.status,
            "message": f"Skipped: job is {job.status}.",
        }

    if _is_latest_tile_build_job(job_id, job.collection_id) and job.status in ("pending", "running"):
        clear_pending(job.collection_id)
        update_tile_build_job(job_id, status="cancelled", message="Cancelled by user.")
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Tile build cancelled.",
        }

    is_process = get_process_job_meta(job_id) is not None
    is_bulk = is_registered_bulk_import_job(job_id)

    if job.status == "pending" and is_bulk:
        sk = get_bulk_import_storage_key(job_id)
        remove_bulk_job_from_redis_queue(job_id)
        if sk:
            try:
                get_bulk_storage().delete(sk)
            except Exception:
                pass
        unregister_bulk_import_job(job_id)
        update_job(job_id, status="cancelled", message="Cancelled by user.")
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Bulk import cancelled before it started.",
        }

    if job.status == "pending":
        update_job(job_id, status="cancelled", message="Cancelled by user.")
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Job cancelled.",
        }

    if job.status in ("running", "replacing") and is_bulk:
        update_job(
            job_id,
            status="cancelled",
            message="Cancellation requested — stopping soon…",
        )
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Bulk import cancellation requested.",
        }

    if job.status == "finalizing" and is_bulk:
        remove_finalize_from_queue(job_id)
        clear_finalize_pending(job_id)
        try:
            from sqlalchemy import create_engine
            from app.core.config import get_settings

            engine = create_engine(get_settings().database_sync_url, pool_pre_ping=True, future=True)
            try:
                drop_staging_table_sync(engine, job_id)
            finally:
                engine.dispose()
        except Exception:
            pass
        unregister_bulk_import_job(job_id)
        update_job(job_id, status="cancelled", message="Cancelled during partition swap; staged data dropped.")
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Bulk finalize cancelled; staging table dropped.",
        }

    if job.status in ("running", "replacing") and is_process:
        update_job(
            job_id,
            status="cancelled",
            message="Cancellation requested — stopping soon…",
        )
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Process job cancellation requested.",
        }

    jl = getattr(job, "job_label", None) or ""
    if job.status == "running" and jl == "property_index":
        update_job(
            job_id,
            status="cancelled",
            message="Cancellation requested — stopping soon…",
        )
        return {
            "job_id": job_id,
            "cancelled": True,
            "status": "cancelled",
            "message": "Property index job cancellation requested.",
        }

    return {
        "job_id": job_id,
        "cancelled": False,
        "status": job.status,
        "message": "Job type or status cannot be cancelled from here.",
    }


def _build_job_dicts(jobs, include_owner_username: bool = False, owner_names: dict | None = None):
    out = []
    for j in jobs:
        meta = get_process_job_meta(j.job_id)
        latest = get_latest_tile_build_job(j.collection_id)
        is_tile_latest = latest is not None and latest.job_id == j.job_id
        d = build_job_view_dict(j, meta=meta, is_tile_build_latest=is_tile_latest)
        # Back-compat for older clients / scripts
        d["is_tile_build"] = d.get("job_category") == "tile_build"
        if meta:
            d["process_id"] = meta.get("process_id")
            d["collection_id_a"] = meta.get("collection_id_a")
            d["collection_id_b"] = meta.get("collection_id_b")
            d["collection_ids"] = meta.get("collection_ids")
            d["feature_source"] = meta.get("feature_source")
            if meta.get("result_collection_id"):
                d["result_collection_id"] = meta.get("result_collection_id")
        if include_owner_username and owner_names is not None and j.owner_id is not None:
            d["owner_username"] = owner_names.get(j.owner_id)
        elif include_owner_username and j.owner_id is None:
            d["owner_username"] = "(admin/legacy)"
        out.append(d)
    return out


@router.get(
    "",
    summary="List jobs",
    description="Returns recent jobs (own only; admins see all and owner). Without collection_id: all jobs. With collection_id: jobs for that collection only. Requires login.",
)
async def list_jobs(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user_required)],
    db: Annotated[AsyncSession, Depends(get_db)],
    collection_id: str | None = Query(None, description="Optional collection id to filter jobs. If omitted, returns all jobs."),
    limit: int = Query(50, ge=1, le=200, description="Max number of jobs to return"),
):
    owner_filter = None if current_user.is_admin else current_user.id
    if wants_html(request) and collection_id is None:
        all_jobs = list_all_jobs(limit=limit, owner_id=owner_filter)
        owner_ids = [j.owner_id for j in all_jobs if j.owner_id is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if (current_user.is_admin and owner_ids) else {}
        jobs_for_page = _build_job_dicts(all_jobs, include_owner_username=current_user.is_admin, owner_names=owner_names)
        base = _base_url(request)
        return html_response(
            "jobs_list.html",
            base=base,
            jobs=jobs_for_page,
            username=current_user.username,
            is_admin=current_user.is_admin,
        )
    if collection_id is not None:
        collection = await collections_crud.get_collection(db, collection_id)
        if not collection or not await can_see_collection(db, collection, current_user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
        jobs = list_jobs_for_collection(collection_id, limit=limit, owner_id=owner_filter)
        owner_ids = [j.owner_id for j in jobs if j.owner_id is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if (current_user.is_admin and owner_ids) else {}
        jobs_out = _build_job_dicts(jobs, include_owner_username=current_user.is_admin, owner_names=owner_names)
        return {"collection_id": collection_id, "jobs": [d for d in jobs_out]}
    all_jobs = list_all_jobs(limit=limit, owner_id=owner_filter)
    owner_ids = [j.owner_id for j in all_jobs if j.owner_id is not None]
    owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if current_user.is_admin else {}
    jobs_out = _build_job_dicts(
        all_jobs,
        include_owner_username=current_user.is_admin,
        owner_names=owner_names if current_user.is_admin else None,
    )
    return {"jobs": jobs_out}


@router.post(
    "/{job_id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel a queued or running job",
    description=(
        "Marks a job as cancelled. Pending jobs are removed from the queue. "
        "Running bulk imports and process jobs are cooperatively cancelled and cleaned up; "
        "tile builds use POST /collections/{id}/tiles/build/cancel."
    ),
)
async def cancel_job(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user_required)],
):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if not _can_see_job(job.owner_id, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = cancel_job_record(job)
    if not result.get("cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.get("message") or f"Job is {job.status}; cannot cancel.",
        )
    return {
        "job_id": job_id,
        "status": result.get("status", "cancelled"),
        "message": result.get("message", "Cancelled."),
    }


@router.post(
    "/cancel-active",
    status_code=status.HTTP_200_OK,
    summary="Cancel all active jobs",
    description=(
        "Cancels every job in pending, running, or replacing state visible to the caller "
        "(own jobs, or all jobs for admins). Bulk imports, process jobs, and tile builds are included."
    ),
)
async def cancel_active_jobs(
    current_user: Annotated[User, Depends(get_current_user_required)],
    limit: int = Query(200, ge=1, le=500, description="Max jobs to scan"),
):
    owner_filter = None if current_user.is_admin else current_user.id
    jobs = list_all_jobs(limit=limit, owner_id=owner_filter)
    results: list[dict] = []
    cancelled_count = 0
    skipped_count = 0
    seen_tile_collections: set[str] = set()

    for job in jobs:
        if job.status not in _ACTIVE_JOB_STATUSES:
            continue
        if not _can_see_job(job.owner_id, current_user):
            continue
        if _is_latest_tile_build_job(job.job_id, job.collection_id):
            cid = job.collection_id or ""
            if cid and cid in seen_tile_collections:
                skipped_count += 1
                results.append(
                    {
                        "job_id": job.job_id,
                        "cancelled": False,
                        "status": job.status,
                        "message": "Skipped: tile build for collection already cancelled.",
                    }
                )
                continue
            if cid:
                seen_tile_collections.add(cid)
        result = cancel_job_record(job)
        results.append(result)
        if result.get("cancelled"):
            cancelled_count += 1
        else:
            skipped_count += 1

    return {
        "cancelled_count": cancelled_count,
        "skipped_count": skipped_count,
        "scanned_active": len(results),
        "results": results,
        "message": (
            f"Cancelled {cancelled_count} job(s)."
            + (f" Skipped {skipped_count}." if skipped_count else "")
        ),
    }


@router.get(
    "/{job_id}",
    summary="Job status",
    description="Returns status of a job. Owner or admin only. Use ?f=html for a user-friendly status page.",
)
async def get_job_status(
    request: Request,
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user_required)],
):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if not _can_see_job(job.owner_id, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    meta = get_process_job_meta(job_id)
    try:
        latest = get_latest_tile_build_job(job.collection_id)
        is_tile_latest = latest is not None and latest.job_id == job.job_id
    except Exception:
        is_tile_latest = False
    job_view = build_job_view_dict(job, meta=meta, is_tile_build_latest=is_tile_latest)

    if wants_html(request):
        # Breadcrumb / primary collection: for feature-vs-layers, use initiator collection when known
        display_collection_id = job.collection_id
        if meta:
            fs = meta.get("feature_source") or ""
            if fs.startswith("reference "):
                part = fs[len("reference ") :].strip()
                if "/" in part:
                    display_collection_id = part.split("/", 1)[0]
        is_tile_build = job_view.get("job_category") == "tile_build"
        is_process_job = meta is not None
        base = _base_url(request)
        return html_response(
            "job.html",
            base=base,
            job_id=job.job_id,
            collection_id=display_collection_id,
            status=job.status,
            is_tile_build=is_tile_build,
            is_bulk_import=job_view.get("job_category") == "bulk_import",
            is_process_job=is_process_job,
            message=job.message or "",
            items_in=job.items_in,
            items_created=job.items_created,
            items_failed=job.items_failed,
            created_at=job.created_at.isoformat() + "Z",
            updated_at=job.updated_at.isoformat() + "Z",
            finished_at=job.finished_at.isoformat() + "Z" if job.finished_at else None,
            username=current_user.username,
            is_admin=current_user.is_admin,
            job_view=job_view,
            job_type_label=job_view.get("job_type_label", "Job"),
            job_category=job_view.get("job_category", ""),
            input_summary=job_view.get("input_summary", ""),
            details_json=json.dumps(job_view.get("details") or {}, indent=2, default=str),
        )
    # JSON API: full enriched record
    return job_view
