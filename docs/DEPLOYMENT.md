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

### Large bulk import profile (resilience + throughput)

For multi-GB uploads, tune API + worker envs together (same Redis settings on every host):

- `BULK_IMPORT_BATCH_SIZE` (start `2000`, adjust by DB CPU/IO)
- `BULK_INSERT_PARTS_BATCH_SIZE` (start `160`, controls batched SQL VALUES when one feature splits)
- `BULK_PROGRESS_HEARTBEAT_SECONDS` (e.g. `5`, emits progress while a large batch is still running)
- `BULK_EXTENT_UPDATE_MODE` = `best_effort` or `deferred` for very large layers (reduces end-of-job tail latency)
- `BULK_DB_RETRY_MAX_ATTEMPTS`, `BULK_DB_RETRY_BASE_SECONDS`, `BULK_DB_RETRY_MAX_SECONDS`
- `REDIS_RETRY_BASE_SECONDS`, `REDIS_RETRY_MAX_SECONDS`, `REDIS_RETRY_ENQUEUE_MAX_ATTEMPTS`
- `BULK_UPLOAD_SESSION_TTL_SECONDS`, `BULK_UPLOAD_CHUNK_SIZE_BYTES` (resumable uploads)
- `BULK_SHARDED_INGEST_ENABLED`, `BULK_SHARD_LINES_PER_PART` (single-file sharded ingest)

Recommended starting point for multi-GB browser uploads: `BULK_UPLOAD_CHUNK_SIZE_BYTES=33554432` (32 MiB). This cuts request count significantly (about 147 parts for a 5GB file, vs ~589 with 8 MiB parts).

For Cloudflare-proxied deployments, prefer the resumable bulk upload session flow:

- `POST /collections/{id}/items/bulk/sessions`
- `PUT /collections/{id}/items/bulk/sessions/{upload_id}/parts/{part_no}`
- `POST /collections/{id}/items/bulk/sessions/{upload_id}/complete`
- `DELETE /collections/{id}/items/bulk/sessions/{upload_id}`

Use `immediate` extent updates only when you need bbox refreshed synchronously after each import.

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

## Distributed mosaic planning (Redis subjobs)

Large mosaic plans can split **STAC collection** work into **subtasks** (one `(bbox × datetime slice)` per task) so many worker processes—on one host or several—share the upstream `/search` load. The **parent** job runs on a worker that dequeues `geofastmap:mosaic_plan_queue`; it enqueues shard work on `geofastmap:mosaic_plan_subtask_queue`, waits for results in **waves**, merges features, then runs the greedy planner. Optional **thumbnail `footprint_display`** work can run on `geofastmap:mosaic_footprint_subtask_queue` after planning (see below).

**Requirements**

- Same **`REDIS_URL`** everywhere (API + all mosaic workers).
- **`MOSAIC_QUEUE_TYPE=redis`** on API and workers.
- **`MOSAIC_SUBJOB_QUEUE_ENABLED=true`** on the **API** (so `compute_mosaic_plan` uses the distributed planner when the worker runs the job with `allow_distributed=True`) and on **every mosaic worker** that should enqueue or consume subtasks.
- Workers run [`app/mosaic_worker_main.py`](../app/mosaic_worker_main.py); code paths live under [`app/services/mosaic_plan_distributed.py`](../app/services/mosaic_plan_distributed.py), [`app/services/mosaic_footprint_distributed.py`](../app/services/mosaic_footprint_distributed.py), and [`app/services/mosaic_plan_jobs.py`](../app/services/mosaic_plan_jobs.py).

**Homogeneous mosaic workers (simple default)**

You do **not** need different env on “coordinator” vs “shard” machines. **Recommended:** use the **same** mosaic-related variables on the **API** and on **every** `mosaic_worker` process. Each process can dequeue a **parent** job (run the coordinator loop, enqueue subtasks to Redis), **and** dequeue **subtasks** (and **footprint** tasks if enabled) from the same queues—`MOSAIC_SUBJOB_CONSUME_SUBTASKS_WHILE_PARENT_ACTIVE` defaults to **true** so a machine that is busy with a parent job can still help execute its own shards.

