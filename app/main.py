import logging
import secrets
from urllib.parse import quote

from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.html import wants_html

from app.api.routes import auth, basemaps_api, basemaps_pages, collection_styles, collections, items, jobs, maps, processes, project_docs, root, styles, tiles
from app.core.config import get_settings
from app.utils.geometry_limits import GeometryTooLargeError
from app.services.bulk_queue import start_memory_consumer
from app.services.bulk_worker import process_bulk_job

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db.session import AsyncSessionLocal
    from app.crud import user as user_crud
    from app.models.user import User
    from sqlalchemy import select

    # Seed default admin user if no users exist
    async with AsyncSessionLocal() as session:
        r = await session.execute(select(User).limit(1))
        if r.scalar_one_or_none() is None:
            settings = get_settings()
            await user_crud.create_user(
                session,
                settings.auth_default_admin_username,
                settings.auth_default_admin_password,
                is_admin=True,
                must_change_password=True,
            )
    yield


async def http_exception_redirect_to_login(request: Request, exc):
    """On 401/403 for HTML requests, redirect to login with next=current URL so user can sign in and return."""
    if exc.status_code not in (401, 403):
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
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="OGC API - Features style service built with FastAPI and PostgreSQL.",
        lifespan=lifespan,
    )
    app.add_exception_handler(HTTPException, http_exception_redirect_to_login)

    async def geometry_too_large_handler(request: Request, exc: GeometryTooLargeError):
        from fastapi import status

        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    app.add_exception_handler(GeometryTooLargeError, geometry_too_large_handler)
    session_secret = settings.auth_secret_key
    if not session_secret:
        session_secret = secrets.token_hex(32)
        logger.info(
            "AUTH_SECRET_KEY not set; using an auto-generated session key. "
            "Sessions will be invalidated on restart. Set AUTH_SECRET_KEY for production."
        )
    app.add_middleware(SessionMiddleware, secret_key=session_secret)
    # Start in-process bulk consumer when using memory queue (so no Redis/worker needed)
    if settings.bulk_queue_type == "memory":
        start_memory_consumer(process_bulk_job)

    # OGC root: landing page (/) and conformance (/conformance). Must be first so GET / is landing.
    app.include_router(root.router, tags=["ogc"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])

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
    app.include_router(tiles.router, prefix="/collections", tags=["tiles"])
    app.include_router(collection_styles.router, prefix="/collections", tags=["styles"])
    # Register /styles/basemaps before /styles so it is not captured by /styles/{style_id}.
    app.include_router(basemaps_api.router, prefix="/styles/basemaps", tags=["basemaps"])
    app.include_router(styles.router, prefix="/styles", tags=["styles"])
    app.include_router(basemaps_pages.router, prefix="/basemaps", tags=["basemaps-pages"])
    app.include_router(processes.router, prefix="/processes", tags=["processes"])
    app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
    app.include_router(maps.router, prefix="/maps", tags=["maps"])
    # Human docs (HTML-only; excluded from OpenAPI)
    app.include_router(project_docs.router, tags=["project-docs"])

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()

