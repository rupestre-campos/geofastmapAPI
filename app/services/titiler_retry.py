"""Retry Titiler HTTP requests with simple exponential backoff."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from app.core.config import get_settings

# 404 during Titiler restarts; 5xx / gateway errors during GDAL hiccups.
TITILER_RETRYABLE_HTTP_STATUS = frozenset({404, 500, 502, 503, 504})


def titiler_http_status_retryable(status_code: int) -> bool:
    return status_code in TITILER_RETRYABLE_HTTP_STATUS


def titiler_retry_backoff_seconds(attempt_idx: int, *, base: float, max_seconds: float) -> float:
    """Seconds to sleep before attempt ``attempt_idx + 1`` (0-based failed attempt)."""
    if attempt_idx < 0:
        return 0.0
    return min(max_seconds, base * (2**attempt_idx))


def _retry_settings() -> tuple[int, float, float]:
    settings = get_settings()
    max_attempts = max(1, int(getattr(settings, "titiler_retry_max_attempts", 3) or 3))
    base = max(0.05, float(getattr(settings, "titiler_retry_base_seconds", 0.15) or 0.15))
    max_backoff = max(base, float(getattr(settings, "titiler_retry_max_seconds", 2.0) or 2.0))
    return max_attempts, base, max_backoff


async def titiler_execute_with_retry(
    request_fn: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int | None = None,
    base_seconds: float | None = None,
    max_backoff_seconds: float | None = None,
    before_retry: Callable[[int, BaseException | httpx.Response], Awaitable[None]] | None = None,
) -> tuple[httpx.Response, int]:
    """
    Run ``request_fn`` until success or attempts exhausted.

    Retries on ``httpx.RequestError`` and retryable HTTP status codes (404, 5xx).
    Returns ``(response, attempts_used)``; the last response may still be an error status.
    """
    cfg_max, cfg_base, cfg_cap = _retry_settings()
    attempts_limit = max_attempts if max_attempts is not None else cfg_max
    backoff_base = base_seconds if base_seconds is not None else cfg_base
    backoff_cap = max_backoff_seconds if max_backoff_seconds is not None else cfg_cap

    attempts_used = 0
    last_exc: httpx.RequestError | None = None
    for attempt in range(attempts_limit):
        attempts_used += 1
        try:
            resp = await request_fn()
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt >= attempts_limit - 1:
                raise
            if before_retry:
                await before_retry(attempt, exc)
            await asyncio.sleep(
                titiler_retry_backoff_seconds(attempt, base=backoff_base, max_seconds=backoff_cap)
            )
            continue

        if titiler_http_status_retryable(resp.status_code) and attempt < attempts_limit - 1:
            if before_retry:
                await before_retry(attempt, resp)
            await asyncio.sleep(
                titiler_retry_backoff_seconds(attempt, base=backoff_base, max_seconds=backoff_cap)
            )
            continue
        return resp, attempts_used

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("titiler_execute_with_retry: no response")
