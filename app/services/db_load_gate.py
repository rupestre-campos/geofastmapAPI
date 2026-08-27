"""In-process limits on concurrent heavy Postgres reads (dynamic tiles, big items lists).

MapLibre can open dozens of tile requests at once. Without a gate they each check out a
pool connection and hold it for up to statement_timeout, starving the rest of the API.

Each Uvicorn worker has its own gate (not cluster-wide). Prefer failing fast (empty tile /
503) over queuing forever when the worker is already busy.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from starlette.requests import Request

from app.core.config import get_settings

T = TypeVar("T")


class DbLoadOverloaded(Exception):
    """Raised when the gate wait budget is exceeded."""


@dataclass
class _Gate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: int = 0
    waiters: deque[asyncio.Event] = field(default_factory=deque)


_GATES: dict[str, _Gate] = {}


def _gate(name: str) -> _Gate:
    g = _GATES.get(name)
    if g is None:
        g = _Gate()
        _GATES[name] = g
    return g


async def _run_gated(
    *,
    name: str,
    max_concurrent: int,
    wait_seconds: float,
    request: Request | None,
    fn: Callable[[], Awaitable[T]],
    on_overload: Callable[[], Awaitable[T]] | None = None,
) -> T:
    if max_concurrent <= 0:
        return await fn()

    gate = _gate(name)
    ev: asyncio.Event | None = None

    async with gate.lock:
        if gate.active < max_concurrent:
            gate.active += 1
            entered = True
        else:
            entered = False
            ev = asyncio.Event()
            gate.waiters.appendleft(ev)

    async def _release() -> None:
        async with gate.lock:
            gate.active = max(0, gate.active - 1)
            if gate.waiters:
                gate.waiters.popleft().set()

    if entered:
        try:
            return await fn()
        finally:
            await _release()

    assert ev is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.05, float(wait_seconds))
    try:
        while not ev.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                async with gate.lock:
                    try:
                        gate.waiters.remove(ev)
                    except ValueError:
                        pass
                if on_overload is not None:
                    return await on_overload()
                raise DbLoadOverloaded(name)
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(0.2, remaining))
            except asyncio.TimeoutError:
                if request is not None:
                    try:
                        from app.services.titiler_cancel import raise_if_disconnected

                        await raise_if_disconnected(request)
                    except Exception:
                        async with gate.lock:
                            try:
                                gate.waiters.remove(ev)
                            except ValueError:
                                pass
                        raise
                continue
    except BaseException:
        async with gate.lock:
            try:
                gate.waiters.remove(ev)
            except ValueError:
                pass
        raise

    async with gate.lock:
        gate.active += 1

    try:
        return await fn()
    finally:
        await _release()


async def run_dynamic_tile_db(
    request: Request | None,
    fn: Callable[[], Awaitable[T]],
    *,
    on_overload: Callable[[], Awaitable[T]] | None = None,
) -> T:
    """Limit concurrent dynamic-tile DB/MVT work per API worker."""
    s = get_settings()
    return await _run_gated(
        name="dyn_tile",
        max_concurrent=int(getattr(s, "tiles_dynamic_max_concurrent", 2) or 2),
        wait_seconds=float(getattr(s, "tiles_dynamic_gate_wait_seconds", 1.5) or 1.5),
        request=request,
        fn=fn,
        on_overload=on_overload,
    )


async def run_items_list_db(
    request: Request | None,
    fn: Callable[[], Awaitable[T]],
) -> T:
    """Limit concurrent heavy items-list queries so one slow layer cannot monopolize the pool."""
    s = get_settings()
    return await _run_gated(
        name="items_list",
        max_concurrent=int(getattr(s, "items_list_max_concurrent", 3) or 3),
        wait_seconds=float(getattr(s, "items_list_gate_wait_seconds", 2.0) or 2.0),
        request=request,
        fn=fn,
        on_overload=None,
    )
