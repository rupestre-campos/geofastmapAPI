"""Derived revision for collection static tiles (MBTiles artifact fingerprint)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def compute_collection_tiles_revision(collection_id: str, pmtiles_path: str | None) -> str | None:
    """SHA-256 hex of collection_id + tile path + mtime + size; None when file is missing."""
    if not pmtiles_path:
        return None
    path = Path(pmtiles_path)
    if not path.exists():
        return None
    stat = path.stat()
    base = f"{collection_id}:{path}:{stat.st_mtime}:{stat.st_size}"
    return hashlib.sha256(base.encode()).hexdigest()
