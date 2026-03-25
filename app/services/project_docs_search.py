"""Full-text search over project documentation HTML templates (docs_content blocks)."""

from __future__ import annotations

import html as html_module
import re
from functools import lru_cache
from pathlib import Path

# (template path under app/templates/, URL path without ?f=html)
_DOC_PAGES: list[tuple[str, str]] = [
    ("project_docs/index.html", "project-docs"),
    ("project_docs/collections_items.html", "project-docs/collections-items"),
    ("project_docs/tiles.html", "project-docs/tiles"),
    ("project_docs/jobs.html", "project-docs/jobs"),
    ("project_docs/style_editor.html", "project-docs/style-editor"),
    ("project_docs/maps.html", "project-docs/maps"),
    ("project_docs/basemaps.html", "project-docs/basemaps"),
    ("project_docs/processing.html", "project-docs/processing"),
    ("project_docs/auth_permissions.html", "project-docs/auth-permissions"),
    ("project_docs/deploy_cloudflare.html", "project-docs/deploy-cloudflare"),
    ("project_docs/deployment_performance.html", "project-docs/deployment-performance"),
    ("project_docs/troubleshooting.html", "project-docs/troubleshooting"),
]

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"


def _extract_block(raw: str, block_name: str) -> str:
    m = re.search(
        r"{%\s*block\s+" + re.escape(block_name) + r"\s*%}(.*?){%\s*endblock\s*%}",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _html_to_plain(fragment: str) -> str:
    fragment = re.sub(r"{%.*?%}", " ", fragment, flags=re.DOTALL)
    fragment = re.sub(r"{{.*?}}", " ", fragment, flags=re.DOTALL)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html_module.unescape(fragment)
    fragment = re.sub(r"\s+", " ", fragment).strip()
    return fragment


def _page_title(raw: str) -> str:
    t = _extract_block(raw, "title")
    t = re.sub(r"\s*[–—-]\s*Documentation.*$", "", t, flags=re.IGNORECASE).strip()
    if t:
        return t
    m = re.search(r'class="card-title"[^>]*>([^<]+)', raw)
    if m:
        return html_module.unescape(m.group(1)).strip()
    return "Documentation"


def _load_one_doc(rel_path: str, route: str) -> dict:
    path = _TEMPLATES_ROOT / rel_path
    raw = path.read_text(encoding="utf-8")
    body = _extract_block(raw, "docs_content")
    text = _html_to_plain(body) if body else _html_to_plain(raw)
    title = _page_title(raw)
    return {
        "route": route,
        "title": title,
        "text": text,
        "text_lower": text.lower(),
    }


@lru_cache
def _index() -> tuple[dict, ...]:
    return tuple(_load_one_doc(rel, route) for rel, route in _DOC_PAGES)


def _tokenize_query(q: str) -> list[str]:
    q = (q or "").strip()
    if not q:
        return []
    # Words, numbers, path segments; drop very short noise tokens
    parts = re.findall(r"[\w./:-]{2,}", q, flags=re.UNICODE)
    if not parts:
        parts = [q.lower()] if len(q) >= 2 else []
    return [p.lower() for p in parts]


def _score_doc(doc: dict, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    title_l = doc["title"].lower()
    text_l = doc["text_lower"]
    score = 0.0
    for tok in tokens:
        if tok in title_l:
            score += 18.0
            score += 6.0 * title_l.count(tok)
        c = text_l.count(tok)
        if c:
            score += 4.0 * min(c, 20)
            score += 2.0 * min(len(tok), 12) ** 0.3
    return score


def _snippet_html(text: str, tokens: list[str], max_len: int = 220) -> str:
    if not text:
        return ""
    tl = text.lower()
    best_i = -1
    for tok in sorted(set(tokens), key=len, reverse=True):
        i = tl.find(tok)
        if i >= 0:
            best_i = i
            break
    if best_i < 0:
        best_i = 0
    pad = max_len // 2
    start = max(0, best_i - pad)
    end = min(len(text), start + max_len)
    if end - start < max_len and start > 0:
        start = max(0, end - max_len)
    chunk = text[start:end]
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    esc = html_module.escape(chunk)
    for tok in sorted(set(t for t in tokens if len(t) >= 2), key=len, reverse=True):
        if not re.match(r"^[\w./:-]+$", tok):
            continue
        esc = re.sub(
            "(" + re.escape(tok) + ")",
            r"<mark>\1</mark>",
            esc,
            flags=re.IGNORECASE,
        )
    return prefix + esc + suffix


def search_project_docs(
    query: str,
    *,
    page: int = 1,
    per_page: int = 8,
) -> tuple[list[dict], int, int]:
    """Return (result dicts for one page, total matching docs, total_pages)."""
    tokens = _tokenize_query(query)
    page = max(1, page)
    if not tokens:
        return [], 0, 0

    scored: list[tuple[float, dict]] = []
    for doc in _index():
        s = _score_doc(doc, tokens)
        if s > 0:
            scored.append((s, doc))
    scored.sort(key=lambda x: (-x[0], x[1]["title"].lower()))

    total = len(scored)
    total_pages = (total + per_page - 1) // per_page if total else 0
    if total_pages and page > total_pages:
        page = total_pages
    elif not total_pages:
        return [], 0, 0

    start = (page - 1) * per_page
    slice_ = scored[start : start + per_page]

    out: list[dict] = []
    for score, doc in slice_:
        out.append(
            {
                "route": doc["route"],
                "title": doc["title"],
                "score": round(score, 2),
                "snippet_html": _snippet_html(doc["text"], tokens),
            }
        )
    return out, total, total_pages
