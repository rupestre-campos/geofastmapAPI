import logging
import secrets
from urllib.parse import quote

from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.html import wants_html

from app.api.routes import (
    admin_observability,
    auth,
    basemaps_api,
    basemaps_pages,
    collection_styles,
    collections,
    coverages,
    internal_raster,
    items,
    jobs,
    maps,
    mosaics,
    processes,
    project_docs,
    raster_public_styles,
    raster_views,
    raster_styles,
    rasters,
    root,
    stac,
    stac_items,
    styles,
    tiles,
    titiler_proxy,
)
from app.core.config import get_settings
from app.utils.geometry_limits import GeometryTooLargeError
from app.middleware.private_html_cache import PrivateHtmlCacheMiddleware
from app.services.observability import init_observability, instrument_fastapi_app
from app.services.observability_admin import (
    ObservabilityRequestLogMiddleware,
    init_observability_logging,
    shutdown_observability_logging,
)
from app.services.bulk_queue import start_memory_consumer
from app.services.bulk_worker import process_bulk_job

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.session import AsyncSessionLocal
    from app.crud import user as user_crud
    from app.models.user import User
    from sqlalchemy import select

    settings = get_settings()
    if settings.database_use_pgbouncer:
        logger.info(
            "Database: PgBouncer mode (NullPool per process; pool at PgBouncer); "
            "database_use_pgbouncer=true",
        )
    else:
        cap = settings.database_pool_size + settings.database_pool_max_overflow
        logger.info(
            "Database pool (per process): pool_size=%s max_overflow=%s (max %s concurrent checkouts); "
            "database_use_pgbouncer=false",
            settings.database_pool_size,
            settings.database_pool_max_overflow,
            cap,
        )

    # Seed default admin user if no users exist
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).limit(1))
        if r.scalar_one_or_none() is None:
            await user_crud.create_user(
                session,
                settings.auth_default_admin_username,
                settings.auth_default_admin_password,
                is_admin=True,
                must_change_password=True,
            )
    init_observability_logging()
    yield
    from app.services.titiler_http import close_titiler_http_client

    await shutdown_observability_logging()
    await close_titiler_http_client()


async def http_exception_redirect_to_login(request: Request, exc):
    """On 401/403 for HTML requests, redirect to login with next=current URL so user can sign in and return."""
    if exc.status_code not in (401, 403):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    # fetch() / XHR use Sec-Fetch-Dest: empty; return JSON so clients do not follow a login redirect
    # and then fail JSON.parse on HTML.
    if request.headers.get("sec-fetch-dest") == "empty":
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    if not wants_html(request):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    base = str(request.base_url).rstrip("/")
    current_url = str(request.url)
    if not current_url.startswith(base):
        current_url = base + "/"
    login_url = f"{base}/auth/login?f=html&next={quote(current_url, safe='')}"
    return RedirectResponse(url=login_url, status_code=302)


