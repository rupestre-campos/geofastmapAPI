"""Shared Redis retry helpers for queue producers/consumers."""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from app.core.config import get_settings

T = TypeVar("T")


def retry_wait_seconds(attempt_idx: int, *, base: float, max_seconds: float) -> float:
    raw = base * (2 ** max(0, attempt_idx - 1))
    wait = min(max_seconds, raw)
    return wait * (1.0 + random.uniform(0.0, 0.15))


def run_redis_retry(
    label: str,
    fn: Callable[[], T],
    *,
    max_attempts: int | None = None,
    forever: bool = False,
    on_retry: Callable[[int, float, str], None] | None = None,
) -> T:
    settings = get_settings()
    base = max(0.1, float(getattr(settings, "redis_retry_base_seconds", 1.0) or 1.0))
    max_backoff = max(base, float(getattr(settings, "redis_retry_max_seconds", 30.0) or 30.0))
    limit = max_attempts
    if limit is None:
        limit = max(1, int(getattr(settings, "redis_retry_enqueue_max_attempts", 5) or 5))
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as e:
            if not forever and attempt >= limit:
                raise
            wait = retry_wait_seconds(attempt, base=base, max_seconds=max_backoff)
            if on_retry:
                on_retry(attempt, wait, f"{label}: {type(e).__name__}: {e}")
            time.sleep(wait)
