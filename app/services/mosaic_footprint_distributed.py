"""Redis-distributed thumbnail footprint_display for async mosaic plan jobs."""

from __future__ import annotations

from app.core.config import get_settings
from app.services.mosaic_plan_jobs import (
    await_mosaic_footprint_subtask_results,
    clear_mosaic_footprint_subtask_results,
    enqueue_mosaic_footprint_display_subtask,
    set_mosaic_plan_job_progress,
)
from app.services.mosaic_preview_footprint import (
    apply_footprint_display_patches,
    attach_footprint_displays_to_plan_result,
    build_footprint_display_work_specs,
)


async def try_attach_footprints_distributed(
    job_id: str,
    owner_id: int,
    result: dict[str, Any],
    plan_features: list[dict[str, Any]],
) -> None:
    """
    Enqueue footprint_display subtasks in waves; merge results into ``result``.
    On shortfall (timeouts / no workers), falls back to in-process attach.
    """
    s = get_settings()
    max_items = max(1, int(s.mosaic_footprint_max_items or 200))
    specs = build_footprint_display_work_specs(result, plan_features, max_items=max_items)
    if not specs:
        return
    wave = max(1, int(getattr(s, "mosaic_footprint_distributed_wave", 16) or 16))
    timeout_sec = max(10, int(getattr(s, "mosaic_footprint_distributed_timeout_seconds", 300) or 300))
    ttl = max(600, int(s.mosaic_subjob_result_ttl_seconds or 3600))
    total = len(specs)
    set_mosaic_plan_job_progress(
        job_id,
        phase="footprinting",
        round=1,
        rounds_max=1,
        children_total=total,
        children_done=0,
        retry_after_seconds=1,
    )
    batch_seq = 0
    done_count = 0
    fallback_local = False
    for i in range(0, len(specs), wave):
        batch_idx = batch_seq
        batch_seq += 1
        wave_specs = specs[i : i + wave]
        clear_mosaic_footprint_subtask_results(job_id, batch_idx)
        for spec in wave_specs:
            enqueue_mosaic_footprint_display_subtask(
                job_id=job_id,
                owner_id=owner_id,
                batch_idx=batch_idx,
                path=list(spec["path"]),
                url=str(spec["url"]),
                bbox4=list(spec["bbox4"]),
                ttl_seconds=ttl,
            )
        rows = await await_mosaic_footprint_subtask_results(
            job_id=job_id,
            batch_idx=batch_idx,
            expected_count=len(wave_specs),
            timeout_seconds=timeout_sec,
        )
        patches: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("status") or "") not in ("completed", "completed_with_errors"):
                continue
            path = row.get("path")
            geo = row.get("footprint_display")
            if isinstance(path, list) and isinstance(geo, dict):
                patches.append({"path": path, "footprint_display": geo})
        apply_footprint_display_patches(result, patches)
        done_count += len(rows)
        set_mosaic_plan_job_progress(
            job_id,
            phase="footprinting",
            round=1,
            rounds_max=1,
            children_total=total,
            children_done=min(done_count, total),
            retry_after_seconds=1,
        )
        if len(rows) < len(wave_specs):
            fallback_local = True
            break
    if fallback_local:
        await attach_footprint_displays_to_plan_result(result, plan_features)
