"""Tests for admin storage usage aggregation helpers."""

from __future__ import annotations

from pathlib import Path

from app.services import storage_usage as su


def test_format_bytes():
    assert su.format_bytes(0) == "0 B"
    assert su.format_bytes(512) == "512 B"
    assert "KB" in su.format_bytes(2048)
    assert "MB" in su.format_bytes(5 * 1024 * 1024)


def test_compute_includes_collection_and_tiles(monkeypatch, tmp_path: Path):
    tiles = tmp_path / "tiles"
    rasters = tmp_path / "rasters"
    bulk = tmp_path / "bulk"
    tiles.mkdir()
    rasters.mkdir()
    bulk.mkdir()
    (tiles / "layer-a.mbtiles").write_bytes(b"x" * 1000)
    (rasters / "layer-a").mkdir()
    (rasters / "layer-a" / "f1.tif").write_bytes(b"y" * 500)
    (tiles / "orphan.mbtiles").write_bytes(b"z" * 200)

    monkeypatch.setattr(
        su,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "tiles_storage_path": str(tiles),
                "raster_storage_path": str(rasters),
                "bulk_storage_path": str(bulk),
                "database_sync_url": "sqlite:///:memory:",
                "bulk_queue_type": "redis",
                "redis_url": "redis://x",
            },
        )(),
    )

    # Patch DB-heavy parts: empty collections/mosaics, empty partitions
    class _Conn:
        def execute(self, *_a, **_k):
            class _R:
                def fetchall(self):
                    return []

            return _R()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Eng:
        def connect(self):
            return _Conn()

        def dispose(self):
            pass

    snap = su.compute_storage_usage_sync(engine=_Eng())
    kinds = {r["kind"] for r in snap["rows"]}
    assert "orphan_tiles" in kinds
    assert "orphan_rasters" in kinds
    orphan_tile = next(r for r in snap["rows"] if r["id"] == "orphan.mbtiles")
    assert orphan_tile["tiles_bytes"] == 200
    orphan_ras = next(r for r in snap["rows"] if r["id"] == "layer-a")
    assert orphan_ras["raster_bytes"] == 500
    assert snap["totals"]["total_bytes"] >= 700


def test_delete_orphan_tiles_rejects_path_traversal(tmp_path: Path, monkeypatch):
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    monkeypatch.setattr(
        su,
        "get_settings",
        lambda: type("S", (), {"tiles_storage_path": str(tiles)})(),
    )
    assert su.delete_orphan_tiles_file("../etc/passwd") is False
    assert su.delete_orphan_tiles_file("a/b.mbtiles") is False
