from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import basemaps_api, basemaps_pages, collection_styles, collections, items, jobs, maps, processes, root, styles, tiles
from app.core.config import get_settings
from app.services.bulk_queue import start_memory_consumer
from app.services.bulk_worker import process_bulk_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="OGC API - Features style service built with FastAPI and PostgreSQL.",
        lifespan=lifespan,
    )
    # Start in-process bulk consumer when using memory queue (so no Redis/worker needed)
    if settings.bulk_queue_type == "memory":
        start_memory_consumer(process_bulk_job)

    # OGC root: landing page (/) and conformance (/conformance). Must be first so GET / is landing.
    app.include_router(root.router, tags=["ogc"])

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
    app.include_router(styles.router, prefix="/styles", tags=["styles"])
    app.include_router(basemaps_api.router, prefix="/styles/basemaps", tags=["basemaps"])
    app.include_router(basemaps_pages.router, prefix="/basemaps", tags=["basemaps-pages"])
    app.include_router(processes.router, prefix="/processes", tags=["processes"])
    app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
    app.include_router(maps.router, prefix="/maps", tags=["maps"])

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()

