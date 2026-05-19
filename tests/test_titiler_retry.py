"""Titiler HTTP retry/backoff helpers."""

import httpx
import pytest

from app.services import titiler_retry as tr


def test_backoff_exponential_with_cap():
    assert tr.titiler_retry_backoff_seconds(0, base=0.15, max_seconds=2.0) == 0.15
    assert tr.titiler_retry_backoff_seconds(1, base=0.15, max_seconds=2.0) == 0.3
    assert tr.titiler_retry_backoff_seconds(4, base=0.15, max_seconds=2.0) == 2.0


def test_retryable_includes_404_and_5xx():
    assert tr.titiler_http_status_retryable(404)
    assert tr.titiler_http_status_retryable(502)
    assert not tr.titiler_http_status_retryable(200)
    assert not tr.titiler_http_status_retryable(403)


@pytest.mark.asyncio
async def test_execute_retries_404_then_succeeds():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(404, request=httpx.Request("GET", "http://t/x"))
        return httpx.Response(200, request=httpx.Request("GET", "http://t/x"))

    resp, attempts = await tr.titiler_execute_with_retry(
        fetch,
        max_attempts=3,
        base_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    assert resp.status_code == 200
    assert attempts == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_execute_retries_request_error():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "http://t/x"))
        return httpx.Response(200, request=httpx.Request("GET", "http://t/x"))

    resp, attempts = await tr.titiler_execute_with_retry(
        fetch,
        max_attempts=2,
        base_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    assert resp.status_code == 200
    assert attempts == 2


@pytest.mark.asyncio
async def test_execute_returns_final_error_status():
    async def fetch():
        return httpx.Response(404, request=httpx.Request("GET", "http://t/x"))

    resp, attempts = await tr.titiler_execute_with_retry(
        fetch,
        max_attempts=2,
        base_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    assert resp.status_code == 404
    assert attempts == 2
