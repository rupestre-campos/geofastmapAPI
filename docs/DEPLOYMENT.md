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

When API/workers run on hosts that mount shared NFS/SMB at `/data/*`, stack the NFS overrides so containers use host bind mounts (instead of local named volumes):

```bash
docker compose -f deploy/compose/docker-compose.api.yml -f deploy/compose/docker-compose.api.nfs.yml up -d --build
docker compose -f deploy/compose/docker-compose.workers.yml -f deploy/compose/docker-compose.workers.nfs.yml up -d --build
```

Split compose files use a fixed project name (`geofastmap_api`) so services, networks, and volumes are reused even if the repo directory name differs. Override on a host with `docker compose -p <project_name> ...` (or `COMPOSE_PROJECT_NAME`) when needed.

If you need **NFS** so worker hosts see the same **`tiles` / `bulk-uploads` / `rasters`** files as the primary, the **recommended** approach is **kernel NFS on the primary host** (Ubuntu `nfs-kernel-server`), **not** inside Docker: **[`lab/nfs-host-ubuntu.md`](lab/nfs-host-ubuntu.md)** has copy-paste steps, `/etc/exports`, firewall, and worker mount commands.

An **optional** Docker-based NFS helper exists for experiments only—see [`deploy/compose/README.md`](../deploy/compose/README.md) and [`deploy/compose/docker-compose.nfs.yml`](../deploy/compose/docker-compose.nfs.yml). Use only on trusted LANs; never expose NFS to the public internet.

The [`Dockerfile`](../Dockerfile) includes `static/` so the web UI works without bind-mounting the repo.

**Titiler:** set `TITILER_INTERNAL_URL` on the API to your Titiler container or an **nginx** upstream ([`deploy/nginx/titiler-upstream.conf`](../deploy/nginx/titiler-upstream.conf)). On a **worker** with NFS-mounted **`/data/rasters`**, stack [`docker-compose.titiler.yml`](../deploy/compose/docker-compose.titiler.yml) with [`docker-compose.titiler.nfs.yml`](../deploy/compose/docker-compose.titiler.nfs.yml) so rasters are not read from an empty named volume.

**Postgres on bare metal:** install PostGIS + `pg_trgm`, then point `DATABASE_URL` at it; run `alembic upgrade head` once when upgrading.

**Safety:** do not expose Postgres or Redis to the public internet; use strong passwords on the LAN.

### Optional admin observability (simple default)

Recommended first step for new deployments: use in-app admin pages at:

- `/admin/observability?f=html` (live request logs + filters)
- `/admin/observability/performance?f=html` (mean/p50/p90 by endpoint)
- `/admin/observability/servers?f=html` (local + configured server load snapshots)

Tune defaults in the admin page itself (`/admin/observability?f=html` -> Settings).  
Those values are persisted in database runtime settings, so they are not required in `.env`.

For host metrics on the same machine, Netdata is integrated directly in:

- [`deploy/compose/docker-compose.api.yml`](../deploy/compose/docker-compose.api.yml)

When you start the API split stack, Netdata starts too:

`docker compose -f deploy/compose/docker-compose.api.yml up -d --build`

### Optional advanced observability stack (Grafana/Loki/Tempo)

An optional compose profile is available for logs + metrics + tracing:

- file: [`deploy/compose/docker-compose.observability.yml`](../deploy/compose/docker-compose.observability.yml)
- services: Grafana, Loki, Promtail, Prometheus, node_exporter, Tempo, OTEL Collector
- start: `docker compose -f deploy/compose/docker-compose.observability.yml --profile observability up -d`

Security default: Grafana binds to `127.0.0.1:3000` only. Keep it private (VPN/SSH tunnel) or front it with admin auth.

To emit API traces, set in API env (see [`deploy/env/api.sample`](../deploy/env/api.sample)):

- `OBSERVABILITY_TRACING_ENABLED=true`
- `OBSERVABILITY_OTLP_ENDPOINT=http://otel_collector:4317`
- optional `OBSERVABILITY_TRACE_SAMPLE_RATIO` (0.0..1.0)

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

---

## CDN revalidation note for large mosaics

If Cloudflare shows repeated `CF-Cache-Status: REVALIDATED` for mosaic tiles, verify:

1. Tile URLs include `?v=<tiles_revision>` (versioned cache path).
2. CDN cache key includes query string for `/raster-views/*/titiler/tiles/*`.
3. Edge TTL rules do not override origin immutable caching for versioned URLs.

The API now emits `X-Mosaic-Versioned-Cache: hit|miss` on mosaic tile responses to confirm whether a request matched the versioned cache policy.

### Browser-canceled tile requests (e.g. NS_BINDING_ABORTED)

MapLibre cancels in-flight tile HTTP requests when the user zooms or when `fitBounds` runs with animation, so the browser may show **canceled** rows in DevTools even though the request already reached your reverse proxy or API.

- **Zero network on reload** needs a normal reload (not “hard reload”), DevTools **cache enabled**, and tile URLs with **`?v=<tiles_revision>`** plus immutable cache headers so the browser can serve from disk without contacting the origin.
- The API can return **204** when the ASGI client has disconnected (before or after a tile is ready), and it **cancels upstream TiTiler reads** when disconnect is detected mid-fetch so less work is wasted. A request may still open at the edge before disconnect is visible to the app.
- The mosaic **detail preview** uses **`fitBounds` with `duration: 0`** for the initial bbox so the map jumps to extent in one step (fewer intermediate tile requests). Map editor/viewer keep short animated fits for smoother UX; server-side disconnect handling still limits wasted TiTiler work on aborted tiles.
