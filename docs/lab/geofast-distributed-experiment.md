# Advanced setup: distributed GeoFast on owned hardware

> **Work in progress.** This guide is updated as we wire real machines. The default, supported path for everyone remains **single-machine** [`docker compose up`](../DEPLOYMENT.md)—this document is **optional** and describes **our** lab-style layout (no cloud hosting bill for the app stack).

**In the web UI:** the same topic is available under **Project documentation** → *Advanced setup (multi-machine, WIP)* at `/project-docs/advanced-setup?f=html` on your GeoFast instance.

---

## Why this doc exists (mission)

We want GeoFast running **across machines we already own**, using **local disks and LAN**, with **no recurring cloud bill** for the service itself. A **Raspberry Pi** may run **Cloudflare Tunnel** only to expose **one** HTTPS entry to the lab—other apps on the Pi stay separate; we do **not** aim to run the full Docker stack on the Pi.

---

## Hardware we have (this lab)

| Machine | CPU / RAM / disk | What it is good for (in this stack) |
|---------|------------------|-------------------------------------|
| **Primary machine** (e.g. Ryzen workstation) | 8 cores, 16 GB RAM, **1 TB NVMe** | **Main server:** PostgreSQL, Redis, **API**, **Titiler**, optional local workers, and **all Docker volumes** (tiles, rasters, bulk). Acts as **NFS/SMB file server** in Phase 2 so worker hosts read/write the same paths. NVMe suits DB, MBTiles, and COGs. |
| **Small worker machine(s)** (optional) | Example: 2 cores (4 threads), **8 GB** RAM, **1 TB HDD** | **Extra workers only** (`process_worker` for more layer/intersection throughput; optionally `bulk` / `tile_worker` if shared storage is fast enough). Heavy **Tippecanoe** builds on low-RAM / slow disks are painful—prefer the primary for large tile jobs. Use **Linux or macOS** with Docker; verify containers see NFS mounts at `/data/...`. |
| **Raspberry Pi** | (edge only) | **Cloudflare Tunnel** to forward HTTPS to **one** origin on the LAN (usually the primary API port). **Do not** run Postgres, Redis, or Tippecanoe on the Pi for this project. |

---

## What each part of the app needs (reminder)

| Component | Needs | Typical host |
|-----------|--------|--------------|
| **PostgreSQL** | Disk + RAM for data | Primary (NVMe). |
| **Redis** | Small RAM, low CPU | Primary, reachable from worker hosts. |
| **API** | HTTP, sessions, serves vector tiles from **disk paths** in DB | Primary. Must see the **same files** as workers for tiles/bulk (local disk or NFS). |
| **Raster upload → COG** | GDAL/rasterio during **HTTP upload** | **API on the primary only** today ([`rasters.py`](../../app/api/routes/rasters.py)); not a background worker. Keep **`/data/rasters`** on the primary unless you add async jobs later. |
| **tile_worker** (Tippecanoe) | CPU, RAM, **`TILES_STORAGE_PATH`** | Primary for large builds; optional on a small worker **if** that path is **shared** (NFS) and performance is acceptable. |
| **process_worker** (intersection / erase) | CPU + **Postgres** | Primary and/or **small worker** with `DATABASE_URL` / `REDIS_URL` pointing at the primary. No local tile volume needed for many jobs; results live in the DB. |
| **bulk worker** | Redis + **`BULK_STORAGE_PATH`** shared with API if it writes uploads | Primary or worker with **NFS** to the same path. |
| **Titiler** | CPU, RAM, **raster files** | Primary; `TITILER_INTERNAL_URL` on API. |

---

## Recommended layout

### Phase 1 — default (recommended first)

**Run the entire stack on the primary machine** with the repo’s root [`docker-compose.yml`](../../docker-compose.yml): one `docker compose up`, all data under Docker volumes (ideally on **NVMe**).

| Machine | Role |
|---------|------|
| **Primary** | Full stack: Postgres, Redis, API, all workers, Titiler. Bind API to LAN (`0.0.0.0:8000` or behind local nginx). |
| **Raspberry Pi** | **cloudflared** (or Cloudflare Tunnel) **only**: public hostname → `http://<PRIMARY_LAN_IP>:8000` (or `https` if you terminate TLS on the primary/nginx). |
| **Small worker machine(s)** | Not used—no NFS required. |

