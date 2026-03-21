"""Bulk import of geospatial files into a collection. Runs in a background thread (sync)."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime
from typing import Callable

from sqlalchemy import create_engine, delete, text, update
from sqlalchemy.orm import Session, sessionmaker
from shapely.geometry import shape, GeometryCollection, MultiPoint, MultiLineString, MultiPolygon, Point, LineString, Polygon
from shapely.validation import make_valid
from uuid6 import uuid7

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.models.collection import Collection
from app.models.feature import Feature
from app.utils.feature_subdivide import (
    MAX_COORDS_FOR_DB_SUBDIVIDE,
    _coord_count,
    insert_feature_parts_batched,
    insert_feature_subdivided_sql,
    subdivide_geometry_by_vertices,
)
from app.utils.geometry_limits import geometry_exceeds_limit

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


def list_shp_in_zip(zip_path: str) -> list[str]:
    """
    List all .shp member paths inside a zip (including in subfolders).
    Returns a list of names as in ZipFile.namelist() (forward slashes).
    """
    with zipfile.ZipFile(zip_path, "r") as z:
        return [n for n in z.namelist() if n.lower().endswith(".shp")]


def _resolve_zip_shapefile(zip_path: str, inner_path: str | None = None) -> tuple[str, str]:
    """
    Resolve path to open for a .zip that contains a shapefile. Reads without extracting.
    Returns (path_to_open, driver) for fiona.open(path_to_open, driver=driver).
    path_to_open uses GDAL /vsizip/ so the .shp is read from the archive.
    If inner_path is given, use that member; otherwise pick one (root-level preferred).
    Raises ValueError if no .shp found in the zip.
    """
    zip_path = os.path.abspath(zip_path)
    with zipfile.ZipFile(zip_path, "r") as z:
        shp_names = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if not shp_names:
        raise ValueError("ZIP contains no .shp file")
    if inner_path is not None:
        if inner_path not in shp_names:
            raise ValueError(f"ZIP does not contain {inner_path!r}")
        inner = inner_path
    else:
        root_shps = [n for n in shp_names if "/" not in n]
        inner = root_shps[0] if root_shps else shp_names[0]
    open_path = f"/vsizip/{zip_path}/{inner}"
    return open_path, "ESRI Shapefile"


def _explode_to_simple_parts(geom) -> list:
    """
    Break multi-part and collection geometries into single-part geometries
    (Point, LineString, Polygon) for one row per simple shape in the database.
    Recurses into GeometryCollection. Invalid geometries are repaired when possible.
    """
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
        if geom is None or geom.is_empty:
            return []
    if isinstance(geom, Point):
        return [geom]
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPoint):
        return [g for g in geom.geoms if g is not None and not g.is_empty]
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if g is not None and not g.is_empty]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if g is not None and not g.is_empty]
    if isinstance(geom, GeometryCollection):
        out: list = []
        for g in geom.geoms:
            out.extend(_explode_to_simple_parts(g))
        return out
    # Fallback (e.g. LinearRing or future types): treat as atomic if possible
    try:
        if hasattr(geom, "geoms"):
            return [g for g in geom.geoms if g is not None and not g.is_empty]
    except Exception:
        pass
    return [geom]


def _import_one_source(
    session: Session,
    open_path: str,
    driver: str,
    collection_id: str,
    batch_size: int,
    on_progress: Callable[[str, int, int | None], None] | None,
    created_so_far: int,
    now: datetime,
) -> tuple[int, int]:
    """Read one fiona source and insert features with ST_Subdivide (≤256 vertices/row). Returns (created, failed)."""
    import fiona

    created = 0
    failed = 0
    max_vertices = get_settings().features_subdivide_max_vertices

    with fiona.open(open_path, driver=driver) as src:
        for rec in src:
            try:
                geom_dict = rec.get("geometry")
                props = dict(rec.get("properties") or {})
                if geom_dict:
                    base_geom = shape(geom_dict)
                    geoms = _explode_to_simple_parts(base_geom)
                    if not geoms:
                        failed += 1
                        continue
                else:
                    geoms = [None]
                for geom in geoms:
                    if geometry_exceeds_limit(geom):
                        failed += 1
                        continue
                    fid = str(uuid7())
                    if geom is not None and not geom.is_empty and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
                        parts = subdivide_geometry_by_vertices(geom, max_vertices)
                        wkt_list = []
                        for p in parts:
                            if p is None or p.is_empty:
                                continue
                            if geometry_exceeds_limit(p):
                                failed += 1
                                continue
                            wkt_list.append(p.wkt)
                        if not wkt_list:
                            continue
                        for sql, params in insert_feature_parts_batched(
                            fid, collection_id, wkt_list, props if props else None, now
                        ):
                            session.execute(text(sql), params)
                    else:
                        wkt = geom.wkt if (geom is not None and not geom.is_empty) else None
                        sql, params = insert_feature_subdivided_sql(
                            fid, collection_id, wkt, props if props else None, now, max_vertices
                        )
                        session.execute(text(sql), params)
                    created += 1
            except Exception:
                failed += 1
                continue

            if created > 0 and created % batch_size == 0:
                session.commit()
                if on_progress:
                    on_progress("running", created_so_far + created, None)

        if created > 0 and created % batch_size != 0:
            session.commit()
    return created, failed


def run_bulk_import_sync(
    file_path: str,
    collection_id: str,
    mode: str,
    batch_size: int,
    on_progress: Callable[[str, int, int | None], None] | None = None,
    zip_inner_shp_paths: list[str] | None = None,
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
        zip_inner_shp_paths: When file_path is .zip, list of .shp member paths to import (all of them).
            If non-empty, all listed shapefiles are imported in order into the same collection.

    Returns:
        (items_created, items_failed, error_message).
        error_message is set if a fatal error occurred.
    """
    import fiona

    settings = get_settings()
    sync_url = settings.database_sync_url
    engine = create_engine(sync_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    is_zip = file_path.lower().endswith(".zip")
    sources: list[tuple[str, str]] = []  # (open_path, driver)

    if is_zip:
        if zip_inner_shp_paths:
            zip_path = os.path.abspath(file_path)
            for inner in zip_inner_shp_paths:
                open_path = f"/vsizip/{zip_path}/{inner}"
                sources.append((open_path, "ESRI Shapefile"))
            if not sources:
                return 0, 0, "ZIP contains no .shp files to import"
        else:
            try:
                open_path, driver = _resolve_zip_shapefile(file_path)
                sources = [(open_path, driver)]
            except ValueError as e:
                return 0, 0, str(e)
    else:
        driver = _driver_for_path(file_path)
        if not driver:
            return 0, 0, (
                f"Unsupported file type. Supported: "
                f"{', '.join(sorted(set(_FIONA_DRIVERS.values())))} or .zip (shapefile inside)"
            )
        sources = [(file_path, driver)]

    try:
        with SessionLocal() as session:
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

            total_created = 0
            total_failed = 0
            now = datetime.utcnow()

            for open_path, driver in sources:
                created, failed = _import_one_source(
                    session, open_path, driver, collection_id, batch_size,
                    on_progress, total_created, now,
                )
                total_created += created
                total_failed += failed

            if mode == "replace":
                session.execute(
                    update(Collection).where(Collection.id == collection_id).values(feature_count=total_created)
                )
            else:
                session.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(feature_count=Collection.feature_count + total_created)
                )
            session.commit()

            # Precompute collection extent (bbox) from imported geometries before closing the engine.
            try:
                collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id)
            except Exception:
                pass

            if on_progress:
                on_progress("completed", total_created, total_created)
            return total_created, total_failed, None

    except Exception as e:
        if on_progress:
            on_progress("failed", 0, None)
        return 0, 0, str(e)
    finally:
        engine.dispose()
