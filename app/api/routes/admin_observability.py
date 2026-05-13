"""Admin-only in-app observability pages and JSON APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.html import html_response, wants_html
from app.db.session import get_db
from app.models.user import User
from app.services.observability_admin import (
    fetch_server_snapshots,
    get_observability_runtime_settings,
    purge_observability_history,
    set_observability_runtime_settings,
)
from app.utils.outbound_url import UnsafeOutboundUrlError, validate_public_http_url

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


@router.get("/observability", summary="Admin observability: live logs")
async def observability_logs_page(
    request: Request,
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    purge_ok = request.query_params.get("purge") == "ok"
    purge_error = request.query_params.get("purge_error") == "confirm"
    settings_ok = request.query_params.get("settings") == "ok"
    settings_error_json = request.query_params.get("settings_error") == "json"
    settings_error_url = request.query_params.get("settings_error") == "url"
    runtime_settings = await get_observability_runtime_settings()
    if "admin_obs_csrf" not in request.session:
        request.session["admin_obs_csrf"] = secrets.token_urlsafe(32)
    return html_response(
        "admin_observability_logs.html",
        **_ctx(
            request,
            current_user,
            purge_ok=purge_ok,
            purge_error=purge_error,
            settings_ok=settings_ok,
            settings_error_json=settings_error_json,
            settings_error_url=settings_error_url,
            runtime_settings=runtime_settings,
            admin_obs_csrf=request.session["admin_obs_csrf"],
        ),
    )


@router.get("/observability/performance", summary="Admin observability: endpoint performance")
async def observability_performance_page(
    request: Request,
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    return html_response("admin_observability_performance.html", **_ctx(request, current_user))


@router.get("/observability/servers", summary="Admin observability: server load")
async def observability_servers_page(
    request: Request,
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    return html_response("admin_observability_servers.html", **_ctx(request, current_user))


@router.get("/observability/api/logs", summary="Admin observability logs JSON")
async def observability_logs_api(
    _current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    status_code: int | None = Query(None),
    status_family: int | None = Query(None, ge=2, le=5),
    method: str | None = Query(None),
    path_contains: str | None = Query(None),
    min_latency_ms: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
    page: int = Query(1, ge=1, le=100000),
):
    where = ["1=1"]
    params: dict[str, object] = {"limit": limit}
    if status_code is not None:
        where.append("status_code = :status_code")
        params["status_code"] = status_code
    if status_family is not None:
        where.append("status_code BETWEEN :status_lo AND :status_hi")
        params["status_lo"] = status_family * 100
        params["status_hi"] = status_family * 100 + 99
    if method:
        where.append("method = :method")
        params["method"] = method.upper()
    if path_contains:
        where.append("path ILIKE :path_like")
        params["path_like"] = f"%{path_contains}%"
    if min_latency_ms is not None:
        where.append("latency_ms >= :min_latency_ms")
        params["min_latency_ms"] = min_latency_ms

    total_r = await db.execute(
        text(
            f"""
            SELECT COUNT(*)::bigint
            FROM request_events
            WHERE {' AND '.join(where)}
            """
        ),
        params,
    )
    total = int(total_r.scalar() or 0)
    offset = (page - 1) * limit
    params["offset"] = offset

    r = await db.execute(
        text(
            f"""
            SELECT created_at, method, path, route_template, full_url, query_string, client_ip,
                   status_code, latency_ms, username, is_error,
                   CASE WHEN request_body IS NULL THEN NULL ELSE left(request_body, 2000) END AS request_body,
                   CASE WHEN request_headers IS NULL THEN NULL ELSE left(request_headers, 8000) END AS request_headers
            FROM request_events
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    )
    rows = []
    for row in r.mappings():
        rows.append(
            {
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "method": row["method"],
                "path": row["path"],
                "route_template": row["route_template"],
                "full_url": row["full_url"],
                "query_string": row["query_string"],
                "client_ip": row["client_ip"],
                "status_code": int(row["status_code"]),
                "latency_ms": int(row["latency_ms"]),
                "username": row["username"],
                "is_error": bool(row["is_error"]),
                "request_body": row["request_body"],
                "request_headers": row["request_headers"],
            }
        )
    return {
        "items": rows,
        "count": len(rows),
        "total": total,
        "page": page,
        "page_size": limit,
        "total_pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/observability/api/performance", summary="Admin observability performance JSON")
