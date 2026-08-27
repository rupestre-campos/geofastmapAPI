"""CLI: bulk import queue / mutex / job snapshot (docker exec geofastmap_worker python -m app.bulk_queue_status)."""

from scripts.bulk_queue_status import main

if __name__ == "__main__":
    raise SystemExit(main())
