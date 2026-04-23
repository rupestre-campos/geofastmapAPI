"""Merge concurrent Titiler tile fetches that share the same Redis cache key (singleflight)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_pending: dict[str, asyncio.Future] = {}


async def await_tile_singleflight(
    cache_key: str,
    fetch_once: Callable[[], Awaitable[tuple[bytes, str, float, int]]],
) -> tuple[bytes, str, float, int]:
    """
    Only one caller runs fetch_once(); concurrent callers await the same result.

    Exceptions from fetch_once propagate to all waiters. The registry entry is always cleared.
    """
    loop = asyncio.get_running_loop()

    async with _lock:
        fut = _pending.get(cache_key)
        if fut is None:
            fut = loop.create_future()
            _pending[cache_key] = fut
            leader = True
        else:
            leader = False

    if leader:

        async def _run() -> None:
            try:
                result = await fetch_once()
                fut.set_result(result)
            except BaseException as e:
                fut.set_exception(e)
            finally:
                async with _lock:
                    if _pending.get(cache_key) is fut:
                        _pending.pop(cache_key, None)

        asyncio.create_task(_run())
        logger.debug("titiler singleflight leader started key=%s...", cache_key[-16:])

    return await fut
