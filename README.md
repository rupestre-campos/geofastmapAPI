## GeoFast API

OGC API - Features–style service built with **FastAPI**, **PostgreSQL**, and **SQLAlchemy**, following an MVC-ish layout:

- `app/models`: ORM models (`Collection`, `Feature`)
- `app/schemas`: Pydantic schemas (collections, Feature, FeatureCollection)
- `app/crud`: data access and business logic
- `app/api/routes`: FastAPI routers for collections and items
- `alembic`: database migrations
- `tests`: async API tests (PostgreSQL, same DB URL as dev by default)

### Requirements

- Docker and Docker Compose (for containerized run)
- Python 3.10+ (for local/dev usage)

### Installation (local dev)

```bash
git clone <this-repo>
cd geofast_api

python -m venv .venv
source .venv/bin/activate

# install runtime + dev deps (tests, coverage, etc.)
pip install -r requirements-dev.txt
```

### Running the API with Docker Compose

From the project root:

```bash
docker compose up --build
```

This will:

- Start **PostgreSQL** on host port `5434` (container port `5432`)
- Run Alembic migrations
- Start the FastAPI app on `http://localhost:8000`

Open the interactive docs:

- Swagger UI: `http://localhost:8000/docs`

To stop:

```bash
docker compose down
```

### Running the API locally (without Docker)

1. Ensure you have a PostgreSQL database and set `DATABASE_URL` (or adjust `database_url` in `app/core/config.py`).
2. Run Alembic migrations:

```bash
alembic upgrade head
```

3. Start the dev server:

```bash
uvicorn app.main:app --reload
```

Then open `http://localhost:8000/docs`.

### Running tests

Tests use **PostgreSQL** (same URL as dev by default, e.g. `localhost:5434/geofast`) because the Feature model uses PostGIS geometry and JSONB. Start the DB first (e.g. `docker compose up -d db`), then run:

```bash
pytest
```

Override the test DB with `TEST_DATABASE_URL` if needed.

### Running tests with coverage

First make sure dev deps are installed:

```bash
pip install -r requirements-dev.txt
```

Then run pytest with coverage:

```bash
pytest --cov=app --cov-report=term-missing --cov-report=html
```

This will:

- Show a line-by-line coverage summary in the terminal
- Generate an HTML report under `htmlcov/`

Open the HTML coverage report in a browser:

```bash
xdg-open htmlcov/index.html  # Linux (or open via your file browser)
```

