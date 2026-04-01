"""Shared httpx client for Titiler — connection reuse for high tile concurrency."""

from __future__ import annotations

import httpx

from app.core.config import get_settings

_client: httpx.AsyncClient | None = None


def get_titiler_http_client() -> httpx.AsyncClient:
    """Singleton AsyncClient with keep-alive to Titiler (or nginx in front of workers)."""
    global _client
    if _client is None:
        settings = get_settings()
        t = float(settings.stac_search_http_timeout_seconds)
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(t, connect=30.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=64),
            http2=False,
        )
    return _client


async def close_titiler_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
