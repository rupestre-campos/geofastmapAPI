"""Job status for bulk import."""

from fastapi import APIRouter, HTTPException, status

from app.services.job_store import get_job

router = APIRouter()


@router.get(
    "/{job_id}",
    summary="Job status",
    description="Returns status of a job: bulk import or static tile build (pending, running, completed, failed, cancelled).",
)
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job.to_dict()
