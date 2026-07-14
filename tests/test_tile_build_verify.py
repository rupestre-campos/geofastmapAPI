"""Tests for MBTiles build verification."""

import sqlite3
from pathlib import Path

from app.services.tile_build_verify import format_build_success_message, verify_mbtiles_artifact


def _write_minimal_mbtiles(path: Path, *, with_tiles: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE metadata (name text, value text)")
        conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
        if with_tiles:
            conn.execute(
                "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (0, 0, 0, ?)",
                (b"\x1f\x8b\x08",),
            )
        conn.commit()
    finally:
        conn.close()
    # Pad file so it passes default min size check.
    with path.open("ab") as fh:
        fh.write(b"\x00" * 5000)


def test_verify_mbtiles_artifact_ok(tmp_path):
    path = tmp_path / "ok.mbtiles"
    _write_minimal_mbtiles(path)
    assert verify_mbtiles_artifact(path) is None


def test_verify_mbtiles_artifact_missing_file(tmp_path):
    err = verify_mbtiles_artifact(tmp_path / "missing.mbtiles")
    assert err is not None
    assert "missing" in err.lower()


def test_verify_mbtiles_artifact_no_tile_rows(tmp_path):
    path = tmp_path / "empty.mbtiles"
    _write_minimal_mbtiles(path, with_tiles=False)
    err = verify_mbtiles_artifact(path)
    assert err is not None
    assert "no tiles" in err.lower()


def test_format_build_success_message(tmp_path):
    path = tmp_path / "layer.mbtiles"
    _write_minimal_mbtiles(path)
    msg = format_build_success_message(path, feature_count=42)
    assert "layer.mbtiles" in msg
    assert "42 features" in msg
