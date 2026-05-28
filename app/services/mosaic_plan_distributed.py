"""Distributed single-mosaic planning coordinator + shard task executor."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from shapely.geometry import shape

from app.core.config import get_settings
from app.models.stac_catalog import StacCatalog
from app.services.mosaic_plan import (
    _bbox_size_degrees,
    _dedupe_key,
    _subdivide_bbox_2x2,
    collect_stac_features,
    pinpoint_bboxes_from_remainder,
    plan_mosaic_from_features,
    split_initial_search_bboxes,
    void_search_bbox,
)
from app.services.stac_federation import mosaic_subtask_federation_catalog_parallelism
from app.services.mosaic_plan_jobs import (
    await_mosaic_plan_subtask_results,
    enqueue_mosaic_plan_subtask,
    set_mosaic_plan_job_progress,
)
from app.services.mosaic_plan_jobs import _task_id as _mosaic_task_id

log = logging.getLogger(__name__)


@dataclass
class _CatalogLite:
    id: str
    stac_api_root_url: str
    default_collections: list[str] | None


def _catalog_payload(catalogs: list[StacCatalog]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in catalogs:
        out.append(
            {
                "id": str(c.id),
                "stac_api_root_url": str(c.stac_api_root_url),
                "default_collections": list(c.default_collections or []),
            }
        )
    return out


def _catalogs_from_payload(rows: list[dict[str, Any]]) -> list[_CatalogLite]:
    out: list[_CatalogLite] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            _CatalogLite(
                id=str(r.get("id") or ""),
                stac_api_root_url=str(r.get("stac_api_root_url") or ""),
                default_collections=list(r.get("default_collections") or []),
            )
        )
    return out


def build_subtask_payload(
    *,
    catalogs: list[StacCatalog],
    stac_collection: str,
    bbox: list[float],
    datetime_slice: str,
    cloud_cover_max: float | None,
    sort_mode: str,
    fetch_limit: int,
) -> dict[str, Any]:
    return {
        "catalogs": _catalog_payload(catalogs),
        "stac_collection": stac_collection,
        "bbox": [float(x) for x in bbox],
        "datetime_slices": [datetime_slice],
        "cloud_cover_max": cloud_cover_max,
        "sort_mode": sort_mode,
        "fetch_limit": int(fetch_limit),
    }


def _effective_fetch_limit_for_bbox(bbox: list[float], fetch_limit: int) -> int:
    s = get_settings()
    large_limit = max(1, int(getattr(s, "mosaic_stac_large_bbox_limit", 250) or 1))
    min_deg = max(0.05, float(getattr(s, "mosaic_stac_min_bbox_degrees", 0.5) or 0.5))
    w, h = _bbox_size_degrees(bbox)
    big = max(w, h) >= max(2.0, min_deg * 2.0)
    return min(int(fetch_limit), int(large_limit)) if big else int(fetch_limit)


def _can_split_bbox(bbox: list[float], *, depth: int, max_depth: int, min_bbox_deg: float) -> bool:
    if depth >= max_depth:
        return False
    w, h = _bbox_size_degrees(bbox)
    return w > min_bbox_deg or h > min_bbox_deg


async def execute_subtask_payload(payload: dict[str, Any]) -> dict[str, Any]:
    catalogs = _catalogs_from_payload(list(payload.get("catalogs") or []))
    s = get_settings()
    sub_cat = max(
        1,
        int(s.mosaic_subjob_catalog_parallelism or s.mosaic_stac_catalog_parallelism or 1),
    )
    with mosaic_subtask_federation_catalog_parallelism(sub_cat):
        features, errors = await collect_stac_features(
            catalogs,  # type: ignore[arg-type]
            stac_collection=str(payload.get("stac_collection") or ""),
            bbox=[float(x) for x in list(payload.get("bbox") or [])[:4]],
            datetime_slices=[str(x) for x in list(payload.get("datetime_slices") or [])],
            cloud_cover_max=payload.get("cloud_cover_max"),
            sort_mode=str(payload.get("sort_mode") or "lowest_cloud"),
            fetch_limit=int(payload.get("fetch_limit") or 200),
        )
    return {"features": features, "errors": errors}


async def plan_mosaic_with_void_fill_distributed(
    *,
    job_id: str,
    owner_id: int,
    catalogs: list[StacCatalog],
    stac_collection: str,
    aoi: Any,
    search_bbox: list[float],
    datetime_slices: list[str],
    cloud_cover_max: float | None,
    sort_mode: str,
    fetch_limit: int,
    same_pass_date_strips: bool = False,
    swap_options_limit: int = 5,
    swap_options_offset: dict[str, int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    t_all = time.perf_counter()
    settings = get_settings()
    timeout_sec = max(10, int(settings.mosaic_subjob_round_timeout_seconds or 180))
    max_rounds = max(1, int(settings.mosaic_void_fill_max_rounds or 6))
    min_uncovered = float(settings.mosaic_void_fill_min_uncovered or 0.001)
    max_parts = max(1, int(settings.mosaic_void_pinpoint_max_parts or 16))
    fail_on_partial = bool(settings.mosaic_parent_fail_on_partial)
    shard_wave = max(1, int(settings.mosaic_subjob_bbox_datetime_parallelism or 1))
    max_split_depth = max(0, int(getattr(settings, "mosaic_stac_max_split_depth", 2) or 0))
    min_bbox_deg = max(0.05, float(getattr(settings, "mosaic_stac_min_bbox_degrees", 0.5) or 0.5))
    bbox_budget = max(1, int(getattr(settings, "mosaic_stac_max_bbox_tasks_per_round", 64) or 1))

    merged: dict[str, dict[str, Any]] = {}
    all_errors: list[dict[str, str]] = []
    last_result: dict[str, Any] | None = None
    locked_date_window: tuple[date, date] | None = None

    for round_idx in range(max_rounds):
        t_round = time.perf_counter()
        if round_idx == 0:
            q_bboxes = split_initial_search_bboxes([float(x) for x in search_bbox])
        else:
            assert last_result is not None
            uf = float(last_result.get("uncovered_fraction") or 1.0)
            if uf <= min_uncovered:
                last_result["void_fill_stopped"] = "coverage_met"
                break
            rem = last_result.get("remaining_uncovered")
            if not rem:
                last_result["void_fill_stopped"] = "no_remaining_geometry"
                break
            try:
                rem_g = shape(rem)
            except Exception:
                last_result["void_fill_stopped"] = "invalid_remaining"
                break
            pin = pinpoint_bboxes_from_remainder(rem_g, search_bbox, max_parts=max_parts)
            if pin:
                q_bboxes = pin
            else:
                vb = void_search_bbox(rem_g, search_bbox)
                if vb is None:
                    last_result["void_fill_stopped"] = "void_bbox_degenerate"
                    break
                q_bboxes = [vb]
        if not q_bboxes:
            break

        # Build shard rows (and in round 0, adaptively expand bboxes on failures/low yield).
        # We keep a hard cap on bbox count to stay fast.
        bbox_depth: dict[str, int] = {}
        bbox_key_to_bbox: dict[str, list[float]] = {}

        def _bbox_key(bb: list[float]) -> str:
            return ",".join(str(round(float(x), 8)) for x in bb[:4])

        bbox_queue: list[str] = []
        for bb in q_bboxes:
            k = _bbox_key(bb)
            if k in bbox_key_to_bbox:
                continue
            bbox_key_to_bbox[k] = [float(x) for x in bb[:4]]
            bbox_depth[k] = 0
            bbox_queue.append(k)

        # We’ll enqueue shards for bbox keys in waves, and after each wave complete we may add children.
        shard_rows: list[tuple[str, dict[str, Any], str]] = []  # (shard_key, payload, bbox_key)
        # Track all shards to expect in this round.
        while bbox_queue and len(bbox_key_to_bbox) <= bbox_budget:
            k = bbox_queue.pop(0)
            bb = bbox_key_to_bbox[k]
            lim_eff = _effective_fetch_limit_for_bbox(bb, fetch_limit)
            for dt in datetime_slices:
                shard_key = f"{round_idx}:{k}:{dt}"
                payload = build_subtask_payload(
                    catalogs=catalogs,
                    stac_collection=stac_collection,
                    bbox=bb,
                    datetime_slice=dt,
                    cloud_cover_max=cloud_cover_max,
                    sort_mode=sort_mode,
                    fetch_limit=lim_eff,
                )
                shard_rows.append((shard_key, payload, k))
            # Only round 0 gets adaptive expansion; other rounds use pinpoint/void bbox already.
            if round_idx != 0 or max_split_depth <= 0:
                continue
            # Expansion happens after we have results; no-op here.

        children_total = len(shard_rows)
        set_mosaic_plan_job_progress(
            job_id,
            phase="collecting_subjobs",
            round=round_idx + 1,
            rounds_max=max_rounds,
            children_total=children_total,
            children_done=0,
        )
        results: list[dict[str, Any]] = []
        done_count = 0
        next_idx = 0
        in_flight = 0
        ttl_seconds = max(600, int(settings.mosaic_subjob_result_ttl_seconds or 3600))
        # Slot-based dispatch: enqueue new shard work as soon as one result arrives.
        queue_wait_ms = 0
        while done_count < children_total:
            while in_flight < shard_wave and next_idx < children_total:
                shard_key, payload, _bbox_k = shard_rows[next_idx]
                enqueue_mosaic_plan_subtask(
                    job_id=job_id,
                    owner_id=owner_id,
                    round_idx=round_idx,
                    shard_key=shard_key,
                    payload=payload,
                    ttl_seconds=ttl_seconds,
                )
                in_flight += 1
                next_idx += 1
            if in_flight <= 0:
                break
            t_wait = time.perf_counter()
            got = await await_mosaic_plan_subtask_results(
                job_id=job_id,
                round_idx=round_idx,
                expected_count=1,
                timeout_seconds=timeout_sec,
            )
            queue_wait_ms += int((time.perf_counter() - t_wait) * 1000)
            if not got:
                break
            results.extend(got)
            step = len(got)
            done_count += step
            in_flight = max(0, in_flight - step)
            set_mosaic_plan_job_progress(
                job_id,
                phase="collecting_subjobs",
                round=round_idx + 1,
                rounds_max=max_rounds,
                children_total=children_total,
                children_done=done_count,
            )
        if len(results) < children_total and fail_on_partial:
            raise RuntimeError("Subjob round timed out before all shard results completed")

        # Round 0 adaptive expansion: if some bboxes failed (errors) or were very low-yield, add child bboxes and run them too.
        if round_idx == 0 and max_split_depth > 0 and len(bbox_key_to_bbox) < bbox_budget:
            # Map task_id -> bbox_key for shards we ran.
            task_to_bbox: dict[str, str] = {}
            for shard_key, _payload, bbox_k in shard_rows:
                tid = _mosaic_task_id(job_id, round_idx, shard_key)
                task_to_bbox[tid] = bbox_k

            stats: dict[str, dict[str, Any]] = {}
            for r in results:
                tid = str(r.get("task_id") or "")
                bk = task_to_bbox.get(tid)
                if not bk:
                    continue
                st = stats.setdefault(bk, {"features": 0, "had_errors": False})
                res = r.get("result") if isinstance(r.get("result"), dict) else {}
                feats = list(res.get("features") or [])
                errs = list(res.get("errors") or [])
                st["features"] += len(feats)
                if errs:
                    st["had_errors"] = True

            added_bbox_keys: list[str] = []
            for bk, st in stats.items():
                depth = int(bbox_depth.get(bk, 0))
                bb = bbox_key_to_bbox.get(bk)
                if not bb:
                    continue
                if not _can_split_bbox(bb, depth=depth, max_depth=max_split_depth, min_bbox_deg=min_bbox_deg):
                    continue
                # Split when errors happened, or when large bbox produced almost nothing.
                had_err = bool(st.get("had_errors"))
                feat_n = int(st.get("features") or 0)
                w, h = _bbox_size_degrees(bb)
                low_yield = (max(w, h) >= max(2.0, min_bbox_deg * 2.0)) and feat_n < 10
                if not (had_err or low_yield):
                    continue
                for ch in _subdivide_bbox_2x2(bb):
                    if len(bbox_key_to_bbox) >= bbox_budget:
                        break
                    ck = _bbox_key(ch)
                    if ck in bbox_key_to_bbox:
                        continue
                    bbox_key_to_bbox[ck] = [float(x) for x in ch[:4]]
                    bbox_depth[ck] = depth + 1
                    added_bbox_keys.append(ck)

            if added_bbox_keys:
                extra_rows: list[tuple[str, dict[str, Any], str]] = []
                for ck in added_bbox_keys:
                    bb = bbox_key_to_bbox[ck]
                    lim_eff = _effective_fetch_limit_for_bbox(bb, fetch_limit)
                    for dt in datetime_slices:
                        shard_key = f"{round_idx}:{ck}:{dt}"
                        payload = build_subtask_payload(
                            catalogs=catalogs,
                            stac_collection=stac_collection,
                            bbox=bb,
                            datetime_slice=dt,
                            cloud_cover_max=cloud_cover_max,
                            sort_mode=sort_mode,
                            fetch_limit=lim_eff,
                        )
                        extra_rows.append((shard_key, payload, ck))
                # Enqueue extras immediately and wait for them.
                for shard_key, payload, _ck in extra_rows:
                    enqueue_mosaic_plan_subtask(
                        job_id=job_id,
                        owner_id=owner_id,
                        round_idx=round_idx,
                        shard_key=shard_key,
                        payload=payload,
                        ttl_seconds=ttl_seconds,
                    )
                extra = await await_mosaic_plan_subtask_results(
                    job_id=job_id,
                    round_idx=round_idx,
                    expected_count=len(extra_rows),
                    timeout_seconds=timeout_sec,
                )
                results.extend(extra)

        ok = 0
        fail = 0
        for row in results:
            st = str(row.get("status") or "")
            if st in ("completed", "completed_with_errors"):
                ok += 1
            else:
                fail += 1
            res = row.get("result") if isinstance(row.get("result"), dict) else {}
            for f in list(res.get("features") or []):
                if isinstance(f, dict):
                    merged[_dedupe_key(f)] = f
            for e in list(res.get("errors") or []):
                if isinstance(e, dict):
                    all_errors.append(
                        {
                            "catalog_id": str(e.get("catalog_id") or ""),
                            "detail": str(e.get("detail") or "subtask_failed"),
                        }
                    )
        set_mosaic_plan_job_progress(
            job_id,
            phase="planning",
            round=round_idx + 1,
            rounds_max=max_rounds,
            children_total=children_total,
            children_done=ok + fail,
            children_failed=fail,
            features_seen=len(merged),
        )
        log.info(
            "mosaic distributed round done job_id=%s round=%d children_total=%d children_done=%d children_failed=%d queue_wait_ms=%d elapsed_ms=%d",
            job_id,
            round_idx + 1,
            children_total,
            ok + fail,
            fail,
            queue_wait_ms,
            int((time.perf_counter() - t_round) * 1000),
        )

        lock_for_plan = locked_date_window if round_idx == 0 else None
        last_result = await asyncio.to_thread(
            plan_mosaic_from_features,
            aoi,
            list(merged.values()),
            sort_mode,
            same_pass_date_strips=same_pass_date_strips,
            locked_date_window=lock_for_plan,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        if round_idx > 0 and same_pass_date_strips:
            last_result["void_fill_relaxed_date_lock"] = True
        if same_pass_date_strips and locked_date_window is None:
            sw = last_result.get("same_seven_day_window")
            if isinstance(sw, dict) and sw.get("start") and sw.get("end"):
                try:
                    locked_date_window = (
                        date.fromisoformat(str(sw["start"])[:10]),
                        date.fromisoformat(str(sw["end"])[:10]),
                    )
                except ValueError:
                    pass
        last_result["void_fill_rounds"] = round_idx + 1
        last_result["stac_feature_pool_size"] = len(merged)
        uf = float(last_result.get("uncovered_fraction") or 0.0)
        if uf <= min_uncovered:
            last_result["void_fill_stopped"] = "coverage_met"
            break

    if last_result is None:
        last_result = await asyncio.to_thread(
            plan_mosaic_from_features,
            aoi,
            [],
            sort_mode,
            same_pass_date_strips=same_pass_date_strips,
            locked_date_window=locked_date_window,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        last_result["void_fill_rounds"] = 0
        last_result["stac_feature_pool_size"] = 0
    if "void_fill_stopped" not in last_result:
        uf = float(last_result.get("uncovered_fraction") or 0.0)
        last_result["void_fill_stopped"] = "max_rounds" if uf > min_uncovered else "coverage_met"

    err_map: dict[str, str] = {}
    for e in all_errors:
        cid = str(e.get("catalog_id") or "")
        if cid:
            err_map[cid] = str(e.get("detail") or "")
    errors_out = [{"catalog_id": k, "detail": v} for k, v in err_map.items()]
    log.info(
        "mosaic distributed plan done job_id=%s rounds=%s features=%d errors=%d elapsed_ms=%d",
        job_id,
        last_result.get("void_fill_rounds"),
        len(merged),
        len(errors_out),
        int((time.perf_counter() - t_all) * 1000),
    )
    return last_result, errors_out, list(merged.values())
