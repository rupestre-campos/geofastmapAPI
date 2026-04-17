# Deploying GeoFastMap API

**Default path:** one machine, **Docker** + **Docker Compose**—see below. No Kubernetes in this repo.

**Advanced setup (work in progress):** multi-machine deployment on **owned hardware** (no cloud bill for the stack) is documented in **[`lab/geofast-distributed-experiment.md`](lab/geofast-distributed-experiment.md)**—primary machine vs optional **small worker** hosts, Raspberry Pi tunnel edge, NFS/file-server notes, phased checklists, and an experiment log. With the API running, open **`/project-docs/advanced-setup?f=html`** for the HTML version in **Project documentation**.

**Config reference:** [`app/core/config.py`](../app/core/config.py).

---

## One machine (the simple path)

```bash
git clone <this-repo-url>
cd geofast_api
docker compose up --build
```

- **API / UI:** http://localhost:8000  
- **Postgres** is on host port **5434** (see [`docker-compose.yml`](../docker-compose.yml)).  
- Starts Postgres, Redis, API (runs migrations), bulk worker, tile worker, process worker, and Titiler.

Stop: `docker compose down`. Add `-v` only if you want to **delete** Docker volumes and local data.

That is the full story for development and for many small deployments.

---

## Splitting across hosts (optional)

If you run DB, API, workers, or Titiler on **different** computers, everyone still uses **one** `DATABASE_URL`, **one** `REDIS_URL`, and the same secrets where required. Paths under **`BULK_STORAGE_PATH`**, **`TILES_STORAGE_PATH`**, and **`RASTER_STORAGE_PATH`** must be the **same files** for every process that reads or writes them—usually **NFS** or **one machine** that holds all data.

Pre-made Compose slices live in [`deploy/compose/`](../deploy/compose/) (see [`deploy/compose/README.md`](../deploy/compose/README.md)). Copy [`deploy/env/api.sample`](../deploy/env/api.sample) and [`workers.sample`](../deploy/env/workers.sample) to `deploy/env/.env.api` and `deploy/env/.env.workers`, edit hosts and secrets, then e.g.:

```bash
docker compose -f deploy/compose/docker-compose.api.yml up -d --build
```

If you need **NFS** so worker hosts see the same **`tiles` / `bulk-uploads` / `rasters`** files as the primary, the **recommended** approach is **kernel NFS on the primary host** (Ubuntu `nfs-kernel-server`), **not** inside Docker: **[`lab/nfs-host-ubuntu.md`](lab/nfs-host-ubuntu.md)** has copy-paste steps, `/etc/exports`, firewall, and worker mount commands.

An **optional** Docker-based NFS helper exists for experiments only—see [`deploy/compose/README.md`](../deploy/compose/README.md) and [`deploy/compose/docker-compose.nfs.yml`](../deploy/compose/docker-compose.nfs.yml). Use only on trusted LANs; never expose NFS to the public internet.

The [`Dockerfile`](../Dockerfile) includes `static/` so the web UI works without bind-mounting the repo.

**Titiler:** set `TITILER_INTERNAL_URL` on the API to your Titiler container or an **nginx** upstream ([`deploy/nginx/titiler-upstream.conf`](../deploy/nginx/titiler-upstream.conf)).

**Postgres on bare metal:** install PostGIS + `pg_trgm`, then point `DATABASE_URL` at it; run `alembic upgrade head` once when upgrading.

**Safety:** do not expose Postgres or Redis to the public internet; use strong passwords on the LAN.

---

## What must match when you split hosts

| Thing | Why |
|-------|-----|
| One Postgres | Same `DATABASE_URL` for API and workers. |
| One Redis | Same `REDIS_URL` for queues and caches. |
| Shared data dirs or colocation | Same tree for bulk, tiles, rasters where those components run. |
| `AUTH_SECRET_KEY` | Same on every API instance (sessions). |
| `TITILER_INTERNAL_SECRET` | Shared between API and Titiler internal fetch. |

---

## Summary

| You want | Do this |
|----------|---------|
| **Fastest** | `docker compose up` at repo root. |
| **Advanced multi-machine (WIP)** | [`lab/geofast-distributed-experiment.md`](lab/geofast-distributed-experiment.md) |
| **NFS on primary (share tiles/bulk/rasters with workers)** | [`lab/nfs-host-ubuntu.md`](lab/nfs-host-ubuntu.md) |
| **Compose/env details** | [`deploy/README.md`](../deploy/README.md) |