- Set **`MOSAIC_QUEUE_TYPE=redis`**, **`MOSAIC_SUBJOB_QUEUE_ENABLED=true`** on API and all mosaic workers.
- Set **`MOSAIC_SUBJOB_WORKER_CONCURRENCY` > 0** on every process that should run STAC subtasks (omit coordinator-only tuning until you need it).
- **Wave size:** set **`MOSAIC_SUBJOB_BBOX_DATETIME_PARALLELISM`** to roughly **`MOSAIC_SUBJOB_WORKER_CONCURRENCY ×` (number of mosaic worker processes)** across the fleet. On a **single** process, start around **4–8**—much larger waves (e.g. 32) on one consumer mostly add wait time at barriers.

Dev **`docker-compose.yml`** in this repo enables subjobs and uses a stable **`container_name`** for one mosaic worker; production can use the same pattern or multiple processes with identical env.

**Optional dedicated roles (advanced)**

For large fleets you *may* split **coordinator-only** hosts (parent queue only) and **shard-only** hosts (subtask queue only) using env:

| Role | Typical env |
|------|-------------|
| **Coordinator** | `MOSAIC_WORKER_MAX_CONCURRENT=1` (or `2` if you want several unrelated parent jobs at once), `MOSAIC_SUBJOB_WORKER_CONCURRENCY=0`, `MOSAIC_SUBJOB_CONSUME_SUBTASKS_WHILE_PARENT_ACTIVE=false`, `MOSAIC_SUBJOB_QUEUE_ENABLED=true` |
| **Shard** | `MOSAIC_WORKER_MAX_CONCURRENT=0` (**does not** dequeue the parent queue), `MOSAIC_SUBJOB_WORKER_CONCURRENCY` set to your desired async concurrency (e.g. `4`–`8`), `MOSAIC_SUBJOB_CONSUME_SUBTASKS_WHILE_PARENT_ACTIVE=true`, `MOSAIC_SUBJOB_QUEUE_ENABLED=true`. For distributed footprints, same hosts (or others) must dequeue the footprint queue—see **Footprint subtasks**. |

`MOSAIC_WORKER_MAX_CONCURRENT=0` is **shard-only**: the process never calls `BRPOP` on `geofastmap:mosaic_plan_queue`. Values `<= 0` are treated as zero; values `>= 1` cap concurrent **parent** jobs per process.

**Queue priority on mixed workers**

Processes that dequeue **both** parent and auxiliary queues use **footprint queue first**, then **subtask queue**, then **parent queue** in each `BRPOP` call so a deep parent backlog does not starve `footprint_display` or STAC shard work. When parent concurrency is already at its cap, the worker first tries `BRPOP` on footprint/subtask keys only (short timeout) before blocking on a parent task to finish—so a **central** host with `MOSAIC_SUBJOB_CONSUME_SUBTASKS_WHILE_PARENT_ACTIVE=true` can still drain footprint subtasks while one or more parent jobs run. Set `MOSAIC_SUBJOB_WORKER_CONCURRENCY=0` on a coordinator if that process must not take STAC subtasks; use `MOSAIC_FOOTPRINT_SUBJOB_WORKER_CONCURRENCY` to cap footprint-only slots.

**Wave size and fleet capacity**

`MOSAIC_SUBJOB_BBOX_DATETIME_PARALLELISM` is read by the **parent** process while planning. It is the **wave size**: how many subtasks are dispatched before the coordinator waits for that wave to finish. Set it to roughly the **total subtask throughput** you want in flight—e.g. sum over all shard machines of `(MOSAIC_SUBJOB_WORKER_CONCURRENCY × number of mosaic worker processes on that host)`. If it is too small, shard CPUs and network sit idle between barriers. If subtasks are slow or waves are huge, raise **`MOSAIC_SUBJOB_ROUND_TIMEOUT_SECONDS`** (e.g. `300`–`600`).

