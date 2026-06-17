"""Track per-collection bulk import activity so other workers (e.g. tile build) can wait until idle."""

from __future__ import annotations

import time
from collections.abc import Callable

from app.core.config import get_settings
from app.services.redis_resilience import run_redis_retry

BULK_COLLECTION_INFLIGHT_PREFIX = "geofastmap:bulk_collection_inflight:"
BULK_COLLECTION_DESTRUCTIVE_PREFIX = "geofastmap:bulk_collection_destructive:"
BULK_COLLECTION_MUTEX_PREFIX = "geofastmap:bulk_collection_mutex:"
_DEFAULT_INFLIGHT_TTL_SECONDS = 86400 * 2  # safety if a worker dies mid-import
_DEFAULT_MUTEX_TTL_SECONDS = 86400 * 2

_TERMINAL_JOB_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "error", "success", "done", "succeeded"}
)


def is_terminal_job_status(status: str | None) -> bool:
    return (status or "").lower() in _TERMINAL_JOB_STATUSES


def _mutex_key(collection_id: str) -> str:
    return f"{BULK_COLLECTION_MUTEX_PREFIX}{collection_id}"


def incr_collection_bulk_activity(collection_id: str) -> None:
    """Call when starting bulk work that mutates collection features (import shard, replace prestage, finalize)."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = f"{BULK_COLLECTION_INFLIGHT_PREFIX}{collection_id}"

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.incr(key)
        r.expire(key, _DEFAULT_INFLIGHT_TTL_SECONDS)

    run_redis_retry("bulk_collection_activity_incr", _run)


def decr_collection_bulk_activity(collection_id: str) -> None:
    """Call when bulk work finishes (paired with incr)."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = f"{BULK_COLLECTION_INFLIGHT_PREFIX}{collection_id}"

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        n = int(r.decr(key))
        if n <= 0:
            r.delete(key)

    run_redis_retry("bulk_collection_activity_decr", _run)


def incr_collection_destructive_bulk_activity(collection_id: str) -> None:
    """TRUNCATE/DELETE prestage or shadow finalize — readers may block; use for items busy guards."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = f"{BULK_COLLECTION_DESTRUCTIVE_PREFIX}{collection_id}"

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        r.incr(key)
        r.expire(key, _DEFAULT_INFLIGHT_TTL_SECONDS)

    run_redis_retry("bulk_collection_destructive_incr", _run)


def decr_collection_destructive_bulk_activity(collection_id: str) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = f"{BULK_COLLECTION_DESTRUCTIVE_PREFIX}{collection_id}"

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        n = int(r.decr(key))
        if n <= 0:
            r.delete(key)

    run_redis_retry("bulk_collection_destructive_decr", _run)


def collection_has_destructive_bulk_activity(collection_id: str) -> bool:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    import redis

    key = f"{BULK_COLLECTION_DESTRUCTIVE_PREFIX}{collection_id}"

    def _read() -> bool:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        v = r.get(key)
        return bool(v) and int(v) > 0

    return run_redis_retry(
        "bulk_collection_destructive_read",
        _read,
        max_attempts=max(
            1,
            int(getattr(settings, "redis_retry_read_max_attempts", 15) or 15),
        ),
    )


def get_active_bulk_job_ids(collection_id: str) -> list[str]:
    """Parent job ids with in-flight bulk imports (mutex holder + non-terminal collection jobs)."""
    from app.services.job_store import list_jobs_for_collection

    active: set[str] = set()
    holder = get_collection_bulk_mutex_holder(collection_id)
    if holder:
        active.add(holder)
    active_statuses = frozenset({"pending", "running", "replacing", "cancelling"})
    for job in list_jobs_for_collection(collection_id, limit=20):
        status = (job.status or "").lower()
        if status in active_statuses and job.job_id:
            jid = job.job_id.split(":shard:")[0]
            active.add(jid)
    return sorted(active)


def collection_has_bulk_activity(collection_id: str) -> bool:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    import redis

    key = f"{BULK_COLLECTION_INFLIGHT_PREFIX}{collection_id}"

    def _read() -> bool:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        v = r.get(key)
        return bool(v) and int(v) > 0

    return run_redis_retry(
        "bulk_collection_activity_read",
        _read,
        max_attempts=max(
            1,
            int(getattr(settings, "redis_retry_read_max_attempts", 15) or 15),
        ),
    )


def get_collection_bulk_mutex_holder(collection_id: str) -> str | None:
    """Return job_id holding the per-collection bulk mutex, or None."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return None
    import redis

    def _read() -> str | None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        v = r.get(_mutex_key(collection_id))
        return str(v) if v else None

    return run_redis_retry(
        "bulk_collection_mutex_read",
        _read,
        max_attempts=max(
            1,
            int(getattr(settings, "redis_retry_read_max_attempts", 15) or 15),
        ),
    )