**Why:** No NFS, no split env. The API **reads** MBTiles and rasters from local paths; that matches how the code works today.

**Checklist — primary**

1. Install **Docker** + **Docker Compose**.
2. Clone this repo, `cd geofast_api`.
3. Set strong passwords in compose or `.env` for production; set `AUTH_SECRET_KEY` and `TITILER_INTERNAL_SECRET` for real use.
4. `docker compose up -d --build`.
5. From another device on the LAN, open `http://<PRIMARY_IP>:8000`.
6. Firewall: allow **8000** (or 443 if nginx) from **LAN + Pi** only, not the whole internet except via Cloudflare.

**Checklist — Raspberry Pi (tunnel only)**

1. Install `cloudflared` (or use Cloudflare’s tunnel installer).
2. Create a tunnel in Cloudflare Dashboard; **private origin** = `http://<PRIMARY_LAN_IP>:8000` (same port the API uses).
3. Do **not** run `docker compose` for GeoFast on the Pi if the Pi is resource-constrained or hosts other services.

---

### Phase 2 — optional small worker machine + primary as file server

Add a **second host** that runs **only** worker containers from [`deploy/compose/docker-compose.workers.yml`](../../deploy/compose/docker-compose.workers.yml). The **primary** keeps Postgres, Redis, API, Titiler, and **owns** all data; it **exports** directories so workers mount the **same** paths (e.g. `/data/tiles`, `/data/bulk-uploads`) via **NFS** or **SMB**.

| Machine | Role |
|---------|------|
| **Primary** | Postgres, Redis, API, Titiler, **all `/data/...` volumes**. **NFS/SMB server** for paths workers must read/write. Optional: keep or stop local `process_worker` / `tile_worker` / `worker` depending on whether you want **extra** queue consumers or to **move** load to the small host. |
| **Small worker** | `docker compose -f deploy/compose/docker-compose.workers.yml` with `DATABASE_URL` and `REDIS_URL` pointed at the primary. Mount shared storage at **identical** paths before starting compose. Typical first step: add **`process_worker`** only (more layer jobs); add **`tile_worker`** only if `TILES_STORAGE_PATH` is shared and fast enough. |

**Why:** Process (and bulk/tile) jobs are **Redis-driven**; they do not need a public HTTP port. **`BULK_STORAGE_PATH`** and **`TILES_STORAGE_PATH`** must be the **same filesystem namespace** the API uses—export from the primary, mount on the worker host **before** `docker compose` so containers see `/data/...` as on the server.

**Raster / COG:** Uploads are converted **inside the API** on the primary. The **rasters** volume can stay **primary-only**; Phase 2 does not require exporting rasters to the worker if it only runs `process_worker`.

**Queue consumers:** Multiple **`process_worker`** instances (primary + small worker, or several hosts) usually **scale** horizontally—each job is one Redis message. For **`tile_worker`**, the code uses Redis **`BRPOP`**; multiple consumers can share the queue, but **test** your workload. Avoid running **duplicate stacks** by mistake (two full `docker compose` apps on the same host). Prefer a deliberate split: **workers only** on the small machine.

**Checklist — primary as file server**

1. Map Docker volume host paths (or bind mounts) you will share—commonly the directories behind **`geofastmap_tiles`** and **`geofastmap_bulk_uploads`** (see root [`docker-compose.yml`](../../docker-compose.yml) / [`docker-compose.api.yml`](../../deploy/compose/docker-compose.api.yml)).
2. Configure **NFS** (Linux) or **SMB** exports **only to LAN** / worker host IPs—not the open internet.
3. Firewall: allow **Postgres** (e.g. `5432` or published `5434`) and **Redis** `6379` from the **worker host IP** only.
4. **NFS (recommended):** on the primary, use **kernel NFS outside Docker**—see **[`nfs-host-ubuntu.md`](nfs-host-ubuntu.md)** for bind mounts under `/srv/geofast/`, **`/etc/exports`**, firewall, and worker **`mount`** commands. Optional: the Docker helper [`docker-compose.nfs.yml`](../../deploy/compose/docker-compose.nfs.yml) exists for experiments; it has limitations (wildcard `PERMITTED`, possible empty listings without `crossmnt`)—prefer **`nfs-host-ubuntu.md`** for a straightforward deploy.

**Checklist — small worker host**

