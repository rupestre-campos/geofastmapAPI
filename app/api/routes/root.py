"""OGC API - Features root: landing page and conformance."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import api_landing as api_landing_crud
from app.db.session import get_db
from app.schemas.api_landing import ApiLandingRead, ApiLandingUpdate
from app.schemas.ogc import (
    CONFORMANCE_CORE,
    CONFORMANCE_GEOJSON,
    CONFORMANCE_OAS30,
    CONFORMANCE_P4_CREATE_REPLACE_DELETE,
    CONFORMANCE_P4_UPDATE,
    CONFORMANCE_TILES_CORE,
    CONFORMANCE_TILES_GEODATA,
    Conformance,
    LandingPage,
    Link,
)

router = APIRouter()


@router.get("/health", summary="Health check", include_in_schema=False)
async def health() -> dict:
    """Returns 200 when the API is up (used by Docker healthcheck)."""
    return {"status": "ok"}


def _base_url(request: Request) -> str:
    """Return API base URL without trailing slash."""
    return str(request.base_url).rstrip("/")


def _landing_links(base: str) -> list:
    return [
        Link(href=base + "/", rel="self", type="application/json"),
        Link(href=base + "/?f=html", rel="alternate", type="text/html"),
        Link(href=base + "/conformance", rel="conformance", type="application/json"),
        Link(href=base + "/collections", rel="data", type="application/json"),
        Link(href=base + "/collections", rel="tiles", type="application/json", title="Collection tiles (TileJSON per collection)"),
        Link(href=base + "/styles", rel="styles", type="application/json", title="OGC API - Styles: public (global) styles"),
        Link(href=base + "/processes", rel="processes", type="application/json", title="OGC API - Processes (intersection, erase)"),
        Link(href=base + "/openapi.json", rel="service-desc", type="application/vnd.oai.openapi+json;version=3.0"),
        Link(href=base + "/api", rel="service-desc", type="application/vnd.oai.openapi+json;version=3.0"),
        Link(href=base + "/docs", rel="service-doc", type="text/html"),
    ]


@router.get(
    "/",
    summary="Landing page",
    description="OGC API - Features landing page (root). Use ?f=html or Accept: text/html for HTML.",
)
async def landing_page(request: Request, db: AsyncSession = Depends(get_db)):
    """OGC API - Features §7.2: Landing page at root. Title/description/contact from DB (user-editable)."""
    base = _base_url(request)
    links = _landing_links(base)
    info = await api_landing_crud.get_or_create_api_landing(db)
    link_dicts = [{"href": l.href, "rel": l.rel, "type": l.type or "application/json", "title": l.title} for l in links]
    if wants_html(request):
        return html_response(
            "landing.html",
            base=base,
            api_title=info.title,
            api_description=info.description or "",
            api_contact=info.contact or "",
            links=link_dicts,
        )
    return LandingPage(
        title=info.title,
        description=info.description or "",
        links=links,
    )


@router.get(
    "/api-info/edit",
    summary="Edit API info (HTML form)",
    description="Form to edit landing page title, description, and contact. Use ?f=html.",
)
async def api_info_edit_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Serve the edit-API-info form. HTML only."""
    if not wants_html(request):
        return RedirectResponse(url=_base_url(request) + "/api-info/edit?f=html", status_code=302)
    info = await api_landing_crud.get_or_create_api_landing(db)
    return html_response(
        "api_info_edit.html",
        base=_base_url(request),
        api_title=info.title,
        api_description=info.description or "",
        api_contact=info.contact or "",
    )


@router.get(
    "/style-editor",
    summary="Style editor (HTML)",
    description="Map with draw tools, basemaps, and style editor to test, load, save, delete styles or import JSON.",
)
async def style_editor_page(request: Request):
    """Serve the style editor page. HTML only."""
    if not wants_html(request):
        return RedirectResponse(url=_base_url(request) + "/style-editor?f=html", status_code=302)
    base = _base_url(request)
    settings = get_settings()
    return html_response(
        "style_editor.html",
        base=base,
        google_maps_api_key=settings.google_maps_api_key or "",
    )


@router.patch(
    "/api-info",
    response_model=ApiLandingRead,
    summary="Update API landing info",
    description="Update title, description, and/or contact. Stored in DB; no redeploy needed.",
)
async def api_info_update(
    payload: ApiLandingUpdate,
    db: AsyncSession = Depends(get_db),
) -> ApiLandingRead:
    """Update the API landing page content (title, description, contact)."""
    row = await api_landing_crud.update_api_landing(
        db,
        title=payload.title,
        description=payload.description,
        contact=payload.contact,
    )
    if not row:
        raise HTTPException(status_code=404, detail="API info not found")
    return ApiLandingRead(title=row.title, description=row.description, contact=row.contact)


@router.get(
    "/api",
    include_in_schema=False,
    summary="OpenAPI document (alternate path)",
    description="Same as /openapi.json. Some OGC clients (e.g. QGIS) expect the API definition at /api.",
)
async def openapi_at_api(request: Request) -> JSONResponse:
    """Serve OpenAPI 3.0 document at /api for client compatibility."""
    return JSONResponse(request.app.openapi())


@router.get(
    "/conformance",
    summary="Conformance declaration",
    description="Declares which OGC API conformance classes this API implements. Use ?f=html for HTML.",
)
async def conformance(request: Request):
    """OGC API - Features §7.4: Conformance declaration."""
    conforms_to = [
        CONFORMANCE_CORE,
        CONFORMANCE_GEOJSON,
        CONFORMANCE_OAS30,
        CONFORMANCE_P4_CREATE_REPLACE_DELETE,
        CONFORMANCE_P4_UPDATE,
        CONFORMANCE_TILES_CORE,
        CONFORMANCE_TILES_GEODATA,
    ]
    if wants_html(request):
        return html_response(
            "conformance.html",
            base=_base_url(request),
            conforms_to=conforms_to,
        )
    return Conformance(conformsTo=conforms_to)
