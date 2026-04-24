"""Tests for in-process Titiler upstream concurrency gate (LIFO waiters)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.titiler_gate import titiler_upstream_gate_run


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch):
    import app.services.titiler_gate as tg

    tg._active = 0  # noqa: SLF001
    tg._waiters.clear()
    yield
    tg._active = 0  # noqa: SLF001
    tg._waiters.clear()


@pytest.mark.asyncio
async def test_gate_lifo_newest_runs_next(monkeypatch):
    monkeypatch.setattr(
        "app.services.titiler_gate.get_settings",
        lambda: SimpleNamespace(titiler_upstream_max_concurrent=1),
    )

    order: list[str] = []

    async def first():
        order.append("a_start")
        await asyncio.sleep(0.05)
        order.append("a_end")

    async def second():
        order.append("b_start")
        await asyncio.sleep(0.05)
        order.append("b_end")

    async def third():
        order.append("c_start")
        await asyncio.sleep(0.05)
        order.append("c_end")

    async def run():
        await titiler_upstream_gate_run(None, first)

    async def run_b():
        await titiler_upstream_gate_run(None, second)

    async def run_c():
        await titiler_upstream_gate_run(None, third)

    t1 = asyncio.create_task(run())
    await asyncio.sleep(0.001)
    t2 = asyncio.create_task(run_b())
    await asyncio.sleep(0.001)
    t3 = asyncio.create_task(run_c())
    await asyncio.gather(t1, t2, t3)

    # With max=1: a runs fully first. b and c wait; LIFO → c runs before b.
    assert order.index("c_start") < order.index("b_start")
    assert order.index("a_end") < order.index("c_start")
