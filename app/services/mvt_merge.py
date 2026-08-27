"""Merge static MVT tiles from multiple member collections into one PBF."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import mapbox_vector_tile

from app.utils.geo import mvt_layer_name
from app.utils.tile_bbox import tile_bbox_mercator

_MVT_EXTENT = 4096


def read_tile_from_mbtiles(path: Path, z: int, x: int, y: int) -> bytes | None:
    """Read and decompress one tile from MBTiles (sync)."""
    import gzip
    import sqlite3

    tms_row = (1 << z) - 1 - y
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, tms_row),
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    raw = row[0]
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def merge_mvt_tiles(
    tile_bytes_list: list[bytes],
    output_layer_name: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    """Decode member PBFs and encode one MVT with all features under output_layer_name."""
    merged_features: list[dict[str, Any]] = []
    for raw in tile_bytes_list:
        if not raw:
            continue
        try:
            decoded = mapbox_vector_tile.decode(raw)
        except Exception:
            continue
        for layer_features in decoded.values():
            if not layer_features:
                continue
            for feat in layer_features:
                merged_features.append(
                    {
                        "geometry": feat.get("geometry"),
                        "properties": feat.get("properties") or {},
                    }
                )
    if not merged_features:
        return b""
    tile_merc = tile_bbox_mercator(z, x, y)
    return mapbox_vector_tile.encode(
        [{"name": output_layer_name, "features": merged_features}],
        default_options={
            "extents": _MVT_EXTENT,
            "quantize_bounds": tile_merc,
        },
    )


def compute_composite_tiles_revision(member_revisions: list[str | None]) -> str:
    """Stable revision hash from member tiles_revision values (order matters)."""
    parts = [r or "" for r in member_revisions]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
