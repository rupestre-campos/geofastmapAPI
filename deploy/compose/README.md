# Compose bundles (subset per machine)

These files implement the **split stacks** described in [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md). Paths are relative to **`deploy/compose/`** (`build.context: ../..`, `env_file: ../env/.env.*`).

All split compose files set `name: geofastmap_api` so DB/API/workers/TiTiler reuse the same Compose project (networks and named volumes) regardless of clone folder name.
If you need a different project name on one host, run with `-p <project_name>` (or `COMPOSE_PROJECT_NAME`).

| File | Purpose |
|------|---------|
| `docker-compose.db.yml` | PostGIS only |
| `docker-compose.redis.yml` | Redis only |
| `docker-compose.api.yml` | API only (named volumes for `/data/*`) |
| `docker-compose.api.nfs.yml` | **Override:** bind-mount host `/data/bulk-uploads`, `/data/tiles`, `/data/rasters` (use with `docker-compose.api.yml`) |
| `docker-compose.workers.yml` | Bulk, tile, and process workers |
| `docker-compose.workers.nfs.yml` | **Override:** bind-mount host `/data/bulk-uploads` and `/data/tiles` (use with `docker-compose.workers.yml`) |
| `docker-compose.workers.process-only.example.yml` | Example: **only** `process_worker` (optional second host; copy/rename to taste) |
| `docker-compose.titiler.yml` | TiTiler only (host port **8001**; named volume `geofastmap_rasters`) |
| `docker-compose.titiler.nfs.yml` | **Override:** bind-mount host **`/data/rasters`** (use with `docker-compose.titiler.yml` on workers / NFS clients) |
| `docker-compose.nfs.yml` | Optional **Docker** NFS helper on primary (see limitations; prefer host NFS in [`docs/lab/nfs-host-ubuntu.md`](../../docs/lab/nfs-host-ubuntu.md)) |
| `docker-compose.observability.yml` | Optional admin observability profile (Grafana, Loki, Promtail, Prometheus, node_exporter, Tempo, OTEL Collector) |
| `docker-compose.pgbouncer.yml` | **PgBouncer only** (no `db` service — use when Postgres already runs elsewhere / name conflict). See [`../env/pgbouncer.sample`](../env/pgbouncer.sample) and [`../../docker/pgbouncer/README.md`](../../docker/pgbouncer/README.md). |
| `docker-compose.pgbouncer.db-network.yml` | **Override:** attach pooler to DB stack network (e.g. `geofastmap_api_default`); use with `docker-compose.pgbouncer.yml` when Postgres is container `geofastmap_db` on the same host. |

**Env files:** copy [`deploy/env/api.sample`](../env/api.sample) → `deploy/env/.env.api` and [`workers.sample`](../env/workers.sample) → `deploy/env/.env.workers`, then edit.

Examples:

```bash
docker compose -f deploy/compose/docker-compose.db.yml up -d --build
docker compose -f deploy/compose/docker-compose.redis.yml up -d
docker compose -f deploy/compose/docker-compose.api.yml up -d --build
docker compose -f deploy/compose/docker-compose.workers.yml up -d --build
docker compose -f deploy/compose/docker-compose.observability.yml --profile observability up -d
```

PgBouncer only, Postgres **in Docker** on same host (DB container `geofastmap_db`, network `geofastmap_api_default`):

```bash
cp deploy/env/pgbouncer.sample deploy/env/.env.pgbouncer
# set PGBOUNCER_BACKEND_DATABASE_URL=postgres://...@geofastmap_db:5432/geofastmap
docker compose --env-file deploy/env/.env.pgbouncer \
  -f deploy/compose/docker-compose.pgbouncer.yml \
  -f deploy/compose/docker-compose.pgbouncer.db-network.yml \
  up -d
```

Then update **`deploy/env/.env.api`** and recreate the API container:

- `DATABASE_URL=postgresql+asyncpg://postgres:PASSWORD@geofastmap_pgbouncer:5432/geofastmap`
- `DATABASE_USE_PGBOUNCER=true`
- `DATABASE_URL_DIRECT=postgresql+asyncpg://postgres:PASSWORD@geofastmap_db:5432/geofastmap`

Observability security default: Grafana is published on `127.0.0.1:3000` only. Keep it behind VPN/SSH tunnel or front it with admin auth.

### Split stack on one machine (db + redis + api)

