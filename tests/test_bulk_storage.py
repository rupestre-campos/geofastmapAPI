"""Tests for app.services.bulk_storage (filesystem backend)."""
import os
import tempfile
import pytest
from app.services.bulk_storage import FilesystemStorage, get_bulk_storage
from app.core.config import get_settings


def test_filesystem_storage_get_write_path():
    with tempfile.TemporaryDirectory() as tmp:
        storage = FilesystemStorage(tmp)
        path = storage.get_write_path("job1.geojson")
        assert path == os.path.join(tmp, "job1.geojson")
        assert os.path.isdir(os.path.dirname(path))


def test_filesystem_storage_write_and_get_path():
    with tempfile.TemporaryDirectory() as tmp:
        storage = FilesystemStorage(tmp)
        key = "test.geojson"
        write_path = storage.get_write_path(key)
        with open(write_path, "wb") as f:
            f.write(b'{"type":"FeatureCollection","features":[]}')
        assert storage.get_path_or_uri(key) == write_path


def test_filesystem_storage_delete():
    with tempfile.TemporaryDirectory() as tmp:
        storage = FilesystemStorage(tmp)
        path = storage.get_write_path("del.geojson")
        with open(path, "wb") as f:
            f.write(b"x")
        storage.delete("del.geojson")
        assert not os.path.exists(path)


def test_filesystem_storage_delete_missing_no_error():
    with tempfile.TemporaryDirectory() as tmp:
        storage = FilesystemStorage(tmp)
    storage.delete("nonexistent.geojson")  # no raise


def test_filesystem_storage_invalid_key_raises():
    with tempfile.TemporaryDirectory() as tmp:
        storage = FilesystemStorage(tmp)
        with pytest.raises(ValueError):
            storage.get_write_path("../../../etc/passwd")
        with pytest.raises(ValueError):
            storage.get_write_path("/absolute")


def test_get_bulk_storage_returns_filesystem(monkeypatch):
    monkeypatch.setenv("BULK_STORAGE_TYPE", "filesystem")
    monkeypatch.setenv("BULK_STORAGE_PATH", "/tmp/bulk")
    get_settings.cache_clear()
    try:
        storage = get_bulk_storage()
        assert isinstance(storage, FilesystemStorage)
        assert storage.base_path == "/tmp/bulk"
    finally:
        get_settings.cache_clear()
