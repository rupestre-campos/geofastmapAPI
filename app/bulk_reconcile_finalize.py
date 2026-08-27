#!/usr/bin/env python3
"""
Bulk-reconcile stuck finalize jobs (no per-job SQL).

Run once on the API / finalize worker host after deploying partition-swap fixes:

  docker exec geofastmap_finalize_worker python -m app.bulk_reconcile_finalize
  docker exec geofastmap_finalize_worker python -m app.bulk_reconcile_finalize --dry-run
"""

from __future__ import annotations

import argparse
import sys

from app.services.bulk_finalize_reconcile import reconcile_all_stuck_finalize_jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile all stuck bulk finalize jobs and orphan partitions."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report actions without changing jobs or database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="Max jobs to scan from Redis (default 5000).",
    )
    args = parser.parse_args(argv)

    stats = reconcile_all_stuck_finalize_jobs(dry_run=args.dry_run, limit=args.limit)
    if stats.errors and not args.dry_run:
        print("Errors (first 10):", file=sys.stderr)
        for jid, err in stats.errors[:10]:
            print(f"  {jid}: {err[:200]}", file=sys.stderr)
        return 1 if stats.promoted == 0 and stats.completed_already_swapped == 0 else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
