# Deploy layout

**Default:** run the full stack from the repo root with [`docker-compose.yml`](../docker-compose.yml) (`docker compose up`). See **[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)**.

| Path | What it is |
|------|------------|
| [`compose/`](compose/) | Optional Compose files to start **only** DB, Redis, API, workers, or Titiler—useful when splitting across machines. |
| [`env/`](env/) | [`api.sample`](env/api.sample) and [`workers.sample`](env/workers.sample); copy to `deploy/env/.env.api` and `.env.workers` (see [`env/README.md`](env/README.md)). |
| [`nginx/`](nginx/) | Example nginx upstream for several Titiler backends. |

**Advanced setup (WIP):** multi-machine on owned hardware—**[`docs/lab/geofast-distributed-experiment.md`](../docs/lab/geofast-distributed-experiment.md)**.

If you use Kubernetes later, reuse the same images and env as in `compose/`—this repo does not ship cluster YAML.
