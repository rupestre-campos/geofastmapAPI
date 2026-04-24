"""In-process limit on concurrent upstream Titiler HTTP calls (per API worker).

When the cap is reached, additional callers wait in a LIFO deque (newest waiters are
released first on slot free) so recent map pan/zoom tiles are favored over older backlog.
Each Uvicorn/gunicorn worker has its own gate (not cluster-wide).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Awaitable, Callable, TypeVar

from starlette.requests import Request

from app.core.config import get_settings
from app.services.titiler_cancel import raise_if_disconnected

T = TypeVar("T")

_lock = asyncio.Lock()
_active: int = 0
# Newest waiters on the left; on release we popleft() → wake most recently queued (LIFO).
_waiters: deque[asyncio.Event] = deque()


async def _release_slot() -> None:
    max_c = int(get_settings().titiler_upstream_max_concurrent)
    if max_c <= 0:
        return
    global _active
    async with _lock:
        _active -= 1
        if _waiters:
            _waiters.popleft().set()


async def _abandon_waiter(ev: asyncio.Event) -> None:
    async with _lock:
        try:
            _waiters.remove(ev)
        except ValueError:
            pass


async def titiler_upstream_gate_run(
    request: Request | None,
    fn: Callable[[], Awaitable[T]],
) -> T:
    """Run ``fn`` while holding one upstream Titiler slot (respecting ``titiler_upstream_max_concurrent``)."""
    max_c = int(get_settings().titiler_upstream_max_concurrent)
    if max_c <= 0:
        return await fn()

    global _active
    ev: asyncio.Event | None = None
    async with _lock:
        if _active < max_c:
            _active += 1
            entered_immediately = True
        else:
            entered_immediately = False
            ev = asyncio.Event()
            _waiters.appendleft(ev)

    if entered_immediately:
        try:
            return await fn()
        finally:
            await _release_slot()

    assert ev is not None
    try:
        while not ev.is_set():
            try:
                await asyncio.wait_for(ev.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                if request is not None:
                    await raise_if_disconnected(request)
                continue
    except BaseException:
        await _abandon_waiter(ev)
        raise

    async with _lock:
        _active += 1

    try:
        return await fn()
    finally:
        await _release_slot()
