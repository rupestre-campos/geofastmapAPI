# Scripts

## `topodata_import.py`

Linear script: load **`scripts/.env`**, read **`scripts/tiles_topodata.geojson`** (a `FeatureCollection` with **`properties.scene`** per tile, e.g. `20S525`), build download URLs

`http://www.dsr.inpe.br/topodata/data/geotiff/{scene}ZN.zip`,

ensure collection **`topodata`** exists (raster DEM), then for each scene download the ZIP and **`POST /collections/{id}/rasters/batch`**. Retries up to 5 times on HTTP **500** (and network errors) with backoff.

### Files

| File | Role |
|------|------|
| `scripts/.env` | `GEOFASTMAP_API_BASE`, `GEOFASTMAP_USER`, `GEOFASTMAP_PASSWORD` |
| `scripts/tiles_topodata.geojson` | Tile index (`properties.scene`) |

Optional env: **`GEOFASTMAP_COLLECTION`** (default `topodata`), **`GEOFASTMAP_SOURCE_CRS`**, **`TOPODATA_TILES_GEOJSON`** (path to another GeoJSON).

### Setup

`cp scripts/.env.example scripts/.env` and fill credentials. Install **`httpx`** from [requirements.txt](../requirements.txt). Run the bulk worker so raster jobs ingest.

### Run

```bash
python3 scripts/topodata_import.py
```

Respect INPE/site terms and bandwidth.
