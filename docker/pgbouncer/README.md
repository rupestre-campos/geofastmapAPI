# PgBouncer

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
