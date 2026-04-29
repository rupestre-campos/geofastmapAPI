"""Bulk upload storage abstraction. Filesystem now; S3/object store via config later."""

from __future__ import annotations

import os
import shutil
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

    @abstractmethod
    def get_chunk_part_path(self, upload_id: str, part_no: int) -> str:
        """Filesystem path for a staged chunk part."""
        ...

    @abstractmethod
    def assemble_chunk_parts(self, upload_id: str, part_numbers: list[int], final_key: str) -> str:
        """Assemble chunk parts into final object key and return output path/uri."""
        ...

    @abstractmethod
    def delete_upload_parts(self, upload_id: str) -> None:
        """Delete staged parts directory for upload session."""
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

    def _upload_dir(self, upload_id: str) -> str:
        if ".." in upload_id or "/" in upload_id or upload_id.startswith("."):
            raise ValueError("Invalid upload id")
        return os.path.join(self.base_path, "_uploads", upload_id)

    def get_write_path(self, key: str) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    def get_path_or_uri(self, key: str) -> str:
        return self._path(key)

    def delete(self, key: str) -> None:
        """Remove uploaded file after processing. Idempotent; ignores missing or permission errors."""
        path = self._path(key)
        try:
            if os.path.isfile(path):
                os.unlink(path)
        except OSError:
            pass

    def get_chunk_part_path(self, upload_id: str, part_no: int) -> str:
        if part_no < 1:
            raise ValueError("part_no must be >= 1")
        pdir = self._upload_dir(upload_id)
        os.makedirs(pdir, exist_ok=True)
        return os.path.join(pdir, f"part-{part_no:08d}.bin")

    def assemble_chunk_parts(self, upload_id: str, part_numbers: list[int], final_key: str) -> str:
        if not part_numbers:
            raise ValueError("No parts to assemble")
        out_path = self.get_write_path(final_key)
        with open(out_path, "wb") as out:
            for pn in sorted(set(int(x) for x in part_numbers)):
                part_path = self.get_chunk_part_path(upload_id, pn)
                if not os.path.isfile(part_path):
                    raise FileNotFoundError(f"Missing upload part {pn}")
                with open(part_path, "rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
        return out_path

    def delete_upload_parts(self, upload_id: str) -> None:
        pdir = self._upload_dir(upload_id)
        try:
            if os.path.isdir(pdir):
                shutil.rmtree(pdir, ignore_errors=True)
        except OSError:
            pass


def get_bulk_storage() -> BulkStorageBackend:
    settings = get_settings()
    if settings.bulk_storage_type == "filesystem":
        return FilesystemStorage(settings.bulk_storage_path)
    if settings.bulk_storage_type == "s3":
        raise NotImplementedError("S3 storage: set BULK_STORAGE_TYPE=filesystem or add S3 backend")
    return FilesystemStorage(settings.bulk_storage_path)
