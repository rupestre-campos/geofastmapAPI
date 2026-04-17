# Compose bundles (subset per machine)

These files implement the **split stacks** described in [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md). Paths are relative to **`deploy/compose/`** (`build.context: ../..`, `env_file: ../env/.env.*`).

| File | Purpose |
|------|---------|
| `docker-compose.db.yml` | PostGIS only |
| `docker-compose.redis.yml` | Redis only |
| `docker-compose.api.yml` | API only |
| `docker-compose.workers.yml` | Bulk, tile, and process workers |
| `docker-compose.workers.process-only.example.yml` | Example: **only** `process_worker` (optional second host; copy/rename to taste) |
| `docker-compose.titiler.yml` | TiTiler only (host port **8001**; named volume `geofastmap_rasters`) |
| `docker-compose.titiler.nfs.yml` | **Override:** bind-mount host **`/data/rasters`** (use with `docker-compose.titiler.yml` on workers / NFS clients) |
| `docker-compose.nfs.yml` | Optional **Docker** NFS helper on primary (see limitations; prefer host NFS in [`docs/lab/nfs-host-ubuntu.md`](../../docs/lab/nfs-host-ubuntu.md)) |

**Env files:** copy [`deploy/env/api.sample`](../env/api.sample) → `deploy/env/.env.api` and [`workers.sample`](../env/workers.sample) → `deploy/env/.env.workers`, then edit.

Examples:

```bash
docker compose -f deploy/compose/docker-compose.db.yml up -d
docker compose -f deploy/compose/docker-compose.api.yml up -d --build
docker compose -f deploy/compose/docker-compose.workers.yml up -d --build
```

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
