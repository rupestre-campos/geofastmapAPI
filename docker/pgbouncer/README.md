# PgBouncer (root `docker-compose.yml` only)

The default stack runs **edoburu/pgbouncer** (pinned tag in `docker-compose.yml`; check [Docker Hub tags](https://hub.docker.com/r/edoburu/pgbouncer/tags) if pull fails) between app containers and Postgres so many processes share a bounded number of server connections.

- **Clients** (api, workers): `DATABASE_URL=postgresql+asyncpg://...@pgbouncer:5432/geofastmap` and `DATABASE_USE_PGBOUNCER=true` (disables asyncpg statement cache for transaction pooling).
- **Migrations** (`alembic upgrade head`): use `DATABASE_URL_DIRECT=postgresql+asyncpg://...@db:5432/geofastmap` or the same value in `ALEMBIC_DATABASE_URL` so DDL hits Postgres directly.

Split / external Postgres: run PgBouncer yourself or use a managed pooler; point `DATABASE_URL` at the pooler and set `DATABASE_USE_PGBOUNCER=true` when using transaction pooling.
