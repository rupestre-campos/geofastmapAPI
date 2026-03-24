"""Run tippecanoe to produce a single tile (z/x/y) from GeoJSON. Optimized for low latency."""

from __future__ import annotations

import gzip
import os
import sqlite3
import subprocess
import tempfile

from app.core.config import get_settings


def _read_tile_from_mbtiles(path: str, z: int, x: int, y: int) -> bytes | None:
    """Read one tile from mbtiles (TMS row order). Returns decompressed pbf or None."""
    tms_row = (1 << z) - 1 - y
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, tms_row),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    raw = row[0]
    if raw is None:
        return None
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def _temp_dir_fast() -> str:
    """Use /dev/shm on Linux for faster I/O when available."""
    try:
        if os.path.exists("/dev/shm") and os.path.isdir("/dev/shm"):
            return tempfile.mkdtemp(prefix="geofastmap_tile_", dir="/dev/shm")
    except OSError:
        pass
    return tempfile.mkdtemp(prefix="geofastmap_tile_")


def build_single_tile(
    geojson_bytes: bytes,
    collection_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """
    Run tippecanoe to build tiles at zoom z only; read the tile at (z, x, y); return pbf bytes.
    Optimized for low latency: stdin input, no drop/simplification, low detail, fast temp dir.
    """
    tippecanoe_bin = getattr(get_settings(), "tippecanoe_path", None) or "tippecanoe"
    tmpdir = _temp_dir_fast()
    mbtiles_path = os.path.join(tmpdir, "out.mbtiles")
    fast_opts = [
        "--force",
        "--full-detail=6",
        "--low-detail=4",
        "--minimum-detail=2",
    ]
    try:
        cmd = [
            tippecanoe_bin,
            "-o", mbtiles_path,
            f"-z{z}",
            f"-Z{z}",
            f"--layer={collection_id}",
            *fast_opts,
            "-",  # read GeoJSON from stdin
        ]
        proc = subprocess.run(
            cmd,
            input=geojson_bytes,
            capture_output=True,
            timeout=30,
            cwd=tmpdir,
        )
        if proc.returncode == 0:
            tile_bytes = _read_tile_from_mbtiles(mbtiles_path, z, x, y)
            return tile_bytes if tile_bytes is not None else b""
        # Fallback: some builds don't accept stdin; use temp file
        geojson_path = os.path.join(tmpdir, "input.geojson")
        with open(geojson_path, "wb") as f:
            f.write(geojson_bytes)
        cmd_file = [
            tippecanoe_bin,
            "-o", mbtiles_path,
            f"-z{z}",
            f"-Z{z}",
            f"--layer={collection_id}",
            *fast_opts,
            geojson_path,
        ]
        proc = subprocess.run(cmd_file, capture_output=True, timeout=30, cwd=tmpdir)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tippecanoe failed: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        tile_bytes = _read_tile_from_mbtiles(mbtiles_path, z, x, y)
        return tile_bytes if tile_bytes is not None else b""
    finally:
        try:
            for name in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)
        except OSError:
            pass