**STAC tuning (mostly on shards)**

Subtasks call federated STAC search with settings from the **shard** host:

- **`MOSAIC_STAC_CATALOG_PARALLELISM`** / **`MOSAIC_STAC_TOTAL_INFLIGHT_MAX`** — catalog fan-out and global in-flight budget per merged `/search`. Raise together; watch upstream **429** and latency.
- **`MOSAIC_SUBJOB_CATALOG_PARALLELISM`** — applied **inside subtasks only** (overrides the catalog parallelism used for that merge path so shard boxes can differ from the API’s defaults). Implementation: context in [`app/services/stac_federation.py`](../app/services/stac_federation.py).
- **`MOSAIC_STAC_DATETIME_PARALLELISM`** — parallel datetime slices inside `collect_stac_features` (relevant when a subtask payload has multiple slices).

**Distributed footprint_display (optional)**

After **`plan_mosaic_from_features`** returns, the planner can attach UI **`footprint_display`** polygons by fetching preview images (HTTP + CPU). With **`MOSAIC_FOOTPRINT_DISTRIBUTED_ENABLED=true`** on the **API** and **`MOSAIC_QUEUE_TYPE=redis`**, async mosaic jobs (`include_footprint_display` true) enqueue one Redis task per selected/swap thumbnail on **`geofastmap:mosaic_footprint_subtask_queue`**, wait in waves (`MOSAIC_FOOTPRINT_DISTRIBUTED_WAVE`), and merge GeoJSON back into the job result. If a wave returns fewer results than expected (no consumers or timeout), the parent **falls back** to in-process [`attach_footprint_displays_to_plan_result`](../app/services/mosaic_preview_footprint.py).

- **`MOSAIC_FOOTPRINT_SUBJOB_WORKER_CONCURRENCY`**: concurrent footprint subtasks **per mosaic worker process**; **`0`** means use **`MOSAIC_SUBJOB_WORKER_CONCURRENCY`**. Shard hosts that only run STAC subtasks should still set subjob concurrency (or a positive footprint concurrency) so the footprint queue is drained.
- **`MOSAIC_FOOTPRINT_DISTRIBUTED_TIMEOUT_SECONDS`**: per-wave `BRPOP` budget (like subtask rounds).

**Coordinator CPU (one parent job)**

The greedy **`plan_mosaic_from_features`** step (Shapely, selection) is **CPU-heavy**; a **single** parent job still runs that phase as **one** greedy computation at a time (it is offloaded to a worker thread so the asyncio loop can keep handling subtasks/footprints on **mixed** workers). It does **not** split one greedy pass across many cores.

**Using many cores on one machine**

- Run **several mosaic worker processes** (separate OS processes / containers) on the same host so **different** queued parent jobs use **different** CPUs. Root **`docker-compose.yml`** uses one named `mosaic_worker` container; to scale replicas with Compose, remove `container_name` from that service or run additional worker processes another way (systemd, multiple stacks).
- Raise **`MOSAIC_SUBJOB_WORKER_CONCURRENCY`** on shard boxes (and **`MOSAIC_SUBJOB_BBOX_DATETIME_PARALLELISM`** on the coordinator to match total fleet throughput) so STAC subtasks keep many cores busy during collection waves—watch upstream **429** and latency.
- For **local-only** footprint attach, raise **`MOSAIC_FOOTPRINT_CPU_MAX_CONCURRENT`** (and **`MOSAIC_FOOTPRINT_FETCH_MAX_CONCURRENT`**) so thumbnail decode/geometry uses more `asyncio.to_thread` capacity. With **distributed footprints**, that load moves to workers that dequeue the footprint queue.
- Optionally set **`MOSAIC_WORKER_MAX_CONCURRENT` > `1`** in one process to overlap **multiple** parent jobs (each greedy phase still ~one thread at a time per job).

