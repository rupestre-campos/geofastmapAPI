"""Cancel in-flight Titiler HTTP reads when the browser disconnects (e.g. MapLibre tile abort).

Used on tile hot paths. When combined with singleflight, all waiters for the same cache key
share one upstream fetch; if the leader's client disconnects mid-fetch, the fetch is
cancelled and waiters get ClientDisconnected (clients typically retry the tile).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from starlette.requests import Request

logger = logging.getLogger(__name__)


class ClientDisconnected(Exception):
    """ASGI client closed the connection before the tile response was sent."""


async def raise_if_disconnected(request: Request) -> None:
    if await request.is_disconnected():
        raise ClientDisconnected()


async def titiler_get_cancel_on_disconnect(
    request: Request,
    client: httpx.AsyncClient,
    url: str,
    *,
    params=None,
    headers: dict[str, str] | None = None,
    poll_interval: float = 0.2,
) -> httpx.Response:
    """
    Run client.get while polling request.is_disconnected().
    On disconnect, cancel the httpx task and raise ClientDisconnected.
    """
    hdrs = dict(headers) if headers else {}
    task: asyncio.Task = asyncio.create_task(
        client.get(url, params=params, headers=hdrs)
    )
    try:
        while True:
            done, _ = await asyncio.wait(
                {task},
                timeout=poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return await task
            if await request.is_disconnected():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, httpx.RequestError, Exception):
                    pass
                logger.debug("titiler fetch cancelled: client disconnected")
                raise ClientDisconnected()
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except Exception:
            pass
        raise ClientDisconnected() from None
