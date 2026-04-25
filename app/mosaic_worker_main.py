#!/usr/bin/env python3
"""Standalone worker for mosaic planner compute jobs."""

from __future__ import annotations

import json
import sys
import asyncio
import traceback

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.mosaic_plan_jobs import (
    MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY,
    MOSAIC_PLAN_QUEUE_KEY,
    MOSAIC_PLAN_SUBTASK_QUEUE_KEY,
    set_mosaic_plan_job_error,
    set_mosaic_plan_job_progress,
    set_mosaic_plan_job_result,
    set_mosaic_plan_job_status,
    set_mosaic_plan_subtask_result,
    set_mosaic_plan_subtask_status,
    set_mosaic_footprint_subtask_result,
    set_mosaic_footprint_subtask_status,
)


async def _run_job(payload: dict) -> None:
    from app.api.routes.mosaics import MosaicPlanBody, compute_mosaic_plan
    from app.crud import user as user_crud

    job_id = str(payload.get("job_id") or "")
    owner_id = int(payload.get("owner_id") or 0)
    body_raw = payload.get("body") or {}
    if not job_id or not owner_id or not isinstance(body_raw, dict):
        return
    print(f"[mosaic-parent] start job_id={job_id} owner_id={owner_id}", flush=True)
    set_mosaic_plan_job_status(job_id, "running", message="Computing mosaic plan")
    settings = get_settings()
    heartbeat_secs = max(2, int(settings.mosaic_job_heartbeat_seconds or 10))
    rounds_max = max(1, int(settings.mosaic_void_fill_max_rounds or 1))
    hb_stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not hb_stop.is_set():
            set_mosaic_plan_job_progress(
                job_id,
                phase="planning",
                rounds_max=rounds_max,
                retry_after_seconds=1,
            )
            try:
                await asyncio.wait_for(hb_stop.wait(), timeout=heartbeat_secs)
            except asyncio.TimeoutError:
                continue

    hb_task = asyncio.create_task(_heartbeat())
    try:
        body = MosaicPlanBody(**body_raw)
    except Exception as e:
        hb_stop.set()
        await hb_task
        set_mosaic_plan_job_error(job_id, f"Invalid payload: {e}")
        return
    async with AsyncSessionLocal() as db:
        user = await user_crud.get_user_by_id(db, owner_id)
    if user is None:
        hb_stop.set()
        await hb_task
        set_mosaic_plan_job_error(job_id, "User not found")
        return
    try:
        setattr(user, "_mosaic_job_id", job_id)
        result = await compute_mosaic_plan(body, user, allow_distributed=True)
        set_mosaic_plan_job_progress(
            job_id,
            phase="finalizing",
            round=result.get("void_fill_rounds"),
            features_seen=result.get("stac_feature_pool_size"),
            retry_after_seconds=2,
        )
        hb_stop.set()
        await hb_task
        errs = list(result.get("stac_errors") or [])
        st = "completed_with_errors" if errs else "completed"
        set_mosaic_plan_job_result(job_id, result, status=st)
        print(
            f"[mosaic-parent] done job_id={job_id} status={st} rounds={result.get('void_fill_rounds')} pool={result.get('stac_feature_pool_size')}",
            flush=True,
        )
    except Exception as e:
        hb_stop.set()
        await hb_task
        print(f"[mosaic-parent] error job_id={job_id} err={type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        set_mosaic_plan_job_error(job_id, str(e) or type(e).__name__)


async def _run_subtask(payload: dict) -> None:
    from app.services.mosaic_plan_distributed import execute_subtask_payload

    task_id = str(payload.get("task_id") or "")
    job_id = str(payload.get("job_id") or "")
    round_idx = int(payload.get("round_idx") or 0)
    body = payload.get("payload") or {}
    if not task_id or not job_id or not isinstance(body, dict):
        return
    print(f"[mosaic-subtask] start task_id={task_id} job_id={job_id} round={round_idx}", flush=True)
    set_mosaic_plan_subtask_status(task_id, "running")
    try:
        result = await execute_subtask_payload(body)
        errs = list(result.get("errors") or [])
        status = "completed_with_errors" if errs else "completed"
        set_mosaic_plan_subtask_result(
            task_id,
            job_id=job_id,
            round_idx=round_idx,
            result=result,
            status=status,
        )
        print(
            f"[mosaic-subtask] done task_id={task_id} job_id={job_id} round={round_idx} status={status} features={len(list(result.get('features') or []))}",
            flush=True,
        )
    except Exception as e:
        print(f"[mosaic-subtask] error task_id={task_id} job_id={job_id} round={round_idx} err={type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        set_mosaic_plan_subtask_result(
            task_id,
            job_id=job_id,
            round_idx=round_idx,
            result={"features": [], "errors": [{"detail": str(e) or type(e).__name__}]},
            status="failed",
        )


async def _run_footprint_subtask(payload: dict) -> None:
    from app.services.mosaic_preview_footprint import fetch_footprint_display_geojson

    task_id = str(payload.get("task_id") or "")
    job_id = str(payload.get("job_id") or "")
    batch_idx = int(payload.get("batch_idx") or 0)
    path = payload.get("path")
    url = str(payload.get("url") or "")
    bbox4 = payload.get("bbox4") or []
    if not task_id or not job_id or not isinstance(path, list) or len(bbox4) < 4:
        return
    print(f"[mosaic-footprint] start task_id={task_id} job_id={job_id} batch={batch_idx}", flush=True)
    set_mosaic_footprint_subtask_status(task_id, "running")
    try:
        try:
            bb = [float(bbox4[i]) for i in range(4)]
        except (TypeError, ValueError, IndexError):
            set_mosaic_footprint_subtask_result(
                task_id,
                job_id=job_id,
                batch_idx=batch_idx,
                path=path,
                footprint_display=None,
                status="completed",
            )
            return
        geo = await fetch_footprint_display_geojson(url, bb)
        st = "completed" if geo is not None else "completed_with_errors"
        set_mosaic_footprint_subtask_result(
            task_id,
            job_id=job_id,
            batch_idx=batch_idx,
            path=path,
            footprint_display=geo,
            status=st,
        )
        print(
            f"[mosaic-footprint] done task_id={task_id} job_id={job_id} batch={batch_idx} status={st}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[mosaic-footprint] error task_id={task_id} job_id={job_id} batch={batch_idx} err={type(e).__name__}: {e}",
            flush=True,
        )
        print(traceback.format_exc(), flush=True)
        set_mosaic_footprint_subtask_result(
            task_id,
            job_id=job_id,
            batch_idx=batch_idx,
            path=path,
            footprint_display=None,
            status="failed",
        )


async def _worker_loop() -> None:
    settings = get_settings()
    if settings.mosaic_queue_type != "redis":
        print("Set MOSAIC_QUEUE_TYPE=redis for mosaic worker.", file=sys.stderr)
        sys.exit(1)
    # 0 = never claim parent jobs from geofastmap:mosaic_plan_queue (shard-only worker).
    _mc = int(getattr(settings, "mosaic_worker_max_concurrent", 1) or 0)
    max_concurrent = 0 if _mc <= 0 else _mc
    subtask_workers = max(0, int(getattr(settings, "mosaic_subjob_worker_concurrency", 2) or 0))
    fp_sub_cfg = max(0, int(getattr(settings, "mosaic_footprint_subjob_worker_concurrency", 0) or 0))
    footprint_budget = fp_sub_cfg if fp_sub_cfg > 0 else subtask_workers
    consume_while_parent = bool(getattr(settings, "mosaic_subjob_consume_subtasks_while_parent_active", True))
    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True)
    print(
        "Mosaic worker started. Waiting for jobs... "
        f"(max_concurrent={max_concurrent}, subtask_workers={subtask_workers}, footprint_budget={footprint_budget})",
        flush=True,
    )
    active_parent: set[asyncio.Task] = set()
    active_subtask: set[asyncio.Task] = set()
    active_footprint: set[asyncio.Task] = set()

    def _dispatch_brpop_item(key: str, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        if key == MOSAIC_PLAN_SUBTASK_QUEUE_KEY:
            print("[mosaic-worker] dequeued subtask", flush=True)
            active_subtask.add(asyncio.create_task(_run_subtask(payload)))
        elif key == MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY:
            print("[mosaic-worker] dequeued footprint subtask", flush=True)
            active_footprint.add(asyncio.create_task(_run_footprint_subtask(payload)))
        else:
            print("[mosaic-worker] dequeued parent job", flush=True)
            active_parent.add(asyncio.create_task(_run_job(payload)))

    while True:
        while True:
            st_full = (
                subtask_workers > 0
                and bool(getattr(settings, "mosaic_subjob_queue_enabled", False))
                and len(active_subtask) >= subtask_workers
            )
            fp_full = (
                footprint_budget > 0
                and bool(getattr(settings, "mosaic_footprint_distributed_enabled", False))
                and len(active_footprint) >= footprint_budget
            )
            if not st_full and not fp_full:
                break
            wait_on: set[asyncio.Task] = set()
            if st_full:
                wait_on.update(active_subtask)
            if fp_full:
                wait_on.update(active_footprint)
            if not wait_on:
                break
            done, _pending = await asyncio.wait(wait_on, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    await t
                except Exception:
                    pass
                active_subtask.discard(t)
                active_footprint.discard(t)

        can_take_subtasks = (
            subtask_workers > 0
            and bool(getattr(settings, "mosaic_subjob_queue_enabled", False))
            and len(active_subtask) < subtask_workers
            and (consume_while_parent or len(active_parent) == 0)
        )
        can_take_footprints = (
            footprint_budget > 0
            and bool(getattr(settings, "mosaic_footprint_distributed_enabled", False))
            and len(active_footprint) < footprint_budget
            and (consume_while_parent or len(active_parent) == 0)
        )

        # While parent slots are full, still drain footprint/subtask queues first (BRPOP aux-only).
        if max_concurrent > 0 and len(active_parent) >= max_concurrent:
            aux_keys_only: list[str] = []
            if can_take_footprints:
                aux_keys_only.append(MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY)
            if can_take_subtasks:
                aux_keys_only.append(MOSAIC_PLAN_SUBTASK_QUEUE_KEY)
            if aux_keys_only:
                item_cap = await asyncio.to_thread(r.brpop, aux_keys_only, 5)
                if item_cap:
                    _dispatch_brpop_item(item_cap[0], item_cap[1])
                    continue
            done, pending = await asyncio.wait(active_parent, return_when=asyncio.FIRST_COMPLETED)
            active_parent = set(pending)
            for t in done:
                try:
                    await t
                except Exception:
                    pass
            continue

        # Prefer auxiliary queues in BRPOP key order so backlog on parent queue does not starve them.
        keys: list[str] = []
        if can_take_footprints:
            keys.append(MOSAIC_PLAN_FOOTPRINT_SUBTASK_QUEUE_KEY)
        if can_take_subtasks:
            keys.append(MOSAIC_PLAN_SUBTASK_QUEUE_KEY)
        if max_concurrent > 0:
            keys.append(MOSAIC_PLAN_QUEUE_KEY)
        if not keys:
            await asyncio.sleep(1)
            continue
        item = await asyncio.to_thread(r.brpop, keys, 5)
        if not item:
            continue
        _dispatch_brpop_item(item[0], item[1])


def main() -> None:
    asyncio.run(_worker_loop())


if __name__ == "__main__":
    main()

