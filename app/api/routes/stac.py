"""Federated STAC Item Search and admin-registered STAC API catalogs."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, require_admin
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import stac_catalogs as stac_catalogs_crud
from app.db.session import get_db
from app.models.user import User
from app.schemas.stac_catalog import StacCatalogCreate, StacCatalogRead, StacCatalogUpdate
from app.services.stac_collections_list import fetch_collections_grouped
from app.services.stac_federation import federated_search
from app.services.stac_search_cache import cache_key, get_cached, set_cached

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _execute_stac_search(
    db: AsyncSession, body: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Shared POST /stac/search logic (merge, cache). Returns (item_collection, per-catalog errors)."""
    settings = get_settings()
    catalog_ids_filter = body.get("catalog_ids") or body.get("geofast_catalog_ids")
    all_catalogs = await stac_catalogs_crud.list_catalogs(db, enabled_only=True)
    if isinstance(catalog_ids_filter, list) and catalog_ids_filter:
        want = set(str(x) for x in catalog_ids_filter)
        catalogs = [c for c in all_catalogs if c.id in want]
    else:
        catalogs = all_catalogs

    max_c = settings.stac_search_max_catalogs
    if len(catalogs) > max_c:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many catalogs in search (max {max_c}); narrow catalog_ids",
        )

    cache_k = cache_key(body, [c.id for c in catalogs])
    hit = get_cached(cache_k)
    if hit is not None:
        return hit, []

    merged, catalog_errors = await federated_search(catalogs, body)
    # If any upstream catalogs failed, don't cache this response. Otherwise a transient outage
    # can poison the cache with an empty merge and make searches look permanently empty.
    if not catalog_errors:
        set_cached(cache_k, merged)
    return merged, catalog_errors


def _normalize_collections_param(collections: list[str]) -> list[str]:
    """Split comma-separated entries (legacy bookmarks) and dedupe."""
    out: list[str] = []
    for part in collections:
        for c in str(part).split(","):
            c = c.strip()
            if c:
                out.append(c)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _effective_datetime(
    date_start: str | None,
    date_end: str | None,
    datetime_legacy: str | None,
) -> str | None:
    ds = (date_start or "").strip()
    de = (date_end or "").strip()
    if ds or de:
        if ds and de:
            return f"{ds}T00:00:00Z/{de}T23:59:59Z"
        if ds:
            return f"{ds}T00:00:00Z/.."
        return f"../{de}T23:59:59Z"
    dl = (datetime_legacy or "").strip()
    return dl if dl else None


def _dates_for_template(
    date_start: str | None,
    date_end: str | None,
    datetime_legacy: str | None,
) -> tuple[str, str]:
    ds = (date_start or "").strip()
    de = (date_end or "").strip()
    if ds or de:
        return ds, de
    dl = (datetime_legacy or "").strip()
    if not dl:
        return "", ""
    if "/" in dl:
        a, b = dl.split("/", 1)
        a = a.strip()
        b = b.strip()
        out_s = ""
        out_e = ""
        if a and a != "..":
            out_s = a[:10] if len(a) >= 10 else a
        if b and b != "..":
            out_e = b[:10] if len(b) >= 10 else b
        return out_s, out_e
    return (dl[:10] if len(dl) >= 10 else ""), ""


def _parse_bbox(bbox: str | None) -> list[float] | None:
    if not bbox or not str(bbox).strip():
        return None
    parts = [p.strip() for p in str(bbox).split(",")]
    if len(parts) != 4:
        return None
    try:
        return [float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])]
    except ValueError:
        return None


def _build_stac_search_body_for_html(
    *,
    bbox: str | None,
    datetime_param: str | None,
    collections_list: list[str],
    catalog_ids: list[str] | None,
    fetch_limit: int,
    cloud_cover_max: float | None,
) -> dict[str, Any]:
    """STAC Item Search JSON for federated search (upstream fetch cap)."""
    body: dict[str, Any] = {"limit": fetch_limit}
    bb = _parse_bbox(bbox)
    if bb is not None:
        body["bbox"] = bb
    if datetime_param and datetime_param.strip():
        body["datetime"] = datetime_param.strip()
    if collections_list:
        body["collections"] = collections_list
    if cloud_cover_max is not None:
        body["query"] = {"eo:cloud_cover": {"lte": float(cloud_cover_max)}}
    if catalog_ids:
        body["catalog_ids"] = catalog_ids
    return body


def _catalog_ids_from_stac_target(
    stac_target: str | None,
    enabled_catalog_ids: set[str],
) -> list[str] | None:
    """Map UI stac_target to federated catalog_ids (None = all enabled)."""
    t = (stac_target or "all").strip()
    if t in ("", "all", "geofastmap"):
        return None
    if t in enabled_catalog_ids:
        return [t]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown STAC endpoint selection: {t!r}",
    )