**Other useful flags**

| Variable | Purpose |
|----------|---------|
| `MOSAIC_PARENT_FAIL_ON_PARTIAL` | If `true`, parent fails when a wave times out before all subtask results arrive (`false` = best-effort merge). |
| `MOSAIC_SUBJOB_RESULT_TTL_SECONDS` | Redis TTL for subtask result keys; keep above worst-case wave duration. |
| `MOSAIC_SUBJOB_MAX_RETRIES` | Retry policy for failed subtasks (see job/subtask code). |
| `MOSAIC_JOB_CLIENT_TIMEOUT_SECONDS` | How long clients should wait on async job polling; unrelated to subtask round timeout but should exceed typical plan duration. |
| `MOSAIC_FOOTPRINT_DISTRIBUTED_ENABLED` | Offload thumbnail `footprint_display` to Redis workers (async jobs only). |
| `MOSAIC_FOOTPRINT_DISTRIBUTED_WAVE` / `MOSAIC_FOOTPRINT_DISTRIBUTED_TIMEOUT_SECONDS` | Footprint subtask batch size and per-wave timeout. |
| `MOSAIC_FOOTPRINT_SUBJOB_WORKER_CONCURRENCY` | Per-process footprint consumer slots; `0` = reuse `MOSAIC_SUBJOB_WORKER_CONCURRENCY`. |

**Sample env**

Annotated defaults and coordinator/shard examples: [`deploy/env/workers.sample`](../deploy/env/workers.sample). Mirror **`MOSAIC_SUBJOB_*`** (and queue/redis mode) on [`deploy/env/api.sample`](../deploy/env/api.sample) when the API enqueues mosaic jobs.

**Coverage gaps (holes) and “only N scenes”**

When the mosaic finishes with visible holes or fewer images than expected, check the plan result: **`void_fill_stopped`**, **`uncovered_fraction`**, **`stac_feature_pool_size`**, **`void_fill_rounds`**, and **`same_seven_day_window`**.

Tuning (no code):

- **`MOSAIC_STAC_FETCH_LIMIT`** — each STAC `/search` is capped; raise if the pool drops granules that would fill gaps (watch catalog limits and latency).
- **`MOSAIC_VOID_FILL_MAX_ROUNDS`**, **`MOSAIC_VOID_PINPOINT_MAX_PARTS`** — more rounds / more gap pinpoints improve hole targeting.
- **`MOSAIC_GREEDY_MIN_MARGINAL_COVERAGE_FRACTION`** — lower (e.g. `0.001`) or **`0`** so greedy keeps adding scenes for thin slivers ( **`0`** disables the marginal-gain early stop).
- **Same-pass date strips** — narrows the candidate pool to a sliding 7-day window; for difficult AOIs try turning it off or widening **`date_start` / `date_end`**.

Implementation note: void-fill rounds after the first replan with **same-pass** mode **no longer apply the initial locked week filter** to the candidate set, so granules merged from wider STAC slices can participate in gap fill (response may include **`void_fill_relaxed_date_lock`: true** on later rounds). The first round still picks and records the initial window.

---

## Summary

| You want | Do this |
|----------|---------|
| **Fastest** | `docker compose up` at repo root. |
| **Advanced multi-machine (WIP)** | [`lab/geofast-distributed-experiment.md`](lab/geofast-distributed-experiment.md) |
| **NFS on primary (share tiles/bulk/rasters with workers)** | [`lab/nfs-host-ubuntu.md`](lab/nfs-host-ubuntu.md) |
| **Compose/env details** | [`deploy/README.md`](../deploy/README.md) |
| **Distributed mosaic workers (Redis subjobs)** | [§ above](#distributed-mosaic-planning-redis-subjobs) and [`deploy/env/workers.sample`](../deploy/env/workers.sample) |

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
