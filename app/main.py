from fastapi import FastAPI

from app.api.routes import collections, items, root
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="OGC API - Features style service built with FastAPI and PostgreSQL.",
    )

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

    return app


app = create_app()

