"""Job status for bulk import and tile/process jobs."""

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.html import html_response, wants_html
from app.services.job_store import get_job, list_jobs_for_collection, update_job

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get(
    "",
    summary="List jobs for a collection",
    description="Returns recent jobs (ongoing and recently completed) for the given collection. Use for showing job links on the collection edit page.",
)
async def list_jobs(
    collection_id: str = Query(..., description="Collection id to list jobs for"),
    limit: int = Query(20, ge=1, le=50, description="Max number of jobs to return"),
):
    jobs = list_jobs_for_collection(collection_id, limit=limit)
    return {"collection_id": collection_id, "jobs": [j.to_dict() for j in jobs]}


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
        base = _base_url(request)
        return html_response(
            "job.html",
            base=base,
            job_id=job.job_id,
            collection_id=job.collection_id,
            status=job.status,
            message=job.message or "",
            items_in=job.items_in,
            items_created=job.items_created,
            items_failed=job.items_failed,
            created_at=job.created_at.isoformat() + "Z",
            updated_at=job.updated_at.isoformat() + "Z",
            finished_at=job.finished_at.isoformat() + "Z" if job.finished_at else None,
        )
    return job.to_dict()