def holds_collection_bulk_mutex(collection_id: str, owner_job_id: str) -> bool:
    holder = get_collection_bulk_mutex_holder(collection_id)
    return holder is not None and holder == owner_job_id


def try_acquire_collection_bulk_mutex(collection_id: str, owner_job_id: str) -> bool:
    """Exclusive lock: one bulk mutator per collection (parent job owns lock for its shards)."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return True
    import redis

    key = _mutex_key(collection_id)
    ttl = max(300, int(getattr(settings, "bulk_collection_mutex_ttl_seconds", _DEFAULT_MUTEX_TTL_SECONDS) or _DEFAULT_MUTEX_TTL_SECONDS))

    def _run() -> bool:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        if r.set(key, owner_job_id, nx=True, ex=ttl):
            return True
        return (r.get(key) or "") == owner_job_id

    return bool(
        run_redis_retry(
            "bulk_collection_mutex_acquire",
            _run,
            max_attempts=max(
                1,
                int(getattr(settings, "redis_retry_read_max_attempts", 15) or 15),
            ),
        )
    )


def release_collection_bulk_mutex(collection_id: str, owner_job_id: str) -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = _mutex_key(collection_id)

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        if r.get(key) == owner_job_id:
            r.delete(key)

    run_redis_retry("bulk_collection_mutex_release", _run)


def release_bulk_mutex_for_job(collection_id: str, job_id: str) -> None:
    """Release the collection mutex when *job_id* is the current holder."""
    release_collection_bulk_mutex(collection_id, job_id)


def reclaim_stale_collection_bulk_mutex(collection_id: str) -> str | None:
    """
    Release mutex when the holder job is missing or already terminal.
    Returns the reclaimed holder job_id, or None if mutex was free or still active.
    """
    holder = get_collection_bulk_mutex_holder(collection_id)
    if not holder:
        return None
    from app.services.job_store import get_job

    job = get_job(holder)
    if job is None or is_terminal_job_status(job.status) or job.finished_at is not None:
        release_collection_bulk_mutex(collection_id, holder)
        print(
            f"[bulk-mutex] reclaimed stale lock collection={collection_id} "
            f"holder={holder} status={getattr(job, 'status', 'missing')}",
            flush=True,
        )
        return holder
    return None


def reclaim_all_stale_bulk_mutexes() -> list[tuple[str, str]]:
    """At worker startup: drop mutexes whose holder jobs already finished."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return []
    import redis

    reclaimed: list[tuple[str, str]] = []

    def _scan() -> list[str]:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        return list(r.scan_iter(match=f"{BULK_COLLECTION_MUTEX_PREFIX}*"))

    try:
        keys = run_redis_retry("bulk_collection_mutex_scan", _scan)
    except Exception:
        return reclaimed
    prefix_len = len(BULK_COLLECTION_MUTEX_PREFIX)
    for key in keys:
        collection_id = key[prefix_len:]
        if not collection_id:
            continue
        holder = reclaim_stale_collection_bulk_mutex(collection_id)
        if holder:
            reclaimed.append((collection_id, holder))
    return reclaimed


def refresh_collection_bulk_mutex(collection_id: str, owner_job_id: str) -> None:
    """Extend mutex TTL while long-running bulk work continues."""
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    import redis

    key = _mutex_key(collection_id)
    ttl = max(300, int(getattr(settings, "bulk_collection_mutex_ttl_seconds", _DEFAULT_MUTEX_TTL_SECONDS) or _DEFAULT_MUTEX_TTL_SECONDS))

    def _run() -> None:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        if r.get(key) == owner_job_id:
            r.expire(key, ttl)

    run_redis_retry("bulk_collection_mutex_refresh", _run)


def wait_until_collection_bulk_idle(
    collection_id: str,
    *,
    stop_check: Callable[[], bool] | None = None,
    poll_seconds: float | None = None,
    on_waiting_message: Callable[[], None] | None = None,
) -> bool:
    """
    Block until no bulk import activity for this collection, or stop_check returns True.
    Returns False if stopped early (e.g. cancel); True when idle.
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return True
    poll = float(
        poll_seconds
        if poll_seconds is not None
        else getattr(settings, "tile_build_bulk_wait_poll_seconds", 2.0) or 2.0
    )
    poll = max(0.5, poll)
    last_msg = 0.0
    while collection_has_bulk_activity(collection_id):
        if stop_check and stop_check():
            return False
        now = time.monotonic()
        if on_waiting_message and (now - last_msg >= 30.0 or last_msg == 0.0):
            on_waiting_message()
            last_msg = now
        time.sleep(poll)
    return True
