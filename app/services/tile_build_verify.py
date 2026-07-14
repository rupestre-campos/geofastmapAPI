"""Verify MBTiles artifacts after tippecanoe builds."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIN_MBTILES_BYTES = 4096


def verify_mbtiles_artifact(path: str | Path, *, min_bytes: int = MIN_MBTILES_BYTES) -> str | None:
    """Return an error message when the built file is missing or unusable; None when OK."""
    p = Path(path)
    if not p.is_file():
        return f"MBTiles file missing after build: {p}"
    size = p.stat().st_size
    if size < min_bytes:
        return f"MBTiles file too small ({size} bytes, expected at least {min_bytes}): {p}"
    try:
        uri = f"{p.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()
            tile_count = int(row[0]) if row else 0
            if tile_count <= 0:
                return f"MBTiles contains no tiles (0 rows in tiles table): {p}"
        finally:
            conn.close()
    except Exception as exc:
        return f"MBTiles is not a valid SQLite tile archive: {p} ({exc})"
    return None


def format_build_success_message(path: str | Path, *, feature_count: int | None = None) -> str:
    p = Path(path)
    size = p.stat().st_size if p.is_file() else 0
    parts = [f"Built {p} ({size:,} bytes)"]
    if feature_count is not None:
        parts.append(f"{feature_count:,} features")
    return "; ".join(parts)
