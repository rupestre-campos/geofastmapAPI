"""Resolve static MBTiles paths on disk (DB path or canonical tiles_storage_path layout)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.config import get_settings


def default_mbtiles_path(collection_id: str) -> Path:
    return Path(get_settings().tiles_storage_path) / f"{collection_id}.mbtiles"


def resolve_mbtiles_path(collection_id: str, pmtiles_path: str | None) -> Path | None:
    """Return an existing MBTiles file path for a collection, or None."""
    if pmtiles_path:
        path = Path(pmtiles_path)
        if path.is_file():
            return path
    canonical = default_mbtiles_path(collection_id)
    if canonical.is_file():
        return canonical
    return None


def has_static_mbtiles_file(collection_id: str, pmtiles_path: str | None) -> bool:
    return resolve_mbtiles_path(collection_id, pmtiles_path) is not None


def read_mbtiles_zoom_range(path: Path) -> tuple[int, int]:
    """Read minzoom/maxzoom from MBTiles metadata; fall back to app defaults."""
    settings = get_settings()
    fallback_min = settings.tippecanoe_minzoom
    fallback_max = settings.tippecanoe_maxzoom
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "SELECT name, value FROM metadata WHERE name IN ('minzoom', 'maxzoom')"
            ).fetchall()
            meta = {str(k): str(v) for k, v in rows}
            minz = int(meta["minzoom"]) if "minzoom" in meta else fallback_min
            maxz = int(meta["maxzoom"]) if "maxzoom" in meta else fallback_max
            return minz, maxz
        finally:
            conn.close()
    except Exception:
        return fallback_min, fallback_max
