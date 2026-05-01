<div align="center">
  <img src="static/favicon.svg" alt="GeoFastMap API logo" width="140" />
</div>

# GeoFastMap API

**Fast, OGC-compliant geo API for vector tiles, PostGIS, and web maps.**

Struggle dealing with large geospatial files? That was then. **GeoFastMap API** is a modern stack: FastAPI + PostgreSQL/PostGIS, built to be *fast*—low latency, streaming downloads, efficient vector tiles, and real-time intersection/erase. No Java, no heavyweight servers. Just Python, async I/O, and a database that knows geometry.

---

## What is it?

GeoFastMap API is an **OGC API – Features** (and tiles, styles, processes) implementation focused on:

- **Speed** — Async FastAPI, asyncpg, keyset-paginated streaming, bounded-memory exports
- **Vector tiles** — Static MBTiles (Tippecanoe) and dynamic MVT from PostGIS; TileJSON per collection
- **Spatial operations** — Intersection and erase between collections (OGC API – Processes style), with background workers and progress
- **Web maps** — HTML map views, collection/item editors, style editor, basemaps, multi-layer maps
- **Bulk & streaming** — Bulk import (GeoJSON, GeoJSONL, KML, GPKG, shapefile in ZIP), streaming GeoJSONL download with low RAM and fast time-to-first-byte

Use it as a **vector tile server**, a **feature API** for QGIS/other clients, or the **backend for your own web mapping apps**.

---

## Features

| Area | What you get |
|------|----------------|
| **OGC API – Features** | Collections, items (CRUD), pagination, `bbox`, `datetime`, `sortby`, attribute filters, full-text search (`q`), property selection |
| **Tiles** | TileJSON per collection; static PMTiles/MBTiles build (Tippecanoe worker); dynamic MVT from PostGIS; cache (Redis) and optional tile job queue |
| **Styles** | Per-collection and global styles; style editor UI; fill/line/point toggles and paint options |
| **Processes** | **Intersection** and **Erase** between two collections; async jobs with status and result collection |
| **Maps** | Saved maps (layers + basemap + styles); HTML map view and editor |
| **Bulk** | Upload GeoJSON, GeoJSONL, KML, GPKG, or shapefile (ZIP); append or replace; background worker (Redis or in-process) |
| **Export** | `GET /collections/{id}/items/data` → streaming GeoJSONL download (keyset pagination, 256 KB chunks, minimal RAM) |
| **Data integrity** | Geometry validation (`make_valid`); optional splitting of GeometryCollections into points/lines/polygons on import and process results; migration to fix existing invalid geometries |

---

## Tech stack

