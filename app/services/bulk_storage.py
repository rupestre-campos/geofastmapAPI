"""Bulk upload storage abstraction. Filesystem now; S3/object store via config later."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import BinaryIO

from app.core.config import get_settings


class BulkStorageBackend(ABC):
    """Interface for storing bulk uploads. Implementations: filesystem, (future) S3."""

    @abstractmethod
    def get_write_path(self, key: str) -> str:
        """Path to write to (filesystem); caller streams upload here. Creates parent dirs."""
        ...

    @abstractmethod
    def get_path_or_uri(self, key: str) -> str:
        """Path (filesystem) or URI/URL (S3) for worker to read the file."""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove file after processing (optional cleanup)."""
        ...


class FilesystemStorage(BulkStorageBackend):
    """Store files under a directory. Shared volume for API and worker."""

    def __init__(self, base_path: str) -> None:
        self.base_path = base_path.rstrip("/")

    def _path(self, key: str) -> str:
        # key is e.g. job_id.geojson; avoid path traversal
        if ".." in key or key.startswith("/"):
            raise ValueError("Invalid storage key")
        return os.path.join(self.base_path, key)

    def get_write_path(self, key: str) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    def get_path_or_uri(self, key: str) -> str:
        return self._path(key)

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def get_bulk_storage() -> BulkStorageBackend:
    settings = get_settings()
    if settings.bulk_storage_type == "filesystem":
        return FilesystemStorage(settings.bulk_storage_path)
    if settings.bulk_storage_type == "s3":
        raise NotImplementedError("S3 storage: set BULK_STORAGE_TYPE=filesystem or add S3 backend")
    return FilesystemStorage(settings.bulk_storage_path)
