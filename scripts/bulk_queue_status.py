#!/usr/bin/env python3
"""Print bulk import queue, mutex, and job status from Redis (run on API or worker host)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.bulk_collection_activity import BULK_COLLECTION_MUTEX_PREFIX, get_collection_bulk_mutex_holder
from app.services.bulk_queue import QUEUE_KEY, BulkJobPayload
from app.services.job_store import get_job, list_all_jobs


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


def main() -> int:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        print("BULK_QUEUE_TYPE is not redis; nothing to inspect.")
        return 1

    import redis

    r = redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=8, socket_timeout=8)
    try:
        ok = r.ping()
    except Exception as e:
        print(f"Redis unreachable at {settings.redis_url}: {e}")
        return 2
    print(f"redis_ok={ok} url={settings.redis_url}")

    qlen = int(r.llen(QUEUE_KEY) or 0)
    print(f"queue_key={QUEUE_KEY} length={qlen}")
    if qlen:
        print("queue_head_to_tail:")
        for i, raw in enumerate(r.lrange(QUEUE_KEY, 0, min(qlen, 20) - 1)):
            try:
                p = BulkJobPayload.from_json(raw)
                print(f"  [{i}] job={p.job_id} collection={p.collection_id} mode={p.mode}")
            except Exception:
                print(f"  [{i}] invalid payload: {raw[:120]}")

    mutex_keys = sorted(r.scan_iter(match=f"{BULK_COLLECTION_MUTEX_PREFIX}*", count=500))
    print(f"collection_mutexes={len(mutex_keys)}")
    for key in mutex_keys:
        collection_id = key[len(BULK_COLLECTION_MUTEX_PREFIX) :]
        holder = get_collection_bulk_mutex_holder(collection_id) or r.get(key)
        job = get_job(holder) if holder else None
        status = job.status if job else "missing"
        age = _age_seconds(job.updated_at.isoformat() + "Z" if job and job.updated_at else None)
        print(f"  {collection_id}: holder={holder} status={status} age_s={int(age) if age is not None else '?'}")

    pending = []
    running = []
    for job in list_all_jobs(limit=500):
        if job.status == "pending":
            pending.append(job)
        elif job.status in ("running", "replacing"):
            running.append(job)

    print(f"jobs_pending={len(pending)} jobs_running={len(running)}")
    for job in sorted(pending, key=lambda j: j.updated_at, reverse=True)[:25]:
        age = _age_seconds(job.updated_at.isoformat() + "Z" if job.updated_at else None)
        msg = (job.message or "")[:80]
        print(f"  pending {job.job_id} coll={job.collection_id} age_s={int(age) if age is not None else '?'} msg={msg!r}")
    for job in sorted(running, key=lambda j: j.updated_at, reverse=True)[:10]:
        age = _age_seconds(job.last_progress_at.isoformat() + "Z" if job.last_progress_at else None)
        print(f"  {job.status} {job.job_id} coll={job.collection_id} last_progress_age_s={int(age) if age is not None else '?'}")

    if qlen and not running and not mutex_keys:
        print("\nhint: jobs queued but none running and no mutexes — bulk worker may be down or cannot reach Redis.")
    if mutex_keys and pending:
        print("\nhint: pending jobs with active mutexes — run worker watchdog or restart worker after deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
