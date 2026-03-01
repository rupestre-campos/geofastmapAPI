"""Bulk import of geospatial files into a collection. Runs in a background thread (sync)."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime
from typing import Callable

from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.collection import Collection
from app.models.feature import Feature
from app.utils.geo import geojson_to_wkt_element

# Driver for fiona by file extension (lowercase). No shapefile (would require sidecar files).
# .geojsonseq is the same format as .geojsonl (GeoJSON Seq / newline-delimited GeoJSON).
# Shapefile is supported only inside a .zip via GDAL /vsizip (no extraction).
_FIONA_DRIVERS: dict[str, str] = {
    ".kml": "KML",
    ".gpkg": "GPKG",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".geojsonl": "GeoJSONSeq",
    ".geojsonseq": "GeoJSONSeq",
    ".jsonl": "GeoJSONSeq",
}


def _driver_for_path(path: str) -> str | None:
    """Return fiona driver for file path, or None if unsupported."""
    ext = os.path.splitext(path)[1].lower()
    return _FIONA_DRIVERS.get(ext)


def _resolve_zip_shapefile(zip_path: str) -> tuple[str, str]:
    """
    Resolve path to open for a .zip that contains a shapefile. Reads without extracting.
    Returns (path_to_open, driver) for fiona.open(path_to_open, driver=driver).
    path_to_open uses GDAL /vsizip/ so the .shp is read from the archive.
    Raises ValueError if no .shp found in the zip.
    """
    zip_path = os.path.abspath(zip_path)
    with zipfile.ZipFile(zip_path, "r") as z:
        shp_names = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if not shp_names:
        raise ValueError("ZIP contains no .shp file")
    # Prefer root-level .shp (zip namelist uses forward slash)
    root_shps = [n for n in shp_names if "/" not in n]
    inner = root_shps[0] if root_shps else shp_names[0]
    # GDAL /vsizip/ expects path/to/archive.zip/path/inside.zip
    open_path = f"/vsizip/{zip_path}/{inner}"
    return open_path, "ESRI Shapefile"


def run_bulk_import_sync(
    file_path: str,
    collection_id: str,
    mode: str,
    batch_size: int,
    on_progress: Callable[[str, int, int | None], None] | None = None,
) -> tuple[int, int, str | None]:
    """
    Read a geospatial file with fiona and insert features into the collection.
    Runs in a sync context (e.g. thread). Does not block the async event loop.

    Args:
        file_path: Path to uploaded file (e.g. .kml, .gpkg, .geojson, .geojsonl, .geojsonseq, or .zip with shapefile).
        collection_id: Target collection id.
        mode: "append" or "replace". Replace deletes all existing features first.
        batch_size: Number of features per DB commit.
        on_progress: Optional callback(status, items_created, total_or_none) for job updates.

    Returns:
        (items_created, items_failed, error_message).
        error_message is set if a fatal error occurred.
    """
    import fiona

    settings = get_settings()
    sync_url = settings.database_sync_url
    engine = create_engine(sync_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    # Resolve path and driver: direct file or .zip containing a shapefile (read via GDAL /vsizip)
    if file_path.lower().endswith(".zip"):
        try:
            open_path, driver = _resolve_zip_shapefile(file_path)
        except ValueError as e:
            return 0, 0, str(e)
    else:
        driver = _driver_for_path(file_path)
        if not driver:
            return 0, 0, (
                f"Unsupported file type. Supported: "
                f"{', '.join(sorted(set(_FIONA_DRIVERS.values())))} or .zip (shapefile inside)"
            )
        open_path = file_path

    try:
        with SessionLocal() as session:
            # Ensure collection exists
            from app.models.collection import Collection
            coll = session.get(Collection, collection_id)
            if not coll:
                return 0, 0, "Collection not found"

            if mode == "replace":
                if on_progress:
                    on_progress("replacing", 0, None)
                session.execute(delete(Feature).where(Feature.collection_id == collection_id))
                session.execute(
                    update(Collection).where(Collection.id == collection_id).values(feature_count=0)
                )
                session.commit()

            if on_progress:
                on_progress("running", 0, None)

            created = 0
            failed = 0
            batch: list[Feature] = []
            now = datetime.utcnow()

            with fiona.open(open_path, driver=driver) as src:
                for rec in src:
                    try:
                        geom_dict = rec.get("geometry")
                        props = dict(rec.get("properties") or {})
                        wkt = geojson_to_wkt_element(geom_dict, srid=4326)
                        # Id is always generated by the model (UUID); source/layer ids are ignored.
                        feat = Feature(
                            collection_id=collection_id,
                            geometry=wkt,
                            properties=props if props else None,
                            created_at=now,
                            updated_at=now,
                        )
                        batch.append(feat)
                        created += 1
                    except Exception:
                        failed += 1
                        continue

                    if len(batch) >= batch_size:
                        session.bulk_save_objects(batch)
                        session.commit()
                        if on_progress:
                            on_progress("running", created, None)
                        batch = []

                if batch:
                    session.bulk_save_objects(batch)
                    session.commit()

            # Update collection feature_count (replace: set to created; append: add created)
            if mode == "replace":
                session.execute(
                    update(Collection).where(Collection.id == collection_id).values(feature_count=created)
                )
            else:
                session.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(feature_count=Collection.feature_count + created)
                )
            session.commit()

            if on_progress:
                on_progress("completed", created, created)
            return created, failed, None

    except Exception as e:
        if on_progress:
            on_progress("failed", 0, None)
        return 0, 0, str(e)
    finally:
        engine.dispose()