- **API:** [FastAPI](https://fastapi.tiangolo.com/), [Pydantic](https://docs.pydantic.dev/)
- **DB:** [PostgreSQL](https://www.postgresql.org/) + [PostGIS](https://postgis.net/), [SQLAlchemy](https://www.sqlalchemy.org/) 2.0 async ([asyncpg](https://magicstack.github.io/asyncpg/))
- **Tiles:** [Tippecanoe](https://github.com/felt/tippecanoe) (static), PostGIS MVT (dynamic), [MapLibre GL JS](https://maplibre.org/) (frontend)
- **Workers:** Redis-backed queues for bulk import, tile builds, and process jobs
- **Migrations:** [Alembic](https://alembic.sqlalchemy.org/)

---

## Quick start (Docker Compose)

**Requirements:** Docker and Docker Compose.

```bash
git clone <this-repo>
cd geofast_api
docker compose up --build
```

This starts:

- **PostgreSQL (PostGIS)** on port `5434` (host)
- **Redis** on port `6379`
- **API** on **http://localhost:8000** (runs migrations then uvicorn)
- **Worker** — bulk import (GeoJSON, shapefile, etc.)
- **Tile worker** — Tippecanoe MBTiles/PMTiles builds
- **Process worker** — intersection and erase jobs

- **Landing (HTML):** http://localhost:8000/?f=html  
- **OpenAPI (Swagger):** http://localhost:8000/docs  
- **Collections:** http://localhost:8000/collections  

Create a collection, add data (upload or API), then use **View items**, **Build tiles**, or **Download GeoJSONL** from the collection page.

**Deploy:** **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** (single machine). **Advanced setup (multi-machine, work in progress):** **[docs/lab/geofast-distributed-experiment.md](docs/lab/geofast-distributed-experiment.md)**.

---

## API overview

| Path | Description |
|------|-------------|
| `GET /` | OGC landing page (JSON or HTML with `?f=html`) |
| `GET /conformance` | OGC conformance classes |
| `GET /collections` | List collections (OGC – Features) |
| `GET /collections/{id}` | Collection metadata; HTML map/view |
| `GET /collections/{id}/items` | Features (GeoJSON FC); supports `limit`, `offset`, `bbox`, `datetime`, `sortby`, `filter`, `q`, `properties` |
| `GET /collections/{id}/items/data` | **Streaming GeoJSONL download** (low RAM, fast start) |
| `GET/POST/PUT/PATCH/DELETE /collections/{id}/items[/{feature_id}]` | Feature CRUD (OGC Part 4 style) |
| `GET /collections/{id}/tiles` | TileJSON (dynamic and/or static tiles) |
| `POST /collections/{id}/tiles/build` | Request static tile build (returns job id) |
| `GET /collections/{id}/tiles/static/{z}/{x}/{y}.pbf` | Static vector tiles (PMTiles/MBTiles) |
| `GET /stac`, `POST /stac/search` | Federated STAC Item Search; `GET /stac?f=html` browse; catalog admin at `POST/PUT/DELETE /stac/catalogs` |
| `POST /collections/{id}/rasters` | Upload GeoTIFF (EPSG:4326) → COG on disk + feature with footprint |
| `GET /collections/{id}/coverages/{feature_id}` | Download COG; PNG tiles at `.../coverages/{id}/tiles/{z}/{x}/{y}.png` |
| `GET /collections/{id}/coverages/{feature_id}/titiler/tiles/...` | Proxy to Titiler (requires `TITILER_INTERNAL_URL`) |
| `POST /raster-views` | Save MosaicJSON + metadata; tile proxy at `GET /raster-views/{id}/titiler/tiles/...` |
| `GET /collections/{id}/styles` | Collection styles |
| `GET /styles` | Global (public) styles |
| `GET /processes` | List processes (intersection, erase); `?f=html` → processing UI |
| `POST /processes/intersection/execution` | Run intersection (body: `collection_id_a`, `collection_id_b`) |
| `POST /processes/erase/execution` | Run erase (body: `collection_id_a`, `collection_id_b`) |
| `GET /jobs/{job_id}` | Job status (bulk, tiles, process); cancel supported for process jobs |
| `GET /maps` | List saved maps |
| `GET /maps/{id}` | Map view (HTML or JSON) |
| `GET /auth/login` | Login form (HTML; requires `AUTH_SECRET_KEY`) |
| `POST /auth/login` | Submit login (form); redirects to `next` or change-password if required |
| `GET /auth/logout` | Clear session, redirect to landing |
| `GET /auth/change-password` | Change own password (requires login) |
| `GET /auth/users` | List users (admin only, HTML) |

Visibility: **public** (anyone), **logged** (authenticated users), **private** (owner/shared only). Admins see all collections, maps, and styles. Use HTTP Basic Auth for API clients (e.g. QGIS).

Most list/detail endpoints support **`?f=html`** or **`Accept: text/html`** for the web UI (maps, forms, style editor).

---

## Items query parameters (OGC-style)

For `GET /collections/{id}/items` (and the same filters apply to the GeoJSONL export and dynamic tiles):

- **limit** / **offset** — Pagination (default limit from config, max 1000). Response includes `numberMatched`, `numberReturned`, **next** / **prev** links.
- **bbox** — Bounding box: `minx,miny,maxx,maxy` (WGS84). Uses PostGIS spatial index.
- **datetime** — Filter by feature `created_at`: instant (`2024-01-01`) or range (`2024-01-01/2024-12-31`).
- **sortby** — `id`, `created_at`, or any property name.
- **sortdesc** — Sort descending when `true`.
- **filter** — Structured filters: `filter=key:op:value` (repeat for AND). Operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`.
- **q** — Full-text search across property values (uses trigram index).
- **properties** — Comma-separated property names (attribute selection).
- **Any other query param** — Treated as property filter: `?name=Main%20St` (exact), `?name=*St` (ends with), etc. Multiple filters are ANDed.

---

## Configuration

Key settings (env vars or `.env`; see `app/core/config.py`):

- **DATABASE_URL** — PostgreSQL + PostGIS (e.g. `postgresql+asyncpg://user:pass@host:5432/geofastmap`).
- **REDIS_URL** — For bulk queue, tile build queue, process queue, and tile cache (default `redis://localhost:6379/0`).
- **BULK_QUEUE_TYPE** — `redis` (separate worker) or `memory` (in-process consumer).
- **PROCESS_QUEUE_TYPE** — `redis` or `memory` for intersection/erase jobs.
- **TILES_STORAGE_PATH** — Where static MBTiles/PMTiles are stored (default `/data/tiles`).
- **BULK_STORAGE_PATH** — Where uploaded files go (default `/data/bulk-uploads`).
- **database_pool_size** / **database_pool_max_overflow** — Tune for concurrent tile/export load.

**Auth (users, roles, visibility):**

- **AUTH_SECRET_KEY** — Secret key for signing session cookies (HTML login). If **unset**, the app generates a **random key on every start**, so sessions are invalidated after each container restart (nav shows **Login** and you only see **public** collections until you sign in again). Set a stable value in production and in Docker (see `docker-compose.yml`).
- **AUTH_DEFAULT_ADMIN_USERNAME** / **AUTH_DEFAULT_ADMIN_PASSWORD** — Used only when the DB has no users (first run): a default `admin` user is created and must change password on first login.
- **Passwords:** Stored as bcrypt(salt + secret) on the server. The web UI hashes passwords with SHA-256 in the browser before sending; the backend then bcrypts that value. API/HTTP Basic can send either the same SHA-256 hex or plaintext (server hashes plaintext with SHA-256 before bcrypt).

Optional:

- **TILES_DYNAMIC_USE_QUEUE** — Use Redis search cache + tile job queue for dynamic tiles (workers read from cache).
- **TILES_DYNAMIC_WORKER_URL** — Offload dynamic tiles to another service (e.g. tippecanoe worker).
- **process_batch_max_bytes**, **process_batch_workers**, etc. — Tune process worker memory and parallelism.

---

## Running without Docker

1. **PostgreSQL + PostGIS** and **Redis** running and reachable.
2. Set **DATABASE_URL** (and **REDIS_URL** if using Redis).
3. Migrations and run:

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws wsproto
```

For bulk import and tile builds you’ll need workers (same codebase: `python -m app.worker_main`, `python -m app.tile_worker_main`, `python -m app.process_worker_main`) and Redis so they can consume the same queues.

---

## Deploying from home with Cloudflare Tunnel

You can run GeoFastMap API on a PC or small server at home and reach it on the public internet over **HTTPS** without opening inbound ports on your router. The usual path is: **own a domain**, **run a Cloudflare Tunnel**, and **point DNS at Cloudflare** so traffic flows to your tunnel and then to the app on `localhost`.

**Cost:** This pattern fits **Cloudflare’s free tier** for tunnels and DNS: there is **no usage-based “hosting” bill** from Cloudflare for typical tunnel + proxy traffic in the way many cloud VMs charge by the hour or by egress. Your main recurring cost is usually **domain registration** (annual). Always confirm current plans on [Cloudflare’s pricing](https://www.cloudflare.com/plans/), but this setup is aimed at **predictable, no-surprise-bills** hosting compared to metered cloud servers.

1. **Get a domain** — Register a domain (for example through [Cloudflare Registrar](https://www.cloudflare.com/products/registrar/) or any registrar) and add the zone to your Cloudflare account. If the domain lives elsewhere, change its **nameservers** to the pair Cloudflare gives you so Cloudflare can host DNS and route traffic for your tunnel.

2. **Create a tunnel** — In the [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) dashboard, go to **Networks → Tunnels**, create a tunnel, and install **`cloudflared`** on the same machine that runs the API (or a always-on host on your LAN). Authenticate the connector with the token Cloudflare provides.

3. **Route hostname to the app** — In the tunnel configuration, add a **public hostname** (e.g. `api.example.com`) that forwards to your local origin, typically `http://127.0.0.1:8000` if Uvicorn listens on port 8000. Cloudflare terminates TLS; users hit `https://api.example.com` and `cloudflared` proxies to your stack.

4. **Run the stack** — Start PostgreSQL, Redis, workers, and the API as usual (Docker Compose or bare metal). Keep **`cloudflared`** running (often as a systemd service) so the tunnel stays up. Set **`AUTH_SECRET_KEY`** and other production env vars as in [Configuration](#configuration).

After DNS propagates, the site is served from your home network while the tunnel handles encryption and inbound connectivity. For exact `cloudflared` commands and dashboard steps, see Cloudflare’s documentation: [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

The same guide is available in the **web UI** under **Documentation → Deploy from home (Cloudflare)** (`GET /project-docs/deploy-cloudflare?f=html`).

---

## Tests

Tests use an in-memory mock DB (no PostgreSQL or Docker required):

```bash
pip install -r requirements-dev.txt
pytest
```

With coverage:

```bash
pytest --cov=app --cov-report=term-missing --cov-report=html
# open htmlcov/index.html
```

---

## Tile build and process jobs

- **Tile build:** `POST /collections/{id}/tiles/build` returns a `job_id` and `status_url`. Poll `GET /jobs/{job_id}` until completed (or failed/cancelled). Tile worker runs Tippecanoe; resource limits are in `docker-compose.yml` (e.g. 4 CPUs, 8 GB RAM).
- **Process jobs:** After `POST /processes/intersection/execution` or `POST /processes/erase/execution`, poll `GET /jobs/{job_id}`. You can cancel with `POST /jobs/{job_id}/cancel` when status is `pending`.
- **Logs:** `docker compose logs tile_worker` or `docker compose logs process_worker` to inspect build or process runs.

---

## Project layout (high level)

- **app/models** — ORM (Collection, Feature, Style, Map, etc.).
- **app/schemas** — Pydantic request/response models.
- **app/crud** — Data access and business logic (collections, features, tiles, styles, maps).
- **app/api/routes** — FastAPI routers (root, collections, items, tiles, styles, processes, jobs, maps, basemaps).
- **app/services** — Bulk import, tile build queue, process worker, dynamic tile cache, job store.
- **app/templates** — Jinja2 HTML (maps, collection/item/style editors, landing).
- **alembic/versions** — DB migrations (PostGIS, partitions, indexes, basemaps, etc.).
- **static** — JS/CSS for map UIs (e.g. MapLibre, `geofastmap-utils.js`).

---

## Why “GeoFastMap”?

Because we lived without it: heavyweight servers, slow feeds, and bloated stacks. **GeoFastMap API** is built to be fast and lean—async from the DB to the HTTP response, streaming where it matters, and no more resources than you need. Vector tiles, DB-backed intersection, and web maps, without the pfff.

---

## License

GeoFastMap API is released under a **permissive license with attribution**: you may use, modify, and distribute it for any purpose, but you **must give clear credit** to GeoFastMap API (e.g. in documentation, “About” / “Credits,” or as “Powered by GeoFastMap API”). See [LICENSE](LICENSE) for the full terms.
