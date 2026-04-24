"""Mosaic planner API and HTML entry points."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import box, shape
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, get_current_user_required
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.core.permissions import can_see_collection
from app.crud import features as features_crud
from app.crud import stac_catalogs as stac_catalogs_crud
from app.crud import collections as collections_crud
from app.api.routes.raster_views import compute_mosaic_tiles_revision
from app.db.session import AsyncSessionLocal, get_db
from app.models.user import User
from app.services.mosaic_plan import (
    collect_stac_features,
    plan_mosaic_with_void_fill,
    season_datetime_slices,
    swap_options_for_selected,
)
from app.services.mosaic_plan_distributed import plan_mosaic_with_void_fill_distributed
from app.services.mosaic_footprint_distributed import try_attach_footprints_distributed
from app.services.mosaic_preview_footprint import attach_footprint_displays_to_plan_result
from app.services.mosaic_plan_jobs import enqueue_mosaic_plan_job, get_mosaic_plan_job
from geoalchemy2.shape import to_shape


router = APIRouter(prefix="/mosaics", tags=["mosaics"])


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


class MosaicPlanBody(BaseModel):
    catalog_id: str = Field(..., min_length=1, description="Registered STAC catalog id")
    stac_collection_id: str = Field(..., min_length=1, description="Single STAC collection id")
    bbox: list[float] | None = Field(None, description="minx,miny,maxx,maxy WGS84 for STAC search")
    aoi_geojson: dict[str, Any] | None = Field(None, description="GeoJSON Polygon geometry")
    date_start: str | None = None
    date_end: str | None = None
    seasons: list[str] = Field(default_factory=list, description="spring, summer, autumn, winter (northern hemisphere)")
    cloud_cover_max: float | None = Field(None, ge=0, le=100)
    sort_mode: str = Field("lowest_cloud", description="lowest_cloud | newest_first")
    use_same_pass_date_strips: bool = Field(
        False,
        description="If true, restrict to a sliding 7-day UTC date window (best mean cloud), then per-column same-day strips; STAC cloud filter is not applied. Void-fill rounds stay in that window. Gap fill may mix dates.",
    )
    geofast_collection_id: str | None = None
    geofast_feature_id: str | None = None
    selected: list[dict[str, Any]] | None = Field(
        None,
        description="Optional pre-selected items (key + footprint). If provided, the planner returns swap_options for these items.",
    )
    swap_options_limit: int = Field(
        5,
        ge=1,
        le=50,
        description="Max same-tile alternatives returned per selected row per request (use swap_options_offset to page).",
    )
    swap_options_offset: dict[str, int] | None = Field(
        None,
        description="Per dedupe key: skip this many alternatives before returning the next page.",
    )
    include_footprint_display: bool = Field(
        True,
        description="If false, skip thumbnail-based footprint display post-processing for faster planning.",
    )

    @field_validator("date_start", "date_end", mode="before")
    @classmethod
    def _blank_dates_to_none(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v


@router.post(
    "/plan",
    summary="Plan a mosaic (STAC search + greedy coverage)",
    description="Returns selected items, footprints, and swap options. Requires login.",
)
async def mosaic_plan(
    body: MosaicPlanBody,
    async_mode: bool = Query(False, description="If true, enqueue compute and return job id."),
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    if async_mode and settings.mosaic_queue_type == "redis":
        job_id = enqueue_mosaic_plan_job(body.model_dump(), int(current_user.id))
        return {"job_id": job_id, "status": "pending"}
    return await compute_mosaic_plan(body, current_user)


async def compute_mosaic_plan(
    body: MosaicPlanBody,
    current_user: User,
    *,
    allow_distributed: bool = False,
) -> dict[str, Any]:
    if body.sort_mode not in ("lowest_cloud", "newest_first"):
        raise HTTPException(status_code=400, detail="sort_mode must be lowest_cloud or newest_first")

    async with AsyncSessionLocal() as db:
        catalog = await stac_catalogs_crud.get_catalog(db, body.catalog_id)
        if not catalog or not catalog.enabled:
            raise HTTPException(status_code=404, detail="Unknown or disabled STAC catalog")

        aoi = None
        search_bbox: list[float]

        if body.geofast_collection_id and body.geofast_feature_id:
            coll = await collections_crud.get_collection(db, body.geofast_collection_id)
            if not coll or not await can_see_collection(db, coll, current_user):
                raise HTTPException(status_code=404, detail="Collection not found")
            feat = await features_crud.get_feature(db, body.geofast_collection_id, body.geofast_feature_id)
            if feat is None or feat.geometry is None:
                raise HTTPException(status_code=404, detail="Feature not found")
            sh = to_shape(feat.geometry)
            if sh.is_empty:
                raise HTTPException(status_code=400, detail="Feature has no geometry")
            if sh.geom_type == "Polygon":
                aoi = sh
            elif sh.geom_type == "MultiPolygon":
                aoi = max(sh.geoms, key=lambda p: p.area)
            else:
                raise HTTPException(status_code=400, detail="Feature geometry must be polygonal")
            b = aoi.bounds
            pad = max((b[2] - b[0]) * 0.01, (b[3] - b[1]) * 0.01, 0.005)
            search_bbox = [
                max(-180.0, b[0] - pad),
                max(-85.0, b[1] - pad),
                min(180.0, b[2] + pad),
                min(85.0, b[3] + pad),
            ]
        elif body.aoi_geojson:
            try:
                g = shape(body.aoi_geojson)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid aoi_geojson")
            if g.geom_type == "Polygon":
                aoi = g
            elif g.geom_type == "MultiPolygon":
                aoi = max(g.geoms, key=lambda p: p.area)
            else:
                raise HTTPException(status_code=400, detail="AOI must be a Polygon or MultiPolygon")
            b = aoi.bounds
            pad = max((b[2] - b[0]) * 0.01, (b[3] - b[1]) * 0.01, 0.005)
            search_bbox = [
                max(-180.0, b[0] - pad),
                max(-85.0, b[1] - pad),
                min(180.0, b[2] + pad),
                min(85.0, b[3] + pad),
            ]
        elif body.bbox and len(body.bbox) == 4:
            search_bbox = [float(x) for x in body.bbox]
            b = search_bbox
            aoi = box(b[0], b[1], b[2], b[3])
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide bbox (4 floats), aoi_geojson (Polygon), or geofast_collection_id + geofast_feature_id",
            )

        db.expunge(catalog)

    slices = season_datetime_slices(body.date_start, body.date_end, body.seasons or None)
    if not slices:
        raise HTTPException(status_code=400, detail="Set date_start and/or date_end")

    settings = get_settings()
    cap = min(int(settings.mosaic_stac_fetch_limit or 500), settings.stac_search_max_catalogs * 200)

    cloud_for_search = None if body.use_same_pass_date_strips else body.cloud_cover_max
    swap_off = body.swap_options_offset or {}
    swap_lim = body.swap_options_limit

    if body.selected:
        features, stac_errors = await collect_stac_features(
            [catalog],
            stac_collection=body.stac_collection_id,
            bbox=search_bbox,
            datetime_slices=slices,
            cloud_cover_max=body.cloud_cover_max,
            sort_mode=body.sort_mode,
            fetch_limit=cap,
        )
        result = swap_options_for_selected(
            aoi,
            features,
            body.sort_mode,
            body.selected,
            swap_options_limit=swap_lim,
            swap_options_offset=swap_off,
        )
        plan_features = features
    else:
        use_distributed = (
            allow_distributed
            and bool(getattr(settings, "mosaic_subjob_queue_enabled", False))
            and settings.mosaic_queue_type == "redis"
        )
        if use_distributed:
            result, stac_errors, plan_features = await plan_mosaic_with_void_fill_distributed(
                job_id=str(getattr(current_user, "_mosaic_job_id", "") or ""),
                owner_id=int(current_user.id),
                catalogs=[catalog],
                stac_collection=body.stac_collection_id,
                aoi=aoi,
                search_bbox=search_bbox,
                datetime_slices=slices,
                cloud_cover_max=cloud_for_search,
                sort_mode=body.sort_mode,
                fetch_limit=cap,
                same_pass_date_strips=body.use_same_pass_date_strips,
                swap_options_limit=swap_lim,
                swap_options_offset=swap_off,
            )
        else:
            result, stac_errors, plan_features = await plan_mosaic_with_void_fill(
                [catalog],
                stac_collection=body.stac_collection_id,
                aoi=aoi,
                search_bbox=search_bbox,
                datetime_slices=slices,
                cloud_cover_max=cloud_for_search,
                sort_mode=body.sort_mode,
                fetch_limit=cap,
                same_pass_date_strips=body.use_same_pass_date_strips,
                swap_options_limit=swap_lim,
                swap_options_offset=swap_off,
            )
    include_footprint_display = bool(body.include_footprint_display)
    if include_footprint_display:
        job_id = str(getattr(current_user, "_mosaic_job_id", "") or "")
        use_distributed_fp = (
            bool(getattr(settings, "mosaic_footprint_distributed_enabled", False))
            and settings.mosaic_queue_type == "redis"
            and bool(job_id)
        )
        if use_distributed_fp:
            await try_attach_footprints_distributed(
                job_id,
                int(current_user.id),
                result,
                plan_features,
            )
        else:
            await attach_footprint_displays_to_plan_result(result, plan_features)
    else:
        result["footprint_display_skipped"] = True
    result["stac_errors"] = stac_errors
    result["search_bbox"] = search_bbox
    result["datetime_slices"] = slices
    result["catalog_id"] = body.catalog_id
    result["stac_collection_id"] = body.stac_collection_id
    if body.selected:
        result["use_same_pass_date_strips"] = body.use_same_pass_date_strips
    return result


@router.get("/plan-jobs/{job_id}", summary="Get async mosaic plan job status/result")
async def mosaic_plan_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    job = get_mosaic_plan_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if int(job.get("owner_id") or 0) != int(current_user.id):
        raise HTTPException(status_code=404, detail="Job not found")
    status = str(job.get("status") or "")
    out = {
        "job_id": job.get("job_id") or job_id,
        "status": status,
        "message": job.get("message"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "retry_after_seconds": int(job.get("retry_after_seconds") or 1),
        "client_timeout_seconds": int(settings.mosaic_job_client_timeout_seconds or 1800),
    }
    for k in ("phase", "round", "rounds_max", "features_seen", "children_total", "children_done", "children_failed"):
        if k in job:
            out[k] = job[k]
    st = str(job.get("status") or "unknown")
    out["terminal"] = st in ("completed", "completed_with_errors", "failed", "cancelled")
    out["is_stale"] = False
    updated_at = job.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            stale_after = max(5, int(settings.mosaic_job_stale_after_seconds or 180))
            age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
            out["is_stale"] = (not out["terminal"]) and age > stale_after
        except ValueError:
            pass
    if status in ("completed", "completed_with_errors") and isinstance(job.get("result"), dict):
        out["result"] = job["result"]
    return out


@router.get("/planner", summary="Mosaic planner UI")
async def mosaic_planner_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=400, detail="Use ?f=html")
    if current_user is None:
        raise HTTPException(status_code=401, detail="Login required")
    base = _base_url(request)
    settings = get_settings()
    catalogs = await stac_catalogs_crud.list_catalogs(db, enabled_only=True)
    cat_list = [{"id": c.id, "title": c.title or c.id} for c in catalogs]
    return html_response(
        "mosaic_planner.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        stac_catalogs=cat_list,
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.get("", summary="Saved mosaics gallery")
async def mosaics_gallery(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    q: str | None = Query(None),
    bbox: str | None = Query(None, description="minx,miny,maxx,maxy filter"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List saved mosaics (logged-in: yours + shared + public)."""
    from app.crud import raster_views as rv_crud

    if not wants_html(request):
        raise HTTPException(status_code=400, detail="Use ?f=html for gallery")
    if current_user is None:
        raise HTTPException(status_code=401, detail="Login required")
    base = _base_url(request)
    bbox_t: tuple[float, float, float, float] | None = None
    if bbox and bbox.strip():
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                bbox_t = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError:
                bbox_t = None
    rows, total = await rv_crud.list_views_visible_to_user(
        db,
        current_user=current_user,
        limit=limit,
        offset=offset,
        q=q,
        bbox_intersects=bbox_t,
        mine_only=False,
    )
    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "title": r.title,
                "visibility": r.visibility,
                "bbox": r.bbox,
                "allow_public_maps": getattr(r, "allow_public_maps", False),
                "updated_at": r.updated_at.isoformat() + "Z" if r.updated_at else None,
            }
        )
    settings = get_settings()
    return html_response(
        "mosaics_gallery.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        mosaics=items,
        total=total,
        limit=limit,
        offset=offset,
        q=q or "",
        bbox_filter=bbox or "",
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.get("/{view_id}", summary="View one mosaic")
async def mosaic_detail_page(
    request: Request,
    view_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    from app.core.permissions import can_edit_raster_view, can_see_raster_view
    from app.crud import raster_views as rv_crud

    if not wants_html(request):
        raise HTTPException(status_code=400, detail="Use ?f=html")
    row = await rv_crud.get_view(db, view_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Mosaic not found")
    if not await can_see_raster_view(db, row.owner_id, row.visibility, view_id, current_user):
        raise HTTPException(status_code=404, detail="Mosaic not found")
    base = _base_url(request)
    can_edit = await can_edit_raster_view(db, row.owner_id, view_id, current_user)
    settings = get_settings()
    tile_matrix = "WebMercatorQuad"
    ext = "png"
    rev = compute_mosaic_tiles_revision(settings, view_id, row.json_relative_path)
    vq = f"?v={rev}" if rev else ""
    tiles_url = (
        f"{base}/raster-views/{view_id}/titiler/tiles/{tile_matrix}/{{z}}/{{x}}/{{y}}.{ext}{vq}"
    )
    return html_response(
        "mosaic_detail.html",
        base=base,
        username=current_user.username if current_user else None,
        is_admin=current_user.is_admin if current_user else False,
        view_id=view_id,
        title=row.title,
        visibility=row.visibility,
        bbox=row.bbox,
        definition=row.definition,
        allow_public_maps=getattr(row, "allow_public_maps", False),
        can_edit=can_edit,
        tiles_url=tiles_url,
        google_maps_api_key=settings.google_maps_api_key or "",
    )