def create_app() -> FastAPI:
    from fastapi import HTTPException

    settings = get_settings()
    doc_kw: dict = {}
    if not getattr(settings, "expose_openapi_docs", True):
        doc_kw = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="GeoFastMap API — OGC API - Features style service built with FastAPI and PostgreSQL.",
        lifespan=lifespan,
        **doc_kw,
    )
    init_observability(settings)
    app.add_exception_handler(HTTPException, http_exception_redirect_to_login)

    async def geometry_too_large_handler(request: Request, exc: GeometryTooLargeError):
        from fastapi import status

        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    app.add_exception_handler(GeometryTooLargeError, geometry_too_large_handler)

    from sqlalchemy.exc import OperationalError

    async def db_operational_handler(request: Request, exc: OperationalError):
        parts = [str(exc)]
        if getattr(exc, "orig", None) is not None:
            parts.append(str(exc.orig))
        msg = " ".join(parts).lower()
        if "too many clients" in msg:
            logger.warning("PostgreSQL connection limit exceeded: %s", exc)
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Database connection limit reached; retry shortly. "
                    "If this persists, use PgBouncer (see docs/DEPLOYMENT.md) or reduce "
                    "API_UVICORN_WORKERS / DATABASE_POOL_SIZE / DATABASE_POOL_MAX_OVERFLOW.",
                },
            )
        logger.exception("Unhandled database operational error")
        return JSONResponse(status_code=500, content={"detail": "Database error"})

    app.add_exception_handler(OperationalError, db_operational_handler)

    session_secret = settings.auth_secret_key
    if not session_secret:
        session_secret = secrets.token_hex(32)
        logger.info(
            "AUTH_SECRET_KEY not set; using an auto-generated session key. "
            "Sessions will be invalidated on restart. Set AUTH_SECRET_KEY for production."
        )
    # Trust X-Forwarded-Proto / Host only from listed reverse-proxy hosts (see docs/SECURITY.md).
    th_raw = (getattr(settings, "proxy_headers_trusted_hosts", None) or "*").strip()
    proxy_trusted: list[str] | str = "*" if th_raw == "*" else [h.strip() for h in th_raw.split(",") if h.strip()]
    if isinstance(proxy_trusted, list) and not proxy_trusted:
        proxy_trusted = "*"

    same_site = (getattr(settings, "session_cookie_same_site", None) or "lax").lower()
    if same_site not in ("lax", "strict", "none"):
        same_site = "lax"

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site=same_site,
        https_only=bool(getattr(settings, "session_cookie_https_only", False)),
    )
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=proxy_trusted)
    app.add_middleware(ObservabilityRequestLogMiddleware)
    # HTML pages include session-dependent nav; must not be cached at CDN edge (e.g. Cloudflare).
    app.add_middleware(PrivateHtmlCacheMiddleware)
    # Start in-process bulk consumer when using memory queue (so no Redis/worker needed)
    if settings.bulk_queue_type == "memory":
        start_memory_consumer(process_bulk_job)

    # OGC root: landing page (/) and conformance (/conformance). Must be first so GET / is landing.
    app.include_router(root.router, tags=["ogc"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(admin_observability.router, prefix="/admin", tags=["admin-observability"])

    # Collections and items (features) endpoints following OGC API - Features style.
    app.include_router(
        collections.router,
        prefix="/collections",
        tags=["collections"],
    )
    app.include_router(
        items.router,
        prefix="/collections",
        tags=["items"],
    )
    app.include_router(coverages.router, prefix="/collections", tags=["coverages"])
    app.include_router(titiler_proxy.router, prefix="/collections", tags=["titiler"])
    app.include_router(rasters.router, prefix="/collections", tags=["rasters"])
    app.include_router(raster_styles.router, prefix="/collections", tags=["raster-styles"])
    app.include_router(internal_raster.router, prefix="/internal", tags=["internal"])
    app.include_router(stac.router, prefix="/stac", tags=["stac"])
    app.include_router(stac_items.router, prefix="/stac", tags=["stac"])
    app.include_router(raster_views.router, tags=["raster-views"])
    app.include_router(mosaics.router, tags=["mosaics"])
    app.include_router(tiles.router, prefix="/collections", tags=["tiles"])
    app.include_router(collection_styles.router, prefix="/collections", tags=["styles"])
    # Register /styles/basemaps before /styles so it is not captured by /styles/{style_id}.
    app.include_router(basemaps_api.router, prefix="/styles/basemaps", tags=["basemaps"])
    app.include_router(styles.router, prefix="/styles", tags=["styles"])
    app.include_router(raster_public_styles.router, prefix="/raster-styles", tags=["raster-styles"])
    app.include_router(basemaps_pages.router, prefix="/basemaps", tags=["basemaps-pages"])
    app.include_router(processes.router, prefix="/processes", tags=["processes"])
    app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
    app.include_router(maps.router, prefix="/maps", tags=["maps"])
    # Human docs (HTML-only; excluded from OpenAPI)
    app.include_router(project_docs.router, tags=["project-docs"])

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    instrument_fastapi_app(app)
    return app


app = create_app()

