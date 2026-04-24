# Environment files for split Compose

1. Copy samples to **`deploy/env/.env.api`** and **`deploy/env/.env.workers`** (Compose references these paths).

   ```bash
   cp deploy/env/api.sample deploy/env/.env.api
   cp deploy/env/workers.sample deploy/env/.env.workers
   ```

2. Edit hosts, passwords, and secrets. Do not commit real secrets.

3. **Mosaic workers:** [`workers.sample`](workers.sample) lists throughput-oriented defaults and commented **coordinator vs shard** overrides. For architecture, Redis queue keys, and tuning (`MOSAIC_SUBJOB_*`, `MOSAIC_WORKER_MAX_CONCURRENT=0` shard-only behavior, wave size vs fleet capacity, STAC knobs), see **[`docs/DEPLOYMENT.md` § Distributed mosaic planning](../../docs/DEPLOYMENT.md#distributed-mosaic-planning-redis-subjobs)**.

Full context: [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md).
