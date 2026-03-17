"""Job status for bulk import and tile/process jobs."""

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.html import html_response, wants_html
from app.services.job_store import get_job, list_all_jobs, list_jobs_for_collection, update_job
from app.services.tile_build_queue import get_latest_tile_build_job

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get(
    "",
    summary="List jobs",
    description="Returns recent jobs. Without collection_id: all jobs (use ?f=html for the jobs list page). With collection_id: jobs for that collection only.",
)
async def list_jobs(
    request: Request,
    collection_id: str | None = Query(None, description="Optional collection id to filter jobs. If omitted, returns all jobs."),
    limit: int = Query(50, ge=1, le=200, description="Max number of jobs to return"),
):
    if wants_html(request) and collection_id is None:
        from app.services.process_queue import get_process_job_meta

        all_jobs = list_all_jobs(limit=limit)
        jobs_for_page = []
        for j in all_jobs:
            d = j.to_dict()
            meta = get_process_job_meta(j.job_id)
            if meta:
                d["process_id"] = meta.get("process_id")
                d["collection_id_a"] = meta.get("collection_id_a")
                d["collection_id_b"] = meta.get("collection_id_b")
                d["collection_ids"] = meta.get("collection_ids")
                d["feature_source"] = meta.get("feature_source")
                d["result_collection_id"] = meta.get("result_collection_id")
                d["is_tile_build"] = False
            else:
                latest = get_latest_tile_build_job(j.collection_id)
                d["is_tile_build"] = latest is not None and latest.job_id == j.job_id
            jobs_for_page.append(d)
        base = _base_url(request)
        return html_response("jobs_list.html", base=base, jobs=jobs_for_page)
    if collection_id is not None:
        jobs = list_jobs_for_collection(collection_id, limit=limit)
        return {"collection_id": collection_id, "jobs": [j.to_dict() for j in jobs]}
    from app.services.process_queue import get_process_job_meta

    all_jobs = list_all_jobs(limit=limit)
    jobs_out = []
    for j in all_jobs:
        d = j.to_dict()
        meta = get_process_job_meta(j.job_id)
        if meta:
            d["process_id"] = meta.get("process_id")
            d["collection_id_a"] = meta.get("collection_id_a")
            d["collection_id_b"] = meta.get("collection_id_b")
            d["collection_ids"] = meta.get("collection_ids")
            d["feature_source"] = meta.get("feature_source")
            d["result_collection_id"] = meta.get("result_collection_id") or d.get("result_collection_id")
        else:
            latest = get_latest_tile_build_job(j.collection_id)
            d["is_tile_build"] = latest is not None and latest.job_id == j.job_id
        jobs_out.append(d)
    return {"jobs": jobs_out}


@router.post(
    "/{job_id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel a queued job",
    description="Marks a job as cancelled. Only jobs with status 'pending' (e.g. process jobs still in queue) can be cancelled. Running or completed jobs are unchanged.",
)
async def cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
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
    description="Returns status of a job: bulk import or static tile build (pending, running, completed, failed, cancelled). Use ?f=html or Accept: text/html for a user-friendly status page.",
)
async def get_job_status(request: Request, job_id: str):
    job = get_job(job_id)
    if not job:
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
        )
    return job.to_dict()