[`docker-compose.db.yml`](docker-compose.db.yml), [`docker-compose.redis.yml`](docker-compose.redis.yml), and [`docker-compose.api.yml`](docker-compose.api.yml) all use **`name: geofastmap_api`**, so they share the default network **`geofastmap_api_default`** (same project). Typical order:

```bash
docker compose -f deploy/compose/docker-compose.db.yml up -d --build
docker compose -f deploy/compose/docker-compose.redis.yml up -d
docker compose -f deploy/compose/docker-compose.api.yml up -d --build
```

Use the PgBouncer block above when you add the pooler; the API lines use container DNS **`geofastmap_pgbouncer`** and **`geofastmap_db`** on that network.

Single-host full stack (dev): root [`docker-compose.yml`](../../docker-compose.yml) — `docker compose up`.

### Workers on a second host (optional)

To add **only** queue workers on a **small worker machine**, keep Postgres, Redis, API, and data volumes on the **primary**, then:

1. Export **`/data/tiles`** and **`/data/bulk-uploads`** (or your real bind-mount paths) from the primary via **NFS** or **SMB** so the worker host mounts them at the **same** paths (e.g. `/data/tiles`).
2. Copy [`deploy/env/workers.sample`](../env/workers.sample) → `deploy/env/.env.workers` on the worker host; set `DATABASE_URL`, `REDIS_URL`, and storage paths to match.
3. Run `docker compose -f deploy/compose/docker-compose.workers.yml up -d --build` from the repo on that host.

Use **`docker-compose.workers.process-only.example.yml`** (or your own override) if you want **only** **`process_worker`** on the second host. Full checklist and diagrams: [`docs/lab/geofast-distributed-experiment.md`](../../docs/lab/geofast-distributed-experiment.md) (Phase 2).

### NFS on the primary (recommended: host server)

For workers to share **`/data/tiles`**, **`/data/bulk-uploads`**, and **`/data/rasters`**, use **kernel NFS on the primary (Ubuntu)**—**outside Docker**. Full steps, `/etc/exports`, firewall, and worker **`mount`** commands:

- **[`docs/lab/nfs-host-ubuntu.md`](../../docs/lab/nfs-host-ubuntu.md)**

That guide exports **`/srv/geofast/tiles`**, **`/srv/geofast/bulk-uploads`**, **`/srv/geofast/rasters`** after bind-mounting Docker volume `_data` paths (three **separate** export lines avoid empty listings on clients).

Set `TILES_STORAGE_PATH=/data/tiles` and `BULK_STORAGE_PATH=/data/bulk-uploads` in `deploy/env/.env.workers`; align Titiler with **`/data/rasters`**.

**Important for split hosts:** base API/workers files use local named volumes for `/data/*`. On hosts where `/data/*` is mounted from NFS/SMB, stack the NFS overrides so containers read/write the shared paths:

```bash
docker compose -f deploy/compose/docker-compose.api.yml -f deploy/compose/docker-compose.api.nfs.yml up -d --build
docker compose -f deploy/compose/docker-compose.workers.yml -f deploy/compose/docker-compose.workers.nfs.yml up -d --build
```

**Titiler on a worker:** mount **`/data/rasters`** via NFS (same path as on the primary), then start TiTiler with the bind-mount override so Docker does not use an empty local named volume:

```bash
docker compose -f deploy/compose/docker-compose.titiler.yml -f deploy/compose/docker-compose.titiler.nfs.yml up -d
```

### Optional: Docker NFS helper compose

If you want to experiment with the **in-container** NFS image instead, use:

```bash
docker compose -f deploy/compose/docker-compose.nfs.yml up -d
```

**Before starting:** on the primary, prepare **`/srv/geofast`** with three **bind-mounts** from each volume `_data` (same as [`nfs-host-ubuntu.md`](../../docs/lab/nfs-host-ubuntu.md) Step A). The compose file mounts **`/srv/geofast:/exports`** only.

**Limitations:** the image’s **`PERMITTED`** handling breaks **CIDR** with a `/` (use `192.168.8.*`-style wildcards); **NFSv4 + bind submounts** may show **empty** directory listings unless you add **`crossmnt`** or use host NFS as above. See comments in [`docker-compose.nfs.yml`](docker-compose.nfs.yml).

Worker mounts against the container differ from host NFS paths—prefer **`nfs-host-ubuntu.md`** for predictable behavior.

Overview: [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md). Advanced setup (WIP): [`docs/lab/geofast-distributed-experiment.md`](../../docs/lab/geofast-distributed-experiment.md).
