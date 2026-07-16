"""Job status for bulk import and tile/process jobs. Users see only their jobs; admins see all and owner."""

import json
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

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
from app.services.job_store import get_job, list_all_jobs, list_all_jobs_unpaginated, list_jobs_for_collection, update_job
from app.services.process_queue import get_process_job_meta
from app.services.tile_build_queue import clear_pending, get_latest_tile_build_job, update_tile_build_job
from app.utils.job_display import PROCESS_TYPE_LABELS, build_job_view_dict

router = APIRouter()

_ACTIVE_JOB_STATUSES = frozenset({"pending", "running", "replacing", "finalizing"})
_ACTIVE_STATUS_FILTER = frozenset({"pending", "running", "replacing", "finalizing", "cancelling"})
_JOB_OPERATION_CHOICES = (
    ("bulk_import", "Bulk import"),
    ("tile_build", "Static tile build"),
    ("property_index", "Property index"),
    ("raster_import", "Raster import"),
    ("process", "Any geoprocess"),
) + tuple((pid, label) for pid, label in sorted(PROCESS_TYPE_LABELS.items()))


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _parse_job_time(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse since/until query values (ISO date or datetime). Naive UTC for comparisons."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            dt = datetime(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _job_matches_collection(job: dict, needle: str) -> bool:
    n = needle.lower()
    fields = [
        job.get("collection_id"),
        job.get("result_collection_id"),
        job.get("input_summary"),
        job.get("collection_id_a"),
        job.get("collection_id_b"),
    ]
    for f in fields:
        if f and n in str(f).lower():
            return True
    cids = job.get("collection_ids")
    if isinstance(cids, list):
        for cid in cids:
            if cid and n in str(cid).lower():
                return True
    return False


def _job_matches_operation(job: dict, operation: str) -> bool:
    op = (operation or "").strip().lower()
    if not op:
        return True
    cat = (job.get("job_category") or "").lower()
    pid = (job.get("process_id") or "").lower()
    if op == "process":
        return cat == "process"
    if op in PROCESS_TYPE_LABELS or op in {"intersection", "erase", "buffer", "explode", "make_valid", "union", "measure"}:
        return pid == op
    return cat == op


def _job_matches_status(job: dict, status_filter: str) -> bool:
    st = (status_filter or "").strip().lower()
    if not st:
        return True
    job_st = (job.get("status") or "").lower()
    if st == "active":
        return job_st in _ACTIVE_STATUS_FILTER
    allowed = {s.strip() for s in st.split(",") if s.strip()}
    return job_st in allowed


def _job_created_naive(job: dict) -> datetime | None:
    raw = job.get("created_at") or job.get("updated_at")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    return _parse_job_time(str(raw))


def _job_in_time_range(job: dict, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    ts = _job_created_naive(job)
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _filter_job_dicts(
    jobs: list[dict],
    *,
    status_filter: str | None,
    collection_q: str | None,
    operation: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[dict]:
    out: list[dict] = []
    coll = (collection_q or "").strip()
    for j in jobs:
        if not _job_matches_status(j, status_filter or ""):
            continue
        if coll and not _job_matches_collection(j, coll):
            continue
        if operation and not _job_matches_operation(j, operation):
            continue
        if not _job_in_time_range(j, since, until):
            continue
        out.append(j)
    return out


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
    description=(
        "Returns recent jobs (own only; admins see all and owner). "
        "Without collection_id: all jobs. With collection_id: jobs for that collection only. "
        "Supports pagination (limit/offset) and filters: status, collection, operation, since, until. "
        "Requires login."
    ),
)
async def list_jobs(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user_required)],
    db: Annotated[AsyncSession, Depends(get_db)],
    collection_id: str | None = Query(None, description="Optional collection id to filter jobs. If omitted, returns all jobs."),
    limit: int = Query(25, ge=1, le=200, description="Max number of jobs to return"),
    offset: int = Query(0, ge=0, description="Number of jobs to skip"),
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Job status, comma-separated, or 'active' for pending/running/replacing/finalizing.",
    ),
    collection_q: str | None = Query(
        None,
        alias="collection",
        description="Substring match on collection id / input / result (when listing all jobs).",
    ),
    operation: str | None = Query(
        None,
        description="Operation: bulk_import, tile_build, property_index, raster_import, process, or a process_id.",
    ),
    since: str | None = Query(None, description="Only jobs created at/after this ISO date or datetime (UTC)."),
    until: str | None = Query(None, description="Only jobs created at/before this ISO date or datetime (UTC)."),
):
    owner_filter = None if current_user.is_admin else current_user.id
    since_dt = _parse_job_time(since)
    until_dt = _parse_job_time(until, end_of_day=True)
    has_list_filters = bool(
        (status_filter and status_filter.strip())
        or (collection_q and collection_q.strip())
        or (operation and operation.strip())
        or since_dt
        or until_dt
    )

    if wants_html(request) and collection_id is None:
        raw_jobs = list_all_jobs_unpaginated(owner_id=owner_filter, max_jobs=5000)
        owner_ids = [j.owner_id for j in raw_jobs if j.owner_id is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if (current_user.is_admin and owner_ids) else {}
        jobs_for_page = _build_job_dicts(raw_jobs, include_owner_username=current_user.is_admin, owner_names=owner_names)
        filtered = _filter_job_dicts(
            jobs_for_page,
            status_filter=status_filter,
            collection_q=collection_q,
            operation=operation,
            since=since_dt,
            until=until_dt,
        )
        number_matched = len(filtered)
        page = filtered[offset : offset + limit]
        base = _base_url(request)

        def _page_url(new_offset: int) -> str:
            q = {
                "f": "html",
                "limit": str(limit),
                "offset": str(max(0, new_offset)),
            }
            if status_filter:
                q["status"] = status_filter
            if collection_q:
                q["collection"] = collection_q
            if operation:
                q["operation"] = operation
            if since:
                q["since"] = since
            if until:
                q["until"] = until
            return f"{base}/jobs?" + urlencode(sorted(q.items()))

        prev_page_url = _page_url(offset - limit) if offset > 0 else None
        next_page_url = _page_url(offset + limit) if offset + len(page) < number_matched else None
        return html_response(
            "jobs_list.html",
            base=base,
            jobs=page,
            username=current_user.username,
            is_admin=current_user.is_admin,
            limit=limit,
            offset=offset,
            number_matched=number_matched,
            number_returned=len(page),
            status_filter=status_filter or "",
            collection_q=collection_q or "",
            operation=operation or "",
            since=since or "",
            until=until or "",
            operation_choices=_JOB_OPERATION_CHOICES,
            prev_page_url=prev_page_url,
            next_page_url=next_page_url,
        )

    if collection_id is not None:
        collection = await collections_crud.get_collection(db, collection_id)
        if not collection or not await can_see_collection(db, collection, current_user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
        # Collection-scoped list: still allow status/operation/time filters.
        fetch_limit = 500 if has_list_filters else min(limit + offset, 200)
        jobs = list_jobs_for_collection(collection_id, limit=fetch_limit, owner_id=owner_filter)
        owner_ids = [j.owner_id for j in jobs if j.owner_id is not None]
        owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if (current_user.is_admin and owner_ids) else {}
        jobs_out = _build_job_dicts(jobs, include_owner_username=current_user.is_admin, owner_names=owner_names)
        filtered = _filter_job_dicts(
            jobs_out,
            status_filter=status_filter,
            collection_q=None,  # already scoped
            operation=operation,
            since=since_dt,
            until=until_dt,
        )
        page = filtered[offset : offset + limit]
        return {
            "collection_id": collection_id,
            "jobs": page,
            "numberMatched": len(filtered),
            "numberReturned": len(page),
            "limit": limit,
            "offset": offset,
        }

    raw_jobs = list_all_jobs_unpaginated(owner_id=owner_filter, max_jobs=5000)
    owner_ids = [j.owner_id for j in raw_jobs if j.owner_id is not None]
    owner_names = await user_crud.get_usernames_by_ids(db, owner_ids) if current_user.is_admin else {}
    jobs_out = _build_job_dicts(
        raw_jobs,
        include_owner_username=current_user.is_admin,
        owner_names=owner_names if current_user.is_admin else None,
    )
    filtered = _filter_job_dicts(
        jobs_out,
        status_filter=status_filter,
        collection_q=collection_q,
        operation=operation,
        since=since_dt,
        until=until_dt,
    )
    page = filtered[offset : offset + limit]
    return {
        "jobs": page,
        "numberMatched": len(filtered),
        "numberReturned": len(page),
        "limit": limit,
        "offset": offset,
    }


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
