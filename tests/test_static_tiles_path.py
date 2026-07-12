"""Tests for static MBTiles path resolution."""

from pathlib import Path

from app.services.static_tiles_path import default_mbtiles_path, resolve_mbtiles_path


def test_resolve_mbtiles_path_prefers_db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.static_tiles_path.get_settings",
        lambda: type("S", (), {"tiles_storage_path": str(tmp_path)})(),
    )
    db_path = tmp_path / "custom.mbtiles"
    db_path.write_bytes(b"sqlite")
    resolved = resolve_mbtiles_path("my-layer", str(db_path))
    assert resolved == db_path


def test_resolve_mbtiles_path_falls_back_to_canonical(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.static_tiles_path.get_settings",
        lambda: type("S", (), {"tiles_storage_path": str(tmp_path)})(),
    )
    canonical = default_mbtiles_path("car-area_imovel-composite")
    canonical.write_bytes(b"sqlite")
    resolved = resolve_mbtiles_path("car-area_imovel-composite", None)
    assert resolved == canonical


def test_resolve_mbtiles_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.static_tiles_path.get_settings",
        lambda: type("S", (), {"tiles_storage_path": str(tmp_path)})(),
    )
    assert resolve_mbtiles_path("missing", "/no/such/file.mbtiles") is None
