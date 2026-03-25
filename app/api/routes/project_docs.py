"""Project documentation pages (human-oriented, HTML-only).

These pages are intentionally excluded from OpenAPI schema so they don't clutter /docs.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import get_current_user_optional
from app.core.html import html_response, wants_html
from app.services.project_docs_search import search_project_docs

router = APIRouter(include_in_schema=False)

_DOCS_SEARCH_PER_PAGE = 8


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ctx(request: Request, current_user, **extra: object):
    ctx = {
        "base": _base_url(request),
        "username": current_user.username if current_user else None,
        "is_admin": current_user.is_admin if current_user else False,
        "search_query": "",
    }
    ctx.update(extra)
    return ctx


def _docs_search_url(base: str, q: str, page: int) -> str:
    return f"{base}/project-docs/search?{urlencode({'f': 'html', 'q': q, 'page': str(page)})}"


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


@router.get("/project-docs/auth-permissions", summary="Docs: auth & permissions")
async def docs_auth_permissions(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/auth_permissions.html", **_ctx(request, current_user))


@router.get("/project-docs/maps", summary="Docs: maps")
async def docs_maps(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/maps.html", **_ctx(request, current_user))


@router.get("/project-docs/basemaps", summary="Docs: basemaps")
async def docs_basemaps(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/basemaps.html", **_ctx(request, current_user))


@router.get("/project-docs/processing", summary="Docs: processing")
async def docs_processing(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/processing.html", **_ctx(request, current_user))


@router.get("/project-docs/deploy-cloudflare", summary="Docs: deploy from home (Cloudflare Tunnel)")
async def docs_deploy_cloudflare(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/deploy_cloudflare.html", **_ctx(request, current_user))


@router.get("/project-docs/deployment-performance", summary="Docs: deployment & performance")
async def docs_deployment_performance(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/deployment_performance.html", **_ctx(request, current_user))


@router.get("/project-docs/troubleshooting", summary="Docs: troubleshooting")
async def docs_troubleshooting(
    request: Request,
    current_user=Depends(get_current_user_optional),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    return html_response("project_docs/troubleshooting.html", **_ctx(request, current_user))


@router.get("/project-docs/search", summary="Docs: full-text search")
async def docs_search(
    request: Request,
    current_user=Depends(get_current_user_optional),
    q: str = "",
    page: int = Query(1, ge=1),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Use ?f=html")
    base = _base_url(request)
    q = (q or "").strip()
    if not q:
        return html_response(
            "project_docs/search_results.html",
            **_ctx(
                request,
                current_user,
                search_query=q,
                has_query=False,
                results=[],
                total=0,
                total_pages=0,
                page=1,
                per_page=_DOCS_SEARCH_PER_PAGE,
                prev_url=None,
                next_url=None,
            ),
        )
    results, total, total_pages = search_project_docs(q, page=page, per_page=_DOCS_SEARCH_PER_PAGE)
    prev_url = _docs_search_url(base, q, page - 1) if page > 1 and total_pages else None
    next_url = _docs_search_url(base, q, page + 1) if total_pages and page < total_pages else None
    return html_response(
        "project_docs/search_results.html",
        **_ctx(
            request,
            current_user,
            search_query=q,
            has_query=True,
            results=results,
            total=total,
            total_pages=total_pages,
            page=page,
            per_page=_DOCS_SEARCH_PER_PAGE,
            prev_url=prev_url,
            next_url=next_url,
        ),
    )

