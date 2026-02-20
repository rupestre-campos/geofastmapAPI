"""OGC API - Features root: landing page and conformance."""

from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.schemas.ogc import (
    CONFORMANCE_CORE,
    CONFORMANCE_GEOJSON,
    CONFORMANCE_OAS30,
    CONFORMANCE_P4_CREATE_REPLACE_DELETE,
    CONFORMANCE_P4_UPDATE,
    Conformance,
    LandingPage,
    Link,
)

router = APIRouter()


def _base_url(request: Request) -> str:
    """Return API base URL without trailing slash."""
    return str(request.base_url).rstrip("/")


@router.get(
    "/",
    response_model=LandingPage,
    summary="Landing page",
    description="OGC API - Features landing page (root). Required for client discovery.",
)
async def landing_page(request: Request) -> LandingPage:
    """OGC API - Features §7.2: Landing page at root."""
    base = _base_url(request)
    settings = get_settings()
    return LandingPage(
        title=settings.app_name,
        description="OGC API - Features service. Read and list feature collections and features (GeoJSON).",
        links=[
            Link(href=base + "/", rel="self", type="application/json"),
            Link(href=base + "/conformance", rel="conformance", type="application/json"),
            Link(href=base + "/collections", rel="data", type="application/json"),
            Link(
                href=base + "/openapi.json",
                rel="service-desc",
                type="application/vnd.oai.openapi+json;version=3.0",
            ),
            Link(href=base + "/docs", rel="service-doc", type="text/html"),
        ],
    )


@router.get(
    "/conformance",
    response_model=Conformance,
    summary="Conformance declaration",
    description="Declares which OGC API conformance classes this API implements.",
)
async def conformance() -> Conformance:
    """OGC API - Features §7.4: Conformance declaration."""
    return Conformance(
        conformsTo=[
            CONFORMANCE_CORE,
            CONFORMANCE_GEOJSON,
            CONFORMANCE_OAS30,
            CONFORMANCE_P4_CREATE_REPLACE_DELETE,
            CONFORMANCE_P4_UPDATE,
        ],
    )
