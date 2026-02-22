## GeoFast API

OGC API - Features–style service built with **FastAPI**, **PostgreSQL**, and **SQLAlchemy**, following an MVC-ish layout:


- `app/models`: ORM models (`Collection`, `Feature`)
- `app/schemas`: Pydantic schemas (collections, Feature, FeatureCollection)
- `app/crud`: data access and business logic
- `app/api/routes`: FastAPI routers for collections and items
- `alembic`: database migrations
- `tests`: async API tests with **mocked DB** (no database or Docker required)

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

**Items endpoint** (`GET /collections/{id}/items`) supports OGC-style query parameters (aligned with [QGIS OGC API Features](https://docs.qgis.org/3.40/en/docs/server_manual/services/ogcapif.html)):

- **limit** / **offset** – Pagination (default limit from config, max 1000). Response includes `numberMatched`, `numberReturned` and **next** / **prev** links.
- **bbox** – Bounding box filter: `minx,miny,maxx,maxy` (WGS84). Uses PostGIS spatial index.
- **datetime** – Filter by feature `created_at`: instant (e.g. `2024-01-01`) or range (`2024-01-01/2024-12-31`).
- **sortby** – Sort by `id`, `created_at`, or any property name.
- **sortdesc** – Sort descending when `true`.
- **Attribute filtering** – Any other query param is treated as a property filter: `?name=Main%20St` (exact), `?name=*St` (ends with), `?name=Main*` (starts with). Multiple filters are ANDed.
- **Attribute selection** – `?properties=name,area` returns only those keys in each feature’s `properties`.

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

Tests use an **in-memory mock** for the database (no PostgreSQL or Docker required). Run:

```bash
pytest
```

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

