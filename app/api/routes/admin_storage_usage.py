"""Admin storage usage page: disk + DB sizes per collection / mosaic."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.html import html_response, wants_html
from app.crud import collections as collections_crud
from app.crud import raster_views as raster_views_crud
from app.db.session import get_db
from app.models.user import User
from app.services import storage_usage as su
from app.services.job_store import create_job
from app.services.storage_delete_queue import StorageDeletePayload, enqueue_storage_delete

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ctx(request: Request, current_user: User, **extra: object) -> dict[str, object]:
    out: dict[str, object] = {
        "base": _base_url(request),
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }
    out.update(extra)
    return out


def _csrf(request: Request) -> str:
    if "admin_storage_csrf" not in request.session:
        request.session["admin_storage_csrf"] = secrets.token_urlsafe(32)
    return request.session["admin_storage_csrf"]


def _check_csrf(request: Request, token: str | None) -> None:
    expected = request.session.get("admin_storage_csrf")
    if not expected or not token or not secrets.compare_digest(str(token), str(expected)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def _redirect_storage(request: Request, *, job_id: str | None = None, error: str | None = None) -> RedirectResponse:
    base = _base_url(request)
    params = "f=html"
    if job_id:
        params += f"&job_id={job_id}&queued=1"
    if error:
        params += f"&error={error}"
    return RedirectResponse(url=f"{base}/admin/storage-usage?{params}", status_code=status.HTTP_303_SEE_OTHER)


def _enqueue_delete(
    *,
    action: str,
    target_id: str,
    owner_id: int | None,
    orphan_kind: str | None = None,
    mosaic_json_path: str | None = None,
) -> str:
    job = create_job(target_id, owner_id=owner_id, job_label="storage_delete")
    enqueue_storage_delete(
        StorageDeletePayload(
            job_id=job.job_id,
            action=action,
            target_id=target_id,
            owner_id=owner_id,
            orphan_kind=orphan_kind,
            mosaic_json_path=mosaic_json_path,
        )
    )
    return job.job_id


@router.get("/storage-usage", summary="Admin storage usage dashboard")
async def storage_usage_page(
    request: Request,
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    return html_response(
        "admin_storage_usage.html",
        **_ctx(request, current_user, admin_storage_csrf=_csrf(request)),
    )


@router.get("/storage-usage/api", summary="Cached storage usage snapshot JSON")
async def storage_usage_api(_current_user: User = Depends(require_admin)):
    snap = su.get_snapshot()
    status_info = su.get_status()
    if snap is None:
        return {
            "computed_at": None,
            "row_count": 0,
            "rows": [],
            "totals": {
                "db_bytes": 0,
                "tiles_bytes": 0,
                "raster_bytes": 0,
                "other_bytes": 0,
                "total_bytes": 0,
                "db_h": "0 B",
                "tiles_h": "0 B",
                "raster_h": "0 B",
                "other_h": "0 B",
                "total_h": "0 B",
            },
            "status": status_info,
            "has_snapshot": False,
        }
    return {**snap, "status": status_info, "has_snapshot": True}


@router.post("/storage-usage/refresh", summary="Recompute storage usage (background)")
async def storage_usage_refresh(
    request: Request,
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    _check_csrf(request, form.get("csrf_token"))
    started = su.start_recompute_background()
    if wants_html(request):
        q = "refresh=started" if started else "refresh=busy"
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/storage-usage?f=html&{q}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return {"ok": True, "started": started, "status": su.get_status()}


@router.post("/storage-usage/collections/{collection_id}/delete-tiles")
async def storage_usage_delete_tiles(
    collection_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    _check_csrf(request, form.get("csrf_token"))
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    try:
        job_id = _enqueue_delete(
            action="delete_tiles",
            target_id=collection_id,
            owner_id=current_user.id,
        )
    except RuntimeError as e:
        if wants_html(request):
            return _redirect_storage(request, error="queue_unavailable")
        raise HTTPException(status_code=503, detail=str(e)) from e
    if wants_html(request):
        return _redirect_storage(request, job_id=job_id)
    return {"ok": True, "job_id": job_id}


@router.post("/storage-usage/collections/{collection_id}/delete")
async def storage_usage_delete_collection(
    collection_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    _check_csrf(request, form.get("csrf_token"))
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    try:
        job_id = _enqueue_delete(
            action="delete_collection",
            target_id=collection_id,
            owner_id=current_user.id,
        )
    except RuntimeError as e:
        if wants_html(request):
            return _redirect_storage(request, error="queue_unavailable")
        raise HTTPException(status_code=503, detail=str(e)) from e
    if wants_html(request):
        return _redirect_storage(request, job_id=job_id)
    return {"ok": True, "job_id": job_id}


@router.post("/storage-usage/mosaics/{view_id}/delete")
async def storage_usage_delete_mosaic(
    view_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    _check_csrf(request, form.get("csrf_token"))
    row = await raster_views_crud.get_view(db, view_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mosaic not found")
    try:
        job_id = _enqueue_delete(
            action="delete_mosaic",
            target_id=view_id,
            owner_id=current_user.id,
            mosaic_json_path=row.json_relative_path,
        )
    except RuntimeError as e:
        if wants_html(request):
            return _redirect_storage(request, error="queue_unavailable")
        raise HTTPException(status_code=503, detail=str(e)) from e
    if wants_html(request):
        return _redirect_storage(request, job_id=job_id)
    return {"ok": True, "job_id": job_id}


@router.post("/storage-usage/orphans/{kind}/delete")
async def storage_usage_delete_orphan(
    kind: str,
    request: Request,
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    _check_csrf(request, form.get("csrf_token"))
    item_id = str(form.get("id") or "")
    if not item_id:
        raise HTTPException(status_code=400, detail="Missing id")
    if kind not in ("orphan_tiles", "orphan_rasters", "orphan_mosaic"):
        raise HTTPException(status_code=400, detail="Unknown orphan kind")
    try:
        job_id = _enqueue_delete(
            action="delete_orphan",
            target_id=item_id,
            owner_id=current_user.id,
            orphan_kind=kind,
        )
    except RuntimeError as e:
        if wants_html(request):
            return _redirect_storage(request, error="queue_unavailable")
        raise HTTPException(status_code=503, detail=str(e)) from e
    if wants_html(request):
        return _redirect_storage(request, job_id=job_id)
    return {"ok": True, "job_id": job_id}