@router.get("/available-collections", summary="Collections exposed by selected STAC endpoint(s)")
async def stac_available_collections(
    db: AsyncSession = Depends(get_db),
    stac_target: str = Query("all", description="Same values as STAC search page"),
):
    """Returns grouped collection ids/titles for building the HTML multi-select (follows STAC paging)."""
    rows_enabled = await stac_catalogs_crud.list_catalogs(db, enabled_only=True)
    enabled_ids = {c.id for c in rows_enabled}
    resolved = _catalog_ids_from_stac_target(stac_target, enabled_ids)
    if resolved is None:
        target_catalogs = list(rows_enabled)
    else:
        want = set(resolved)
        target_catalogs = [c for c in rows_enabled if c.id in want]
    groups = await fetch_collections_grouped(target_catalogs)
    return {"groups": groups}


@router.get("", summary="STAC hubs (JSON or HTML)")
async def stac_hub(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    submitted: bool = Query(False, description="If true, run federated Item Search (HTML)."),
    bbox: str | None = Query(None, description="minx,miny,maxx,maxy WGS84"),
    datetime_param: str | None = Query(None, alias="datetime", description="Legacy STAC datetime (prefer date_start/date_end)"),
    date_start: str | None = Query(None, description="Start date YYYY-MM-DD (UTC day)"),
    date_end: str | None = Query(None, description="End date YYYY-MM-DD (UTC day)"),
    collections: list[str] | None = Query(None, description="STAC collection ids (repeat param; comma-separated supported)"),
    cloud_cover_max: float | None = Query(
        None,
        ge=0,
        le=100,
        description="Max eo:cloud_cover (requires STAC Query extension upstream)",
    ),
    stac_target: str = Query(
        "all",
        description="all | geofastmap (this hub) | registered catalog id",
    ),
    limit: int = Query(20, ge=1, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """List catalogs; HTML: map + form search + paginated results; JSON: hub links."""
    settings = get_settings()
    base = _base_url(request)
    max_page = settings.stac_search_html_max_limit
    if limit > max_page:
        limit = max_page

    rows_enabled = await stac_catalogs_crud.list_catalogs(db, enabled_only=True)
    catalogs = [StacCatalogRead.model_validate(x) for x in rows_enabled]
    enabled_ids = {c.id for c in rows_enabled}

    if wants_html(request):
        features_page: list[dict[str, Any]] = []
        number_matched = 0
        number_returned = 0
        search_error: str | None = None
        catalog_errors: list[dict[str, str]] = []

        cols_norm = _normalize_collections_param(collections or [])
        eff_dt = _effective_datetime(date_start, date_end, datetime_param)
        date_start_val, date_end_val = _dates_for_template(date_start, date_end, datetime_param)

        bbox_for_links = bbox or ""
        if submitted:
            fetch_cap = min(settings.stac_search_html_max_features, 10_000)
            # Default world bbox if none (map-driven search still sends bbox from JS)
            eff_bbox = bbox
            if not (eff_bbox and str(eff_bbox).strip()):
                eff_bbox = "-180,-85,180,85"
            bbox_for_links = eff_bbox
            try:
                resolved_ids = _catalog_ids_from_stac_target(stac_target, enabled_ids)
                body = _build_stac_search_body_for_html(
                    bbox=eff_bbox,
                    datetime_param=eff_dt,
                    collections_list=cols_norm,
                    catalog_ids=resolved_ids,
                    fetch_limit=fetch_cap,
                    cloud_cover_max=cloud_cover_max,
                )
                merged, catalog_errors = await _execute_stac_search(db, body)
                all_feats = merged.get("features") if isinstance(merged, dict) else None
                if not isinstance(all_feats, list):
                    all_feats = []
                number_matched = len(all_feats)
                features_page = all_feats[offset : offset + limit]
                number_returned = len(features_page)
            except HTTPException as e:
                d = e.detail
                search_error = d if isinstance(d, str) else str(d)
            except Exception as e:
                search_error = str(e)

        # Pagination hrefs (preserve filters)
        qparts: list[tuple[str, str]] = [("f", "html"), ("submitted", "true")]
        if bbox_for_links:
            qparts.append(("bbox", str(bbox_for_links)))
        if (date_start or "").strip():
            qparts.append(("date_start", (date_start or "").strip()))
        if (date_end or "").strip():
            qparts.append(("date_end", (date_end or "").strip()))
        if not (date_start or "").strip() and not (date_end or "").strip() and (datetime_param or "").strip():
            qparts.append(("datetime", (datetime_param or "").strip()))
        for c in cols_norm:
            qparts.append(("collections", c))
        if cloud_cover_max is not None:
            qparts.append(("cloud_cover_max", str(cloud_cover_max)))
        qparts.append(("stac_target", stac_target or "all"))
        qparts.append(("limit", str(limit)))

        def page_href(new_offset: int) -> str:
            parts = qparts + [("offset", str(new_offset))]
            return f"{base}/stac?{urlencode(parts, doseq=True)}"

        prev_page_url = page_href(max(0, offset - limit)) if submitted and offset > 0 else None
        next_page_url = (
            page_href(offset + limit)
            if submitted and (offset + number_returned < number_matched)
            else None
        )

        return html_response(
            "stac.html",
            base=base,
            username=current_user.username if current_user else None,
            is_admin=current_user.is_admin if current_user else False,
            catalogs=catalogs,
            stac_target=stac_target or "all",
            manage_url=f"{base}/stac/manage?f=html",
            submitted=submitted,
            bbox=bbox or "",
            datetime_param=datetime_param or "",
            date_start=date_start_val,
            date_end=date_end_val,
            collections_selected=cols_norm,
            cloud_cover_max=cloud_cover_max,
            limit=limit,
            offset=offset,
            features=features_page,
            number_matched=number_matched,
            number_returned=number_returned,
            prev_page_url=prev_page_url,
            next_page_url=next_page_url,
            search_error=search_error,
            catalog_errors=catalog_errors,
            google_maps_api_key=settings.google_maps_api_key or "",
            max_page_limit=max_page,
            features_geojson={"type": "FeatureCollection", "features": features_page},
        )

    return {
        "title": "STAC federated search",
        "catalogs": [c.model_dump(mode="json") for c in catalogs],
        "links": [
            {"href": f"{base}/stac/catalogs", "rel": "catalogs"},
            {"href": f"{base}/stac/search", "rel": "search", "method": "POST"},
            {"href": f"{base}/stac/manage?f=html", "rel": "manage", "type": "text/html", "title": "Manage STAC endpoints"},
        ],
    }


@router.get("/manage", summary="Manage STAC API endpoints (HTML, admin)")
async def stac_manage_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """Admin UI to add, edit, and delete registered STAC API roots."""
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    if current_user is None or not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    base = _base_url(request)
    settings = get_settings()
    admin_rows = await stac_catalogs_crud.list_catalogs(db, enabled_only=False)
    admin_catalogs = [StacCatalogRead.model_validate(x) for x in admin_rows]
    return html_response(
        "stac_manage.html",
        base=base,
        username=current_user.username,
        is_admin=True,
        admin_catalogs=admin_catalogs,
        google_maps_api_key=settings.google_maps_api_key or "",
        search_url=f"{base}/stac?f=html",
    )


@router.get("/catalogs", summary="List registered STAC catalogs", response_model=list[StacCatalogRead])
async def list_stac_catalogs(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    include_disabled: bool = Query(False, description="Admin: include disabled catalogs."),
):
    if include_disabled:
        if current_user is None or not current_user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
        rows = await stac_catalogs_crud.list_catalogs(db, enabled_only=False)
    else:
        rows = await stac_catalogs_crud.list_catalogs(db, enabled_only=True)
    return [StacCatalogRead.model_validate(x) for x in rows]


@router.post("/catalogs", summary="Register a STAC API catalog (admin)", response_model=StacCatalogRead)
async def create_stac_catalog(
    body: StacCatalogCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = await stac_catalogs_crud.create_catalog(
        db,
        title=body.title,
        stac_api_root_url=body.stac_api_root_url,
        enabled=body.enabled,
        notes=body.notes,
        default_collections=body.default_collections,
        catalog_id=body.id,
    )
    return StacCatalogRead.model_validate(row)


@router.put("/catalogs/{catalog_id}", summary="Update STAC catalog (admin)", response_model=StacCatalogRead)
async def update_stac_catalog(
    catalog_id: str,
    body: StacCatalogUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = await stac_catalogs_crud.update_catalog(
        db,
        catalog_id,
        title=body.title,
        stac_api_root_url=body.stac_api_root_url,
        enabled=body.enabled,
        notes=body.notes,
        default_collections=body.default_collections,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
    return StacCatalogRead.model_validate(row)


@router.delete("/catalogs/{catalog_id}", summary="Delete STAC catalog (admin)", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stac_catalog(
    catalog_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    ok = await stac_catalogs_crud.delete_catalog(db, catalog_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")


@router.post("/search", summary="Federated STAC Item Search across registered catalogs")
async def stac_item_search(
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db),
):
    """Merge Item Search results from enabled STAC catalogs. Optional body key `catalog_ids` filters catalogs."""
    merged, _errors = await _execute_stac_search(db, body)
    return merged
