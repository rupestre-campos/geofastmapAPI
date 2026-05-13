"""Guardrails for server-side HTTP to URLs controlled by admins (SSRF mitigation)."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class UnsafeOutboundUrlError(ValueError):
    """Raised when a URL must not be fetched from the API host (SSRF / internal network)."""


def validate_public_http_url(url: str, *, require_https: bool = False) -> None:
    """
    Reject obviously unsafe URLs for outbound requests (link-local, loopback, RFC1918, etc.).

    Literal IP hosts are checked. Non-IP hostnames are allowed (DNS pinning is a separate hardening step).
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeOutboundUrlError("URL is empty")
    try:
        parsed = urlparse(raw)
    except Exception as e:
        raise UnsafeOutboundUrlError("URL parse failed") from e
    scheme = (parsed.scheme or "").lower()
    if require_https:
        if scheme != "https":
            raise UnsafeOutboundUrlError("URL must use https")
    elif scheme not in ("http", "https"):
        raise UnsafeOutboundUrlError("URL scheme must be http or https")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise UnsafeOutboundUrlError("URL has no host")

    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeOutboundUrlError("localhost is not allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved or ip.is_multicast:
        raise UnsafeOutboundUrlError("IP in disallowed range")
