#!/usr/bin/env python3
"""Standalone worker for mosaic planner compute jobs."""

from __future__ import annotations

import json
import sys
import asyncio

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.mosaic_plan_jobs import (
    MOSAIC_PLAN_QUEUE_KEY,
    set_mosaic_plan_job_error,
    set_mosaic_plan_job_result,
    set_mosaic_plan_job_status,
)


async def _run_job(payload: dict) -> None:
    from app.api.routes.mosaics import MosaicPlanBody, compute_mosaic_plan
    from app.crud import user as user_crud

    job_id = str(payload.get("job_id") or "")
    owner_id = int(payload.get("owner_id") or 0)
    body_raw = payload.get("body") or {}
    if not job_id or not owner_id or not isinstance(body_raw, dict):
        return
    set_mosaic_plan_job_status(job_id, "running", message="Computing mosaic plan")
    try:
        body = MosaicPlanBody(**body_raw)
    except Exception as e:
        set_mosaic_plan_job_error(job_id, f"Invalid payload: {e}")
        return
    async with AsyncSessionLocal() as db:
        user = await user_crud.get_user_by_id(db, owner_id)
    if user is None:
        set_mosaic_plan_job_error(job_id, "User not found")
        return
    try:
        result = await compute_mosaic_plan(body, user)
        set_mosaic_plan_job_result(job_id, result)
    except Exception as e:
        set_mosaic_plan_job_error(job_id, str(e) or type(e).__name__)


async def _worker_loop() -> None:
    settings = get_settings()
    if settings.mosaic_queue_type != "redis":
        print("Set MOSAIC_QUEUE_TYPE=redis for mosaic worker.", file=sys.stderr)
        sys.exit(1)
    max_concurrent = max(1, int(getattr(settings, "mosaic_worker_max_concurrent", 1) or 1))
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    print(f"Mosaic worker started. Waiting for jobs... (max_concurrent={max_concurrent})", flush=True)
    active: set[asyncio.Task] = set()
    while True:
        if len(active) >= max_concurrent:
            done, pending = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            active = set(pending)
            for t in done:
                try:
                    await t
                except Exception:
                    # _run_job handles and stores job errors; never fail worker loop on one task.
                    pass
            continue
        # Redis client is sync; run BRPOP in a thread but keep one async event loop for the worker.
        item = await asyncio.to_thread(r.brpop, MOSAIC_PLAN_QUEUE_KEY, 5)
        if not item:
            continue
        _key, raw = item
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        t = asyncio.create_task(_run_job(payload))
        active.add(t)


def main() -> None:
    asyncio.run(_worker_loop())


if __name__ == "__main__":
    main()

