"""Admin storage usage page: disk + DB sizes per collection / mosaic."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.html import html_response, wants_html
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.crud import raster_views as raster_views_crud
from app.db.session import get_db
from app.models.user import User
from app.services import storage_usage as su
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.static_tiles_path import default_mbtiles_path

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


async def _delete_collection_tiles(db: AsyncSession, collection_id: str) -> None:
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    paths = []
    if rec and rec.pmtiles_path:
        paths.append(rec.pmtiles_path)
    paths.append(str(default_mbtiles_path(collection_id)))
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            from pathlib import Path

            path = Path(p)
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    await tiles_crud.clear_static_tiles(db, collection_id)
    invalidate_collection_cache(collection_id)


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
    await _delete_collection_tiles(db, collection_id)
    su.patch_row_tiles_cleared(collection_id)
    if wants_html(request):
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/storage-usage?f=html&deleted=tiles",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return {"ok": True}


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
    await collections_crud.delete_collection(db, collection_id)
    su.delete_collection_raster_dir(collection_id)
    # Ensure canonical mbtiles gone even if DB path was empty
    try:
        p = default_mbtiles_path(collection_id)
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    invalidate_collection_cache(collection_id)
    su.remove_row_from_snapshot("collection", collection_id)
    if wants_html(request):
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/storage-usage?f=html&deleted=collection",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return {"ok": True}


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
    su.delete_mosaic_files(row.json_relative_path)
    await raster_views_crud.delete_view(db, view_id)
    su.remove_row_from_snapshot("mosaic", view_id)
    if wants_html(request):
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/storage-usage?f=html&deleted=mosaic",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return {"ok": True}


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
    ok = False
    if kind == "orphan_tiles":
        ok = su.delete_orphan_tiles_file(item_id)
    elif kind == "orphan_rasters":
        ok = su.delete_orphan_raster_dir(item_id)
    elif kind == "orphan_mosaic":
        ok = su.delete_orphan_mosaic_file(item_id)
    else:
        raise HTTPException(status_code=400, detail="Unknown orphan kind")
    if not ok:
        raise HTTPException(status_code=404, detail="Orphan not found or could not delete")
    su.remove_row_from_snapshot(kind, item_id)
    if wants_html(request):
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/storage-usage?f=html&deleted=orphan",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return {"ok": True}
