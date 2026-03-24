"""Prevent shared caches (CDN / browser) from storing personalized HTML.

Server-rendered pages include nav (Login vs username). Without these headers,
a CDN may cache the anonymous HTML and serve it to every visitor — including
after login — until the cache entry expires.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class PrivateHtmlCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" not in ct.lower():
            return response
        # Browsers: do not store personalized HTML in shared caches.
        response.headers["Cache-Control"] = "private, no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Cookie, Authorization"
        # Cloudflare edge: separate directive so the CDN does not keep a copy even if a dashboard
        # "Cache Rule" would otherwise treat the response as cache-eligible.
        # See https://developers.cloudflare.com/cache/concepts/cache-control/
        response.headers["CDN-Cache-Control"] = "no-store"
        return response
