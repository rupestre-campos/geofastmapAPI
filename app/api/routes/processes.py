"""OGC API - Processes: geometric operations (intersection, erase) between two collections."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.job_store import create_job
from app.services.process_queue import ProcessJobPayload, enqueue_process_job

router = APIRouter()

PROCESSES = [
    {
        "id": "intersection",
        "title": "Intersection",
        "description": "Compute geometry intersection between two collections. Result collection id: intersection_{id_a}_{id_b}.",
    },
    {
        "id": "erase",
        "title": "Erase",
        "description": "Compute geometry difference (A minus B). Result collection id: erase_{id_a}_{id_b}.",
    },
]


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


class ProcessExecutionInput(BaseModel):
    collection_id_a: str = Field(..., description="First collection (layer A).")
    collection_id_b: str = Field(..., description="Second collection (layer B).")


@router.get(
    "",
    summary="List processes",
    description="OGC API - Processes: list available process identifiers (intersection, erase).",
)
async def list_processes(request: Request):
    base = _base_url(request)
    items = []
    for p in PROCESSES:
        items.append({
            **p,
            "links": [
                {"href": f"{base}/processes/{p['id']}", "rel": "self", "type": "application/json"},
                {"href": f"{base}/processes/{p['id']}/execution", "rel": "execute", "type": "application/json"},
            ],
        })
    return JSONResponse(content={"processes": items, "links": [{"href": f"{base}/processes", "rel": "self", "type": "application/json"}]})


@router.get(
    "/{process_id}",
    summary="Describe process",
)
async def get_process(process_id: str):
    for p in PROCESSES:
        if p["id"] == process_id:
            return JSONResponse(content=p)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Process not found")


async def _execute_process(
    request: Request,
    process_id: str,
    payload: ProcessExecutionInput,
    db: AsyncSession,
):
    if process_id not in ("intersection", "erase"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Process not found")
    coll_a = await collections_crud.get_collection(db, payload.collection_id_a)
    coll_b = await collections_crud.get_collection(db, payload.collection_id_b)
    if not coll_a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection not found: {payload.collection_id_a}")
    if not coll_b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection not found: {payload.collection_id_b}")
    from app.core.config import get_settings
    settings = get_settings()
    if settings.process_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Process execution requires Redis (PROCESS_QUEUE_TYPE=redis). Run a process worker.",
        )
    job = create_job(payload.collection_id_a)
    pl = ProcessJobPayload(
        job_id=job.job_id,
        process_id=process_id,
        collection_id_a=payload.collection_id_a,
        collection_id_b=payload.collection_id_b,
    )
    if not enqueue_process_job(pl):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue process job.")
    base = _base_url(request)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "job_id": job.job_id,
            "status_url": f"{base}/jobs/{job.job_id}",
            "message": f"Process {process_id} queued. Result will be in collection {process_id}_{payload.collection_id_a}_{payload.collection_id_b} (sanitized).",
        },
    )


@router.post(
    "/intersection/execution",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute intersection",
    description="Queue intersection between two collections. Poll status_url for job status. Result collection: intersection_{id_a}_{id_b}.",
)
async def execute_intersection(
    request: Request,
    payload: ProcessExecutionInput,
    db: AsyncSession = Depends(get_db),
):
    return await _execute_process(request, "intersection", payload, db)


@router.post(
    "/erase/execution",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute erase",
    description="Queue erase (A minus B). Result collection: erase_{id_a}_{id_b}.",
)
async def execute_erase(
    request: Request,
    payload: ProcessExecutionInput,
    db: AsyncSession = Depends(get_db),
):
    return await _execute_process(request, "erase", payload, db)
