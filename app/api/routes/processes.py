"""OGC API - Processes: geometric operations (intersection, erase) between two collections."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.html import html_response, wants_html
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.job_store import create_job, get_job
from app.services.process_queue import (
    ProcessJobPayload,
    enqueue_process_job,
    get_process_job_meta,
    list_process_job_ids,
)
from app.services.process_worker import (
    _default_result_collection_id,
    _default_result_collection_id_feature,
)

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
    result_collection_id: str | None = Field(None, description="Result collection name (create new). If omitted, uses process + hash of layer ids.")
    update_existing: bool = Field(False, description="If true, write result into existing collection (result_collection_id must be set).")


class FeatureReference(BaseModel):
    collection_id: str = Field(..., description="Collection containing the feature.")
    feature_id: str = Field(..., description="Feature id.")


class FeatureGeoJSONInput(BaseModel):
    geojson: dict = Field(..., description="GeoJSON Feature or FeatureCollection (geometry only used).")


class ProcessFeatureExecutionInput(BaseModel):
    """Single feature vs multiple layers: provide feature by reference or GeoJSON, and list of collection ids."""
    feature: dict = Field(
        ...,
        description='Feature input: {"type": "reference", "collection_id": "...", "feature_id": "..."} '
        'or {"type": "geojson", "geojson": <GeoJSON Feature or FeatureCollection>}.',
    )
    collection_ids: list[str] = Field(..., min_length=1, description="List of collection (layer) ids to run the process against.")
    result_collection_id: str | None = Field(None, description="Result collection name (create new), or existing id when update_existing. Default: process + hash(feature_id + sorted layers).")
    update_existing: bool = Field(False, description="If true, write result into existing collection (result_collection_id must be set).")


@router.get(
    "",
    summary="List processes",
    description="OGC API - Processes: list available process identifiers (intersection, erase). Use ?f=html for the processing page.",
)
async def list_processes(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    if wants_html(request):
        collections, _ = await collections_crud.list_collections(db, limit=500)
        collection_items = [{"id": c.id, "title": c.title or c.id} for c in collections]
        return html_response(
            "processing.html",
            base=base,
            collections=collection_items,
        )
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
    "/default-result-name",
    summary="Get default result collection name",
    description="Returns deterministic result collection id for the given inputs (collection or feature mode).",
)
async def get_default_result_name(
    mode: str = Query(..., description="collection or feature"),
    process_id: str = Query(..., description="intersection or erase"),
    collection_id_a: str = Query("", description="Layer A (collection mode)."),
    collection_id_b: str = Query("", description="Layer B (collection mode)."),
    feature_id: str = Query("", description="Feature id (feature mode; use 'geojson' for GeoJSON input)."),
    collection_ids: str = Query("", description="Comma-separated collection ids (feature mode)."),
):
    if mode == "collection":
        if not collection_id_a or not collection_id_b:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="collection mode requires collection_id_a and collection_id_b")
        name = _default_result_collection_id(process_id, collection_id_a, collection_id_b)
    elif mode == "feature":
        cids = [x.strip() for x in collection_ids.split(",") if x.strip()]
        if not cids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="feature mode requires collection_ids")
        fid = feature_id.strip() or "geojson"
        name = _default_result_collection_id_feature(process_id, fid, cids)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be collection or feature")
    return {"result_collection_id": name}


@router.get(
    "/jobs",
    summary="List process jobs",
    description="Returns recent process jobs (intersection/erase) with status, layers, and result collection.",
)
async def list_process_jobs(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
):
    base = _base_url(request)
    job_ids = list_process_job_ids(limit=limit)
    jobs = []
    for jid in job_ids:
        job = get_job(jid)
        meta = get_process_job_meta(jid)
        if not job:
            continue
        d = job.to_dict()
        d["status_url"] = f"{base}/jobs/{jid}"
        if meta:
            d["process_id"] = meta.get("process_id")
            d["collection_id_a"] = meta.get("collection_id_a")
            d["collection_id_b"] = meta.get("collection_id_b")
            d["result_collection_id"] = meta.get("result_collection_id")
        jobs.append(d)
    return {"jobs": jobs}


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
    if payload.update_existing:
        if not payload.result_collection_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="update_existing requires result_collection_id (existing collection to update).")
        existing = await collections_crud.get_collection(db, payload.result_collection_id)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection to update not found: {payload.result_collection_id}")
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
        result_collection_id=payload.result_collection_id or None,
        update_existing=payload.update_existing,
    )
    if not enqueue_process_job(pl):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue process job.")
    base = _base_url(request)
    result_msg = f"Result: {payload.result_collection_id}" if payload.result_collection_id else "Result collection name will be process + hash of layer ids."
    if payload.update_existing:
        result_msg = f"Updating existing collection: {payload.result_collection_id}"
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "job_id": job.job_id,
            "status_url": f"{base}/jobs/{job.job_id}",
            "message": f"Process {process_id} queued. {result_msg}",
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


async def _execute_process_feature(
    request: Request,
    process_id: str,
    payload: ProcessFeatureExecutionInput,
    db: AsyncSession,
) -> JSONResponse:
    """Validate feature + collection_ids, create job, enqueue feature-vs-layers process."""
    feat = payload.feature
    if not isinstance(feat, dict) or feat.get("type") not in ("reference", "geojson"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='feature must be {"type": "reference", "collection_id": "...", "feature_id": "..."} or {"type": "geojson", "geojson": {...}}',
        )
    collection_ids = list(payload.collection_ids)
    for cid in collection_ids:
        coll = await collections_crud.get_collection(db, cid)
        if not coll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection not found: {cid}")
    feature_ref = None
    feature_geojson = None
    if feat["type"] == "reference":
        cid = feat.get("collection_id")
        fid = feat.get("feature_id")
        if not cid or not fid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reference must include collection_id and feature_id")
        coll = await collections_crud.get_collection(db, cid)
        if not coll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection not found: {cid}")
        from app.crud import features as features_crud
        feature = await features_crud.get_feature(db, cid, fid)
        if not feature:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Feature not found: {cid}/{fid}")
        feature_ref = {"collection_id": cid, "feature_id": fid}
    else:
        geojson = feat.get("geojson")
        if not geojson or not isinstance(geojson, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="geojson must be a GeoJSON object")
        feature_geojson = geojson
    if payload.update_existing:
        if not payload.result_collection_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="update_existing requires result_collection_id (existing collection to update).")
        existing = await collections_crud.get_collection(db, payload.result_collection_id)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Collection to update not found: {payload.result_collection_id}")
    from app.core.config import get_settings
    settings = get_settings()
    if settings.process_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Process execution requires Redis (PROCESS_QUEUE_TYPE=redis). Run a process worker.",
        )
    job = create_job(collection_ids[0] if feature_ref else "feature")
    pl = ProcessJobPayload(
        job_id=job.job_id,
        process_id=process_id,
        feature_ref=feature_ref,
        feature_geojson=feature_geojson,
        collection_ids=collection_ids,
        result_collection_id=payload.result_collection_id or None,
        update_existing=payload.update_existing,
    )
    if not enqueue_process_job(pl):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue process job.")
    base = _base_url(request)
    result_msg = f"Result: {payload.result_collection_id}" if payload.result_collection_id else "Result collection name will be process + hash(feature + layers)."
    if payload.update_existing:
        result_msg = f"Updating existing collection: {payload.result_collection_id}"
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "job_id": job.job_id,
            "status_url": f"{base}/jobs/{job.job_id}",
            "message": f"Process {process_id} (feature vs {len(collection_ids)} layers) queued. {result_msg}",
        },
    )


@router.post(
    "/intersection-feature/execution",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute intersection (single feature vs layers)",
    description="Queue intersection between one feature (by id or GeoJSON) and multiple collections. Result: collection id intersection_<uuid>.",
)
async def execute_intersection_feature(
    request: Request,
    payload: ProcessFeatureExecutionInput,
    db: AsyncSession = Depends(get_db),
):
    return await _execute_process_feature(request, "intersection", payload, db)


@router.post(
    "/erase-feature/execution",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute erase (single feature vs layers)",
    description="Queue erase: feature minus (union of features in the listed layers). Result: collection id erase_<uuid>.",
)
async def execute_erase_feature(
    request: Request,
    payload: ProcessFeatureExecutionInput,
    db: AsyncSession = Depends(get_db),
):
    return await _execute_process_feature(request, "erase", payload, db)
