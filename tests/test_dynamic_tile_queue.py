"""Tests for dynamic tile LIFO queue + multi-core worker helpers."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.tile_queue_worker import _resolve_concurrency


def test_resolve_concurrency_explicit():
    assert _resolve_concurrency(SimpleNamespace(tiles_dynamic_queue_concurrency=4)) == 4


def test_resolve_concurrency_auto_caps():
    with patch("app.tile_queue_worker.os.cpu_count", return_value=32):
        assert _resolve_concurrency(SimpleNamespace(tiles_dynamic_queue_concurrency=0)) == 8


def test_push_tile_job_is_lifo_and_trims():
    pipe = MagicMock()
    r = MagicMock()
    r.pipeline.return_value = pipe

    with (
        patch("app.services.dynamic_tile_cache.get_settings") as gs,
        patch("redis.from_url", return_value=r),
    ):
        gs.return_value = SimpleNamespace(
            redis_url="redis://localhost:6379/0",
            tiles_dynamic_queue_max_jobs=3,
        )
        from app.services.dynamic_tile_cache import TILE_JOBS_QUEUE_KEY, push_tile_job

        push_tile_job("c1", "pk", 5, 1, 2)

    pipe.lpush.assert_called_once()
    args = pipe.lpush.call_args[0]
    assert args[0] == TILE_JOBS_QUEUE_KEY
    payload = json.loads(args[1])
    assert payload["collection_id"] == "c1"
    assert payload["z"] == 5
    assert "enqueued_at" in payload
    pipe.ltrim.assert_called_once_with(TILE_JOBS_QUEUE_KEY, 0, 2)
    pipe.execute.assert_called_once()


def test_pop_tile_job_skips_stale():
    stale = json.dumps(
        {
            "collection_id": "c",
            "params_key": "p",
            "z": 1,
            "x": 0,
            "y": 0,
            "enqueued_at": time.time() - 120,
        }
    )
    fresh = json.dumps(
        {
            "collection_id": "c",
            "params_key": "p",
            "z": 2,
            "x": 0,
            "y": 0,
            "enqueued_at": time.time(),
        }
    )
    r = MagicMock()
    r.blpop.side_effect = [
        (TILE := "geofastmap:tile_jobs", stale),
        (TILE, fresh),
    ]

    with (
        patch("app.services.dynamic_tile_cache.get_settings") as gs,
        patch("redis.from_url", return_value=r),
    ):
        gs.return_value = SimpleNamespace(
            redis_url="redis://localhost:6379/0",
            tiles_dynamic_queue_job_max_age_seconds=30.0,
        )
        from app.services.dynamic_tile_cache import pop_tile_job

        job = pop_tile_job(timeout=5)

    assert job is not None
    assert job["z"] == 2
    assert r.blpop.call_count == 2
