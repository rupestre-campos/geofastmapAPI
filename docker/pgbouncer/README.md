# PgBouncer

## Single machine: Postgres + Redis + API in Docker

**If they are already one Compose stack** (e.g. root [`docker-compose.yml`](../../docker-compose.yml) or your own file that defines `db`, `redis`, `api` on the same network):

- Prefer running **PgBouncer in that same compose file** on the **same** `default` network as `db`, with backend `postgres://...@db:5432/...` (or `@geofastmap_db:5432` if that is the container name). Then set the API **`DATABASE_URL`** to `...@pgbouncer:5432/...` and **`DATABASE_USE_PGBOUNCER=true`** — no host port required between API and pooler.
- Do **not** run `docker compose up -d pgbouncer` from the **root** compose if that compose also defines `db` and you already have `geofastmap_db` from another project; Compose will try to create a second `db`. Either use one project for everything or use **standalone** pooler compose below.

**If Postgres is already up** and you add PgBouncer **without** recreating `db`:** use [`deploy/compose/docker-compose.pgbouncer.yml`](../../deploy/compose/docker-compose.pgbouncer.yml) plus [`docker-compose.pgbouncer.db-network.yml`](../../deploy/compose/docker-compose.pgbouncer.db-network.yml) so PgBouncer joins the DB stack’s network and talks to **`geofastmap_db:5432`**. Copy [`deploy/env/pgbouncer.sample`](../../deploy/env/pgbouncer.sample) → `.env.pgbouncer` and run the two-file `docker compose` command shown in [`deploy/compose/README.md`](../../deploy/compose/README.md).

**Split compose on one host** ([`docker-compose.db.yml`](../../deploy/compose/docker-compose.db.yml) + [`docker-compose.redis.yml`](../../deploy/compose/docker-compose.redis.yml) + [`docker-compose.api.yml`](../../deploy/compose/docker-compose.api.yml), all `name: geofastmap_api`): same network **`geofastmap_api_default`**. Start PgBouncer with [`docker-compose.pgbouncer.db-network.yml`](../../deploy/compose/docker-compose.pgbouncer.db-network.yml), then in **`deploy/env/.env.api`**: `DATABASE_URL=...@geofastmap_pgbouncer:5432/...`, `DATABASE_USE_PGBOUNCER=true`, `DATABASE_URL_DIRECT=...@geofastmap_db:5432/...`. Full commands: [`deploy/compose/README.md`](../../deploy/compose/README.md).

**API container** (same host, other layouts): set `DATABASE_URL=postgresql+asyncpg://...@geofastmap_pgbouncer:5432/...` if API is on the **same** Docker network as the pooler. If the API only reaches the host loopback, use `...@host.docker.internal:6432/...` or the server LAN IP and published **`PGBOUNCER_PUBLISH`** (default **6432**).

---

## Standalone (Postgres already running — avoids `geofastmap_db` name conflict)

If **`docker compose up -d pgbouncer`** from the root [`docker-compose.yml`](../../docker-compose.yml) tries to recreate **`geofastmap_db`** because `pgbouncer` depends on `db`, use the **pooler-only** compose file instead:

```bash
cp deploy/env/pgbouncer.sample deploy/env/.env.pgbouncer
# Set PGBOUNCER_BACKEND_DATABASE_URL (libpq postgres://...) to your real Postgres.
docker compose --env-file deploy/env/.env.pgbouncer -f deploy/compose/docker-compose.pgbouncer.yml up -d
```

- Default **host port** for clients: **6432** → pooler port 5432 inside the container (`PGBOUNCER_PUBLISH` overrides).
- Backend URL often uses **`host.docker.internal:5434`** when Postgres is mapped on the host as `5434:5432` (the sample uses this; Linux gets `host-gateway` via `extra_hosts` in the compose file).
- If Postgres is another **container** on the same host, attach PgBouncer to that stack’s **external** Docker network (see comments in [`deploy/compose/docker-compose.pgbouncer.yml`](../../deploy/compose/docker-compose.pgbouncer.yml)) and set `...@geofastmap_db:5432/...`.

**App env:** `DATABASE_URL=postgresql+asyncpg://USER:PASS@<pooler-host>:6432/DB` and **`DATABASE_USE_PGBOUNCER=true`**. Migrations: **`DATABASE_URL_DIRECT`** (or **`ALEMBIC_DATABASE_URL`**) to Postgres **directly** (not through PgBouncer transaction pool).

Image tag: see [Docker Hub — edoburu/pgbouncer](https://hub.docker.com/r/edoburu/pgbouncer/tags) if a pinned tag fails to pull.

---

## Full stack (root `docker-compose.yml`)

The all-in-one compose runs **edoburu/pgbouncer** between app containers and the bundled `db` service.

- **Clients** (api, workers): `DATABASE_URL=postgresql+asyncpg://...@pgbouncer:5432/geofastmap` and `DATABASE_USE_PGBOUNCER=true` (disables asyncpg statement cache for transaction pooling).
- **Migrations** (`alembic upgrade head`): use `DATABASE_URL_DIRECT=postgresql+asyncpg://...@db:5432/geofastmap` or the same value in `ALEMBIC_DATABASE_URL` so DDL hits Postgres directly.

Split / external Postgres: use the standalone compose above, or any PgBouncer install; point `DATABASE_URL` at the pooler and set `DATABASE_USE_PGBOUNCER=true` when using transaction pooling.
