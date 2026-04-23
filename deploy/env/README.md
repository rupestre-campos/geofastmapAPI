# Environment files for split Compose

1. Copy samples to **`deploy/env/.env.api`** and **`deploy/env/.env.workers`** (Compose references these paths).

   ```bash
   cp deploy/env/api.sample deploy/env/.env.api
   cp deploy/env/workers.sample deploy/env/.env.workers
   ```

2. Edit hosts, passwords, and secrets. Do not commit real secrets.

Full context: [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md).