async def observability_performance_api(
    _current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    hours: int = Query(6, ge=1, le=168),
    endpoint: str | None = Query(None),
):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    params: dict[str, object] = {"since": since}
    endpoint_clause = ""
    if endpoint:
        endpoint_clause = " AND route_template = :endpoint"
        params["endpoint"] = endpoint

    r = await db.execute(
        text(
            f"""
            SELECT bucket_minute, route_template, request_count, mean_ms, p50_ms, p90_ms
            FROM request_metrics_minute
            WHERE bucket_minute >= :since
            {endpoint_clause}
            ORDER BY bucket_minute ASC
            LIMIT 5000
            """
        ),
        params,
    )
    points = []
    for row in r.mappings():
        points.append(
            {
                "bucket_minute": row["bucket_minute"].isoformat() if row["bucket_minute"] else None,
                "route_template": row["route_template"],
                "request_count": int(row["request_count"]),
                "mean_ms": int(row["mean_ms"]),
                "p50_ms": int(row["p50_ms"]),
                "p90_ms": int(row["p90_ms"]),
            }
        )

    e = await db.execute(
        text(
            """
            SELECT route_template, COUNT(*)::int AS c
            FROM request_events
            WHERE created_at >= :since
            GROUP BY route_template
            ORDER BY c DESC
            LIMIT 40
            """
        ),
        {"since": since},
    )
    endpoints = [{"route_template": row[0], "count": int(row[1])} for row in e.fetchall()]
    return {"points": points, "endpoints": endpoints}


@router.get("/observability/api/servers", summary="Admin observability servers JSON")
async def observability_servers_api(_current_user: User = Depends(require_admin)):
    snaps = await fetch_server_snapshots()
    return {"servers": snaps}


@router.post("/observability/logs/purge", summary="Admin purge observability logs")
async def observability_purge_logs(
    request: Request,
    _current_user: User = Depends(require_admin),
):
    form = await request.form()
    if (form.get("csrf_token") or "").strip() != (request.session.get("admin_obs_csrf") or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")
    confirm = (form.get("confirm") or "").strip().upper()
    days_raw = (form.get("older_than_days") or "").strip()
    older_than_days = int(days_raw) if days_raw else None
    if confirm != "DELETE":
        return RedirectResponse(
            url=f"{_base_url(request)}/admin/observability?f=html&purge_error=confirm",
            status_code=status.HTTP_302_FOUND,
        )
    await purge_observability_history(older_than_days=older_than_days)
    return RedirectResponse(
        url=f"{_base_url(request)}/admin/observability?f=html&purge=ok",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/observability/settings", summary="Admin update observability settings")
async def observability_update_settings(
    request: Request,
    _current_user: User = Depends(require_admin),
):
    form = await request.form()
    if (form.get("csrf_token") or "").strip() != (request.session.get("admin_obs_csrf") or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")
    logging_enabled = "logging_enabled" in form
    log_debug_mode = "log_debug_mode" in form
    log_debug_max_body_bytes = int((form.get("log_debug_max_body_bytes") or "4096").strip())
    log_retention_days = int((form.get("log_retention_days") or "7").strip())
    metrics_retention_days = int((form.get("metrics_retention_days") or "30").strip())
    cleanup_interval_seconds = int((form.get("cleanup_interval_seconds") or "3600").strip())
    servers_json = (form.get("servers_json") or "[]").strip()

    # Validate JSON early; if invalid keep previous setting by redirecting with error marker.
    if servers_json:
        import json

        try:
            parsed = json.loads(servers_json)
            if not isinstance(parsed, list):
                raise ValueError("servers_json must be a JSON array")
            for item in parsed:
                if not isinstance(item, dict):
                    raise ValueError("each server entry must be an object")
                bu = str(item.get("base_url") or "").strip()
                if bu:
                    validate_public_http_url(bu.rstrip("/"))
        except UnsafeOutboundUrlError:
            return RedirectResponse(
                url=f"{_base_url(request)}/admin/observability?f=html&settings_error=url",
                status_code=status.HTTP_302_FOUND,
            )
        except Exception:
            return RedirectResponse(
                url=f"{_base_url(request)}/admin/observability?f=html&settings_error=json",
                status_code=status.HTTP_302_FOUND,
            )

    await set_observability_runtime_settings(
        {
            "logging_enabled": str(bool(logging_enabled)).lower(),
            "log_debug_mode": str(bool(log_debug_mode)).lower(),
            "log_debug_max_body_bytes": str(max(128, log_debug_max_body_bytes)),
            "log_retention_days": str(max(1, log_retention_days)),
            "metrics_retention_days": str(max(1, metrics_retention_days)),
            "cleanup_interval_seconds": str(max(60, cleanup_interval_seconds)),
            "servers_json": servers_json or "[]",
        }
    )
    return RedirectResponse(
        url=f"{_base_url(request)}/admin/observability?f=html&settings=ok",
        status_code=status.HTTP_302_FOUND,
    )
