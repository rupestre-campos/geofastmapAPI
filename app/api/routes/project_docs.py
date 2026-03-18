"""Project documentation pages (human-oriented, HTML-only).

These pages are intentionally excluded from OpenAPI schema so they don't clutter /docs.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_current_user_optional
from app.core.html import html_response, wants_html

router = APIRouter(include_in_schema=False)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ctx(request: Request, current_user):
    return {
        "base": _base_url(request),
        "username": current_user.username if current_user else None,
        "is_admin": current_user.is_admin if current_user else False,
    }


@router.get("/project-docs", summary="Project docs home")
async def docs_index(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/index.html", **_ctx(request, current_user))


@router.get("/project-docs/tiles", summary="Docs: tiles")
async def docs_tiles(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/tiles.html", **_ctx(request, current_user))


@router.get("/project-docs/collections-items", summary="Docs: collections & items")
async def docs_collections_items(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/collections_items.html", **_ctx(request, current_user))


@router.get("/project-docs/jobs", summary="Docs: jobs")
async def docs_jobs(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/jobs.html", **_ctx(request, current_user))


@router.get("/project-docs/style-editor", summary="Docs: style editor")
async def docs_style_editor(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/style_editor.html", **_ctx(request, current_user))