1. Install Docker (Docker Engine on Linux, or Docker Desktop where applicable).
2. **Mount** the primary’s exports at **`/data/tiles`**, **`/data/bulk-uploads`** (and any other path your worker services need)—**same paths** as inside containers on the primary.
3. Verify a test container or `ls` sees the mount at `/data/...` **before** relying on workers.
4. Clone repo; copy `deploy/env/workers.sample` → `deploy/env/.env.workers`.
5. Set `DATABASE_URL=postgresql+asyncpg://...@<PRIMARY_LAN_IP>:5434/...` (port **5434** if the primary exposes Postgres like root compose—adjust to your setup).
6. Set `REDIS_URL=redis://<PRIMARY_LAN_IP>:6379/0`.
7. Set `TILES_STORAGE_PATH=/data/tiles` and `BULK_STORAGE_PATH=/data/bulk-uploads` to match mounts.
8. **Shared config:** keep one canonical `.env.workers`; **secure-copy** updates from the primary (or your secrets store). Do **not** publish live secrets via world-readable NFS.
9. Optionally use [`docker-compose.workers.process-only.example.yml`](../../deploy/compose/docker-compose.workers.process-only.example.yml) for **only** `process_worker`, or a custom override—see [`deploy/compose/README.md`](../../deploy/compose/README.md).
10. `docker compose -f deploy/compose/docker-compose.workers.yml up -d --build` (plus `-f` override if you created one).

---

## Network diagram (fill in your IPs)

```
                    Cloudflare
                         |
Internet -----------------|---------------------------
                         |  tunnel (HTTPS)
                    +----+----+
                    |   Pi    |  cloudflared -> http://PRIMARY_IP:8000
                    +----+----+
                         |  LAN only
        +----------------+----------------+
        |                |                |
   +----+----+      +-----+-----+     +----+----+
   | Primary |      |  Worker   |     | Worker  |
   |  :8000  |      |  host A   |     | host B  |
   | PG/Redis|      |  workers  |     |(optional)|
   | tiles/  |      | NFS client|     |         |
   | NFS exp |<-----+  mounts   |     |         |
   +---------+      +-----------+     +---------+
```

---

## Role assignment (log — update as you go)

| Service | Host (planned / actual) | Compose / notes |
|---------|-------------------------|-----------------|
| PostgreSQL | | Primary |
| Redis | | Primary |
| API | | Primary |
| NFS / file shares | | Primary exports `/data/...` for Phase 2 |
| Bulk worker | | Primary and/or worker (needs shared `BULK_STORAGE_PATH`) |
| Tile worker | | Primary; optional on worker with shared `TILES_STORAGE_PATH` |
| Process worker | | Primary and/or one or more small workers |
| Titiler | | Primary |
| Cloudflare Tunnel | Pi → origin | |

---

## Environment snapshot (placeholders — do not commit real secrets)

```bash
# On primary (example)
# DATABASE_URL=postgresql+asyncpg://postgres:...@127.0.0.1:5432/geofastmap
# REDIS_URL=redis://127.0.0.1:6379/0

# On small worker host (example)
# DATABASE_URL=postgresql+asyncpg://postgres:...@PRIMARY_LAN_IP:5434/geofastmap
# REDIS_URL=redis://PRIMARY_LAN_IP:6379/0
# TILES_STORAGE_PATH=/data/tiles
# BULK_STORAGE_PATH=/data/bulk-uploads   # must match NFS mount layout

# Titiler / API (on primary)
# TITILER_INTERNAL_URL=http://PRIMARY_LAN_IP:8001
# RASTER_INTERNAL_FETCH_BASE_URL=http://PRIMARY_LAN_IP:8000
```

Keep real passwords and keys in a private store, not in git.

---

## Experiment log

| Date | Change | Outcome |
|------|--------|--------|
| | | |

**Limitations / notes:** (NFS latency vs local NVMe, Docker bind-mount visibility, tunnel latency, …)

---

## References

- Default deploy: [`../DEPLOYMENT.md`](../DEPLOYMENT.md)
- Split Compose: [`../../deploy/compose/`](../../deploy/compose/)
- **NFS (host, recommended):** [`nfs-host-ubuntu.md`](nfs-host-ubuntu.md)
- Optional Docker NFS helper: [`../../deploy/compose/docker-compose.nfs.yml`](../../deploy/compose/docker-compose.nfs.yml)
- Config: [`../../app/core/config.py`](../../app/core/config.py)
