# Environment files for split Compose

1. Copy samples to **`deploy/env/.env.api`** and **`deploy/env/.env.workers`** (Compose references these paths).

   ```bash
   cp deploy/env/api.sample deploy/env/.env.api
   cp deploy/env/workers.sample deploy/env/.env.workers
   ```

2. Edit hosts, passwords, and secrets. Do not commit real secrets.

3. **Mosaic workers:** Default samples target **homogeneous** workers (same `MOSAIC_SUBJOB_*` / `MOSAIC_QUEUE_TYPE` on the API and every mosaic process—each host can run parent + subtasks). Optional **dedicated coordinator/shard** overrides are commented in [`workers.sample`](workers.sample). For Redis keys and tuning, see **[`docs/DEPLOYMENT.md` § Distributed mosaic planning](../../docs/DEPLOYMENT.md#distributed-mosaic-planning-redis-subjobs)**.

Full context: [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md).
