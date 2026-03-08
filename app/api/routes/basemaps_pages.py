"""HTML pages for basemaps: list, create, edit."""

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.html import html_response, wants_html
from app.crud import basemaps as basemaps_crud
from app.db.session import get_db

router = APIRouter(include_in_schema=False)

BASEMAP_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("", summary="List basemaps (HTML)")
async def list_basemaps_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all basemaps with links to edit and create."""
    if not wants_html(request):
        return RedirectResponse(url=_base_url(request) + "/basemaps?f=html", status_code=302)
    items = await basemaps_crud.list_basemaps(db)
    basemaps = [
        {
            "id": b.id,
            "name": b.name,
            "copyright": b.copyright or "",
            "min_zoom": b.min_zoom,
            "max_zoom": b.max_zoom,
            "tiles": b.tiles or [],
            "labels": b.labels,
            "sort_order": b.sort_order,
        }
        for b in items
    ]
    return html_response(
        "basemaps.html",
        base=_base_url(request),
        basemaps=basemaps,
    )


@router.get("/new", summary="New basemap form (HTML)")
async def new_basemap_page(request: Request):
    if not wants_html(request):
        return RedirectResponse(url=_base_url(request) + "/basemaps/new?f=html", status_code=302)
    return html_response(
        "basemap_edit.html",
        base=_base_url(request),
        basemap=None,
        is_new=True,
    )


@router.get("/{basemap_id}/edit", summary="Edit basemap form (HTML)")
async def edit_basemap_page(
    request: Request,
    basemap_id: str,
    db: AsyncSession = Depends(get_db),
):
    if not wants_html(request):
        return RedirectResponse(url=_base_url(request) + f"/basemaps/{basemap_id}/edit?f=html", status_code=302)
    b = await basemaps_crud.get_basemap(db, basemap_id)
    if not b:
        return RedirectResponse(url=_base_url(request) + "/basemaps?f=html", status_code=302)
    basemap = {
        "id": b.id,
        "name": b.name,
        "copyright": b.copyright or "",
        "min_zoom": b.min_zoom,
        "max_zoom": b.max_zoom,
        "tiles": b.tiles or [],
        "labels": b.labels or "",
        "sort_order": b.sort_order,
    }
    return html_response(
        "basemap_edit.html",
        base=_base_url(request),
        basemap=basemap,
        is_new=False,
    )


def _parse_tiles(tiles_str: str) -> list[str]:
    """Parse tiles from textarea: one URL per line or comma-separated."""
    if not tiles_str or not tiles_str.strip():
        return []
    urls = []
    for line in tiles_str.replace(",", "\n").splitlines():
        u = line.strip()
        if u:
            urls.append(u)
    return urls


@router.post("", summary="Create basemap (form submit)")
async def create_basemap_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    id: str = Form(..., alias="id"),
    name: str = Form(...),
    copyright: str = Form(""),
    min_zoom: int = Form(0),
    max_zoom: int = Form(22),
    tiles: str = Form(..., description="One tile URL per line"),
    labels: str = Form(""),
    sort_order: int = Form(0),
):
    """Create basemap from form and redirect to list."""
    base = _base_url(request)
    if not BASEMAP_ID_PATTERN.match(id):
        return RedirectResponse(url=base + "/basemaps/new?f=html&error=invalid_id", status_code=302)
    existing = await basemaps_crud.get_basemap(db, id)
    if existing:
        return RedirectResponse(url=base + "/basemaps/new?f=html&error=exists", status_code=302)
    tile_list = _parse_tiles(tiles)
    if not tile_list:
        return RedirectResponse(url=base + "/basemaps/new?f=html&error=no_tiles", status_code=302)
    await basemaps_crud.create_basemap(
        db,
        id=id.strip(),
        name=name.strip(),
        copyright=copyright.strip() or None,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tiles=tile_list,
        labels=labels.strip() or None,
        sort_order=sort_order,
    )
    return RedirectResponse(url=base + "/basemaps?f=html", status_code=303)


@router.post("/{basemap_id}", summary="Update basemap (form submit)")
async def update_basemap_form(
    request: Request,
    basemap_id: str,
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    copyright: str = Form(""),
    min_zoom: int = Form(0),
    max_zoom: int = Form(22),
    tiles: str = Form(...),
    labels: str = Form(""),
    sort_order: int = Form(0),
):
    """Update basemap from form and redirect to list."""
    base = _base_url(request)
    tile_list = _parse_tiles(tiles)
    if not tile_list:
        return RedirectResponse(url=base + f"/basemaps/{basemap_id}/edit?f=html&error=no_tiles", status_code=302)
    updated = await basemaps_crud.update_basemap(
        db,
        basemap_id,
        name=name.strip(),
        copyright=copyright.strip() or None,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        tiles=tile_list,
        labels=labels.strip() or None,
        sort_order=sort_order,
    )
    if not updated:
        return RedirectResponse(url=base + "/basemaps?f=html", status_code=302)
    return RedirectResponse(url=base + "/basemaps?f=html", status_code=303)


@router.post("/{basemap_id}/delete", summary="Delete basemap (form submit)")
async def delete_basemap_form(
    request: Request,
    basemap_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete basemap and redirect to list."""
    base = _base_url(request)
    await basemaps_crud.delete_basemap(db, basemap_id)
    return RedirectResponse(url=base + "/basemaps?f=html", status_code=303)
