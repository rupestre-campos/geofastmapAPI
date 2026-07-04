#!/usr/bin/env python3
"""Enqueue partition swap for a job whose staging table still has rows."""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.services.bulk_finalize_queue import BulkFinalizePayload, enqueue_finalize
from app.services.bulk_staging import staging_row_count_sync, staging_table_name
from app.services.job_store import get_job, update_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue bulk finalize (partition swap).")
    parser.add_argument("job_id")
    parser.add_argument("collection_id")
    parser.add_argument("--mode", choices=("replace", "append"), default="replace")
    args = parser.parse_args(argv)

    engine = create_engine(get_settings().database_sync_url, pool_pre_ping=True, future=True)
    try:
        count = staging_row_count_sync(engine, args.job_id)
        print(f"staging={staging_table_name(args.job_id)} rows={count}")
        if count <= 0:
            print("No staging rows.")
            return 1
        job = get_job(args.job_id)
        payload = BulkFinalizePayload(
            job_id=args.job_id,
            collection_id=args.collection_id,
            mode=args.mode,
            items_created=(job.items_created if job else None) or count,
            items_failed=job.items_failed if job else 0,
            owner_id=job.owner_id if job else None,
        )
        enqueue_finalize(payload, force=True)
        update_job(
            args.job_id,
            status="finalizing",
            message=f"Manual recover: queued partition swap for {count:,} staged features.",
            items_created=payload.items_created,
        )
        print("OK enqueued finalize")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
