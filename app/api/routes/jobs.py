"""Job status for bulk import and tile/process jobs. Users see only their jobs; admins see all and owner."""

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
from app.services.job_store import get_job, list_all_jobs, list_jobs_for_collection, update_job
from app.services.tile_build_queue import get_latest_tile_build_job

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _can_see_job(job_owner_id: int | None, user: User) -> bool:
    """Only owner or admin can see a job. Legacy (owner_id None) only admin."""
    if job_owner_id is None:
        return user.is_admin
    return user.id == job_owner_id or user.is_admin


def _build_job_dicts(jobs, include_owner_username: bool = False, owner_names: dict | None = None):
    from app.services.process_queue import get_process_job_meta

    out = []
    for j in jobs:
        d = j.to_dict()
        meta = get_process_job_meta(j.job_id)
        if meta:
            d["process_id"] = meta.get("process_id")
            d["collection_id_a"] = meta.get("collection_id_a")
            d["collection_id_b"] = meta.get("collection_id_b")
            d["collection_ids"] = meta.get("collection_ids")
            d["feature_source"] = meta.get("feature_source")
            d["result_collection_id"] = meta.get("result_collection_id") or d.get("result_collection_id")
            d["is_tile_build"] = False
        else:
            latest = get_latest_tile_build_job(j.collection_id)
            d["is_tile_build"] = latest is not None and latest.job_id == j.job_id
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
    summary="Cancel a queued job",
    description="Marks a job as cancelled. Only jobs with status 'pending' can be cancelled. Owner or admin only.",
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
    if job.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is {job.status}; only pending (queued) jobs can be cancelled.",
        )
    update_job(job_id, status="cancelled", message="Cancelled by user.")
    return {"job_id": job_id, "status": "cancelled", "message": "Job cancelled."}


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
    if wants_html(request):
        # Tile builds are cancelled via /collections/{collection_id}/tiles/build/cancel and can be cancelled
        # even while "running". Detect if this job is the latest tile build for the collection.
        is_tile_build = False
        try:
            latest = get_latest_tile_build_job(job.collection_id)
            is_tile_build = latest is not None and latest.job_id == job.job_id
        except Exception:
            is_tile_build = False
        base = _base_url(request)
        return html_response(
            "job.html",
            base=base,
            job_id=job.job_id,
            collection_id=job.collection_id,
            status=job.status,
            is_tile_build=is_tile_build,
            message=job.message or "",
            items_in=job.items_in,
            items_created=job.items_created,
            items_failed=job.items_failed,
            created_at=job.created_at.isoformat() + "Z",
            updated_at=job.updated_at.isoformat() + "Z",
            finished_at=job.finished_at.isoformat() + "Z" if job.finished_at else None,
            username=current_user.username,
            is_admin=current_user.is_admin,
        )
    return job.to_dict()
