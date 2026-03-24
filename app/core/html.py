"""OGC API HTML representation: content negotiation and Jinja2 templates."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape


def wants_html(request: Request) -> bool:
    """True if client wants HTML (Accept: text/html or query ?f=html)."""
    if request.query_params.get("f", "").lower() == "html":
        return True
    accept = request.headers.get("accept", "")
    accept_l = accept.lower()
    # Typical fetch(..., { headers: { Accept: 'application/json' } }) — not a document navigation.
    if accept_l.strip().startswith("application/json"):
        return False
    return "text/html" in accept and (
        "application/json" not in accept or accept.split(",")[0].strip().lower().startswith("text/html")
    )


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


_env: Environment | None = None


def get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(_templates_dir()),
            autoescape=select_autoescape(("html", "xml")),
        )
    return _env


def render_html(template_name: str, **context: object) -> str:
    return get_env().get_template(template_name).render(**context)


def html_response(template_name: str, **context: object) -> HTMLResponse:
    return HTMLResponse(render_html(template_name, **context))


def self_and_alternate_links(base: str, path: str, current_f: str | None = None) -> list[dict]:
    """Links for OGC: self (current representation), alternate (other). path without leading slash."""
    path = path.lstrip("/")
    self_json = f"{base}/{path}" if path else base
    self_html = f"{base}/{path}?f=html" if path else f"{base}/?f=html"
    if current_f == "html":
        return [
            {"href": self_html, "rel": "self", "type": "text/html"},
            {"href": self_json, "rel": "alternate", "type": "application/json"},
        ]
    return [
        {"href": self_json, "rel": "self", "type": "application/json"},
        {"href": self_html, "rel": "alternate", "type": "text/html"},
    ]
