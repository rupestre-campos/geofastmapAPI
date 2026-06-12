"""Bulk import of geospatial files into a collection. Runs in a background thread (sync)."""

from __future__ import annotations

import os
import random
import time
import zipfile
from datetime import datetime
from collections.abc import Sequence
from typing import Callable

from sqlalchemy import and_, create_engine, delete, literal_column, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from shapely.geometry import shape, GeometryCollection, MultiPoint, MultiLineString, MultiPolygon, Point, LineString, Polygon
from shapely.validation import make_valid
from uuid6 import uuid7

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.db.feature_property_filters import structured_filter_clause
from app.db.features_partitions import resolve_features_partition_relname_sync
from app.models.collection import Collection
from app.models.feature import Feature
from app.utils.property_filters import PropertyFilter
from app.utils.feature_subdivide import (
    MAX_COORDS_FOR_DB_SUBDIVIDE,
    _coord_count,
    insert_feature_parts_batched,
    insert_feature_subdivided_sql,
    subdivide_geometry_by_vertices,
)
from app.utils.geometry_limits import geometry_exceeds_limit
from app.services.bulk_collection_activity import (
    decr_collection_bulk_activity,
    get_collection_bulk_mutex_holder,
    incr_collection_bulk_activity,
    refresh_collection_bulk_mutex,
)
from app.services.job_store import get_job

class BulkImportCancelled(Exception):
    """Raised when the bulk job is marked cancelled in job_store (cooperative stop)."""


def _is_retryable_db_error(err: Exception) -> bool:
    if isinstance(err, (OperationalError, DBAPIError)):
        return True
    msg = str(err).lower()
    return any(
        frag in msg
        for frag in (
            "server closed the connection unexpectedly",
            "connection reset",
            "connection refused",
            "could not connect",
            "network is unreachable",
            "terminating connection",
            "connection is closed",
            "ssl syscall error",
        )
    )


def _retry_wait_seconds(attempt_idx: int, *, base: float, max_seconds: float) -> float:
    raw = base * (2 ** max(0, attempt_idx - 1))
    wait = min(max_seconds, raw)
    return wait * (1.0 + random.uniform(0.0, 0.15))


def _run_db_retry(
    label: str,
    fn: Callable[[], None],
    *,
    on_retry: Callable[[int, float, str], None] | None = None,
) -> None:
    settings = get_settings()
    max_attempts = max(1, int(getattr(settings, "bulk_db_retry_max_attempts", 4) or 4))
    base = max(0.1, float(getattr(settings, "bulk_db_retry_base_seconds", 1.0) or 1.0))
    max_backoff = max(base, float(getattr(settings, "bulk_db_retry_max_seconds", 30.0) or 30.0))
    for attempt in range(1, max_attempts + 1):
        try:
            fn()
            return
        except Exception as e:
            if attempt >= max_attempts or not _is_retryable_db_error(e):
                raise
            wait = _retry_wait_seconds(attempt, base=base, max_seconds=max_backoff)
            if on_retry:
                on_retry(attempt, wait, f"{label}: {type(e).__name__}: {e}")
            time.sleep(wait)


def _raise_if_bulk_cancelled(bulk_import_job_id: str | None) -> None:
    if not bulk_import_job_id:
        return
    j = get_job(bulk_import_job_id)
    if j is not None and j.status == "cancelled":
        raise BulkImportCancelled()


def _delete_where_clause(collection_id: str, filters: Sequence[PropertyFilter] | None):
    clauses = [Feature.collection_id == collection_id]
    if filters:
        for pf in filters:
            clauses.append(structured_filter_clause(pf))
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def _delete_features_batched_sync(
    engine: Engine,
    collection_id: str,
    filters: Sequence[PropertyFilter] | None = None,
    *,
    bulk_import_job_id: str | None = None,
    on_progress: Callable[[str, int, int | None], None] | None = None,
) -> int:
    """Delete feature rows in small commits; honors cooperative cancel between batches."""
    settings = get_settings()
    batch = max(500, int(getattr(settings, "bulk_replace_delete_batch_rows", 25000) or 25000))
    deleted_total = 0
    ctid_col = literal_column("ctid")
    partition = resolve_features_partition_relname_sync(engine, collection_id)

    def _one_batch() -> int:
        removed = 0
        if partition and not filters:
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        f"""
                        DELETE FROM "{partition}" WHERE ctid IN (
                            SELECT ctid FROM "{partition}" LIMIT :lim
                        )
                        """
                    ),
                    {"lim": batch},
                )
                removed += int(result.rowcount or 0)
                conn.commit()
        where_clause = _delete_where_clause(collection_id, filters)
        with engine.connect() as conn:
            subq = select(ctid_col).where(where_clause).limit(batch)
            result = conn.execute(delete(Feature).where(ctid_col.in_(subq)))
            removed += int(result.rowcount or 0)
            conn.commit()
        return removed

    while True:
        _raise_if_bulk_cancelled(bulk_import_job_id)
        n = _run_db_retry("replace_delete_batch", _one_batch)
        if n <= 0:
            break
        deleted_total += n
        holder = get_collection_bulk_mutex_holder(collection_id)
        if holder:
            refresh_collection_bulk_mutex(collection_id, holder)
        if on_progress:
            on_progress("replacing", 0, deleted_total)
        if n < batch:
            break
    return deleted_total


def _update_feature_count_sync(engine: Engine, collection_id: str) -> None:
    """Recompute collections.feature_count from features (COUNT DISTINCT id)."""
    def _run() -> None:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(DISTINCT id) AS n FROM features WHERE collection_id = :cid"
                ),
                {"cid": collection_id},
            ).first()
            n = int(row.n) if row and row.n is not None else 0
            conn.execute(
                text("UPDATE collections SET feature_count = :n WHERE id = :cid"),
                {"cid": collection_id, "n": n},
            )
            conn.commit()

    _run_db_retry("feature_count_update", _run)


def _sync_delete_bulk_import_rows_and_refresh(engine: Engine, collection_id: str, job_id: str) -> None:
    """Remove rows tagged with this bulk job and refresh cached count + extent."""
    def _delete_rows() -> None:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "DELETE FROM features WHERE collection_id = :cid AND bulk_import_job_id = :jid"
                ),
                {"cid": collection_id, "jid": job_id},
            )
            conn.commit()

    _run_db_retry("cancel_cleanup_delete", _delete_rows)
    _update_feature_count_sync(engine, collection_id)
    try:
        collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id)
    except Exception:
        pass


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
    heartbeat_seconds: float,
    commit_with_retry: Callable[[Session], Session],
    bulk_import_job_id: str | None = None,
) -> tuple[int, int]:
    """Read one fiona source and insert features with ST_Subdivide (≤256 vertices/row). Returns (created, failed)."""
    import fiona

    created = 0
    failed = 0
    s = get_settings()
    max_vertices = s.features_subdivide_max_vertices
    insert_parts_batch_size = max(1, int(getattr(s, "bulk_insert_parts_batch_size", 160) or 160))
    last_heartbeat = time.monotonic()

    # On multi-host NFS, a freshly uploaded ZIP can be briefly visible in directory listing
    # but not yet fully readable by GDAL on another worker. Retry a few times before failing.
    last_open_err: Exception | None = None
    src = None
    for attempt in range(5):
        try:
            src = fiona.open(open_path, driver=driver)
            break
        except Exception as e:
            last_open_err = e
            if attempt >= 4:
                raise
            time.sleep(0.35 * (attempt + 1))
    if src is None and last_open_err is not None:
        raise last_open_err

    with src:
        for rec in src:
            _raise_if_bulk_cancelled(bulk_import_job_id)
            if on_progress and heartbeat_seconds > 0:
                elapsed = time.monotonic() - last_heartbeat
                if elapsed >= heartbeat_seconds:
                    on_progress("running", created_so_far + created, None)
                    last_heartbeat = time.monotonic()
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
                _raise_if_bulk_cancelled(bulk_import_job_id)
                if geometry_exceeds_limit(geom):
                    failed += 1
                    continue
                try:
                    # Keep the outer transaction healthy even if a single feature insert fails
                    # (e.g. invalid geometry/properties). This avoids "current transaction is aborted".
                    with session.begin_nested():
                        fid = str(uuid7())
                        if (
                            geom is not None
                            and not geom.is_empty
                            and _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE
                        ):
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
                                fid,
                                collection_id,
                                wkt_list,
                                props if props else None,
                                now,
                                bulk_import_job_id=bulk_import_job_id,
                                batch_size=insert_parts_batch_size,
                            ):
                                session.execute(text(sql), params)
                        else:
                            wkt = (
                                geom.wkt
                                if (geom is not None and not geom.is_empty)
                                else None
                            )
                            sql, params = insert_feature_subdivided_sql(
                                fid,
                                collection_id,
                                wkt,
                                props if props else None,
                                now,
                                max_vertices,
                                bulk_import_job_id=bulk_import_job_id,
                            )
                            session.execute(text(sql), params)
                    created += 1
                except BulkImportCancelled:
                    raise
                except Exception:
                    failed += 1
                    continue

            if created > 0 and created % batch_size == 0:
                session = commit_with_retry(session)
                _raise_if_bulk_cancelled(bulk_import_job_id)
                if on_progress:
                    on_progress("running", created_so_far + created, None)
                    last_heartbeat = time.monotonic()

        if created > 0 and created % batch_size != 0:
            session = commit_with_retry(session)
            _raise_if_bulk_cancelled(bulk_import_job_id)
            if on_progress:
                last_heartbeat = time.monotonic()
    return created, failed


def run_bulk_import_sync(
    file_path: str,
    collection_id: str,
    mode: str,
    batch_size: int,
    on_progress: Callable[[str, int, int | None], None] | None = None,
    zip_inner_shp_paths: list[str] | None = None,
    bulk_import_job_id: str | None = None,
    finalize_collection: bool = True,
    replace_filters: Sequence[PropertyFilter] | None = None,
    replace_prestaged: bool = False,
) -> tuple[int, int, str | None]:
    """
    Read a geospatial file with fiona and insert features into the collection.
    Runs in a sync context (e.g. thread). Does not block the async event loop.

    Args:
        file_path: Path to uploaded file (e.g. .kml, .gpkg, .geojson, .geojsonl, .geojsonseq, or .zip with shapefile).
        collection_id: Target collection id.
        mode: "append", "replace", or "replace_filtered". Replace deletes all existing features first.
            replace_filtered deletes rows matching replace_filters first (unless replace_prestaged).
        replace_filters: Structured property filters for replace_filtered mode.
        replace_prestaged: When True, skip delete (already done by replace_collection_prestage_sync).
        batch_size: Number of features per DB commit.
        on_progress: Optional callback(status, items_created, total_or_none) for job updates.
        zip_inner_shp_paths: When file_path is .zip, list of .shp member paths to import (all of them).
            If non-empty, all listed shapefiles are imported in order into the same collection.
        bulk_import_job_id: When set, rows are tagged for rollback if the job is cancelled.

    Returns:
        (items_created, items_failed, error_message).
        error_message is "cancelled" if the user cancelled; otherwise set if a fatal error occurred.
    """
    incr_collection_bulk_activity(collection_id)
    engine: Engine | None = None
    try:
        import fiona

        settings = get_settings()
        sync_url = settings.database_sync_url
        engine = create_engine(sync_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        heartbeat_seconds = max(0.0, float(getattr(settings, "bulk_progress_heartbeat_seconds", 5.0) or 0.0))

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
    
                def _retry_notice(attempt: int, wait: float, reason: str) -> None:
                    if on_progress:
                        on_progress("running", 0, None)
                    print(
                        f"[bulk-import] retry attempt={attempt} wait={wait:.2f}s reason={reason}",
                        flush=True,
                    )
    
                def _commit_with_retry(sess: Session) -> Session:
                    def _do_commit() -> None:
                        sess.commit()
    
                    try:
                        _run_db_retry("bulk_commit", _do_commit, on_retry=_retry_notice)
                        return sess
                    except Exception:
                        try:
                            sess.rollback()
                        except Exception:
                            pass
                        raise
    
                def _execute_with_retry(fn: Callable[[], None], label: str) -> None:
                    def _wrapped() -> None:
                        try:
                            fn()
                        except Exception:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            raise
    
                    _run_db_retry(label, _wrapped, on_retry=_retry_notice)
    
                _raise_if_bulk_cancelled(bulk_import_job_id)

                finalize_mode = mode
                if mode == "replace_filtered":
                    finalize_mode = "append"
                    if not replace_prestaged and replace_filters:
                        if on_progress:
                            on_progress("replacing", 0, None)

                        def _filtered_delete() -> None:
                            _delete_features_batched_sync(
                                engine,
                                collection_id,
                                replace_filters,
                                bulk_import_job_id=bulk_import_job_id,
                                on_progress=on_progress,
                            )

                        _execute_with_retry(_filtered_delete, "replace_filtered_delete_features")
                        session = _commit_with_retry(session)
                        _update_feature_count_sync(engine, collection_id)
                elif mode == "replace":
                    if on_progress:
                        on_progress("replacing", 0, None)

                    def _batched_replace_delete() -> None:
                        _delete_features_batched_sync(
                            engine,
                            collection_id,
                            bulk_import_job_id=bulk_import_job_id,
                            on_progress=on_progress,
                        )

                    _execute_with_retry(_batched_replace_delete, "replace_delete_features")
                    _execute_with_retry(
                        lambda: session.execute(
                            update(Collection).where(Collection.id == collection_id).values(feature_count=0)
                        ),
                        "replace_reset_feature_count",
                    )
                    session = _commit_with_retry(session)
    
                if on_progress:
                    on_progress("running", 0, None)
    
                total_created = 0
                total_failed = 0
                now = datetime.utcnow()
    
                for open_path, driver in sources:
                    _raise_if_bulk_cancelled(bulk_import_job_id)
                    created, failed = _import_one_source(
                        session,
                        open_path,
                        driver,
                        collection_id,
                        batch_size,
                        on_progress,
                        total_created,
                        now,
                        heartbeat_seconds,
                        _commit_with_retry,
                        bulk_import_job_id=bulk_import_job_id,
                    )
                    total_created += created
                    total_failed += failed
    
                if finalize_collection:
                    if finalize_mode == "replace":
                        _execute_with_retry(
                            lambda: session.execute(
                                update(Collection).where(Collection.id == collection_id).values(feature_count=total_created)
                            ),
                            "replace_finalize_feature_count",
                        )
                    else:
                        _execute_with_retry(
                            lambda: session.execute(
                                update(Collection)
                                .where(Collection.id == collection_id)
                                .values(feature_count=Collection.feature_count + total_created)
                            ),
                            "append_finalize_feature_count",
                        )
                    session = _commit_with_retry(session)
    
                    extent_mode = str(getattr(settings, "bulk_extent_update_mode", "immediate") or "immediate").lower()
                    if extent_mode not in ("immediate", "deferred", "best_effort"):
                        extent_mode = "immediate"
                    if extent_mode != "deferred":
                        try:
                            _run_db_retry(
                                "extent_recompute",
                                lambda: collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id),
                                on_retry=_retry_notice,
                            )
                        except Exception:
                            if extent_mode == "immediate":
                                raise
    
                if on_progress:
                    on_progress("completed", total_created, total_created)
                return total_created, total_failed, None

        except BulkImportCancelled:
            if bulk_import_job_id:
                _sync_delete_bulk_import_rows_and_refresh(engine, collection_id, bulk_import_job_id)
            return 0, 0, "cancelled"
        except Exception as e:
            if on_progress:
                on_progress("failed", 0, None)
            return 0, 0, str(e)
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        decr_collection_bulk_activity(collection_id)


def finalize_collection_import_sync(collection_id: str) -> None:
    """Finalize a collection after sharded append imports."""
    incr_collection_bulk_activity(collection_id)
    engine: Engine | None = None
    try:
        settings = get_settings()
        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        _update_feature_count_sync(engine, collection_id)
        extent_mode = str(getattr(settings, "bulk_extent_update_mode", "immediate") or "immediate").lower()
        if extent_mode not in ("immediate", "deferred", "best_effort"):
            extent_mode = "immediate"
        if extent_mode != "deferred":
            try:
                _run_db_retry(
                    "extent_recompute",
                    lambda: collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id),
                )
            except Exception:
                if extent_mode == "immediate":
                    raise
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        decr_collection_bulk_activity(collection_id)


def delete_features_by_filters_sync(
    collection_id: str,
    filters: Sequence[PropertyFilter],
    *,
    bulk_import_job_id: str | None = None,
    on_progress: Callable[[str, int, int | None], None] | None = None,
) -> None:
    """Delete feature rows matching structured property filters; refresh count and extent."""
    if not filters:
        raise ValueError("replace_filters required for filtered delete")
    incr_collection_bulk_activity(collection_id)
    engine: Engine | None = None
    try:
        settings = get_settings()
        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        _delete_features_batched_sync(
            engine,
            collection_id,
            filters,
            bulk_import_job_id=bulk_import_job_id,
            on_progress=on_progress,
        )
        _update_feature_count_sync(engine, collection_id)
        extent_mode = str(getattr(settings, "bulk_extent_update_mode", "immediate") or "immediate").lower()
        if extent_mode not in ("immediate", "deferred", "best_effort"):
            extent_mode = "immediate"
        if extent_mode != "deferred":
            try:
                _run_db_retry(
                    "extent_recompute",
                    lambda: collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id),
                )
            except Exception:
                if extent_mode == "immediate":
                    raise
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        decr_collection_bulk_activity(collection_id)


def replace_collection_prestage_sync(
    collection_id: str,
    replace_filters: Sequence[PropertyFilter] | None = None,
    *,
    bulk_import_job_id: str | None = None,
    on_progress: Callable[[str, int, int | None], None] | None = None,
) -> None:
    """Delete existing rows before sharded replace imports (full collection or filtered subset)."""
    if replace_filters:
        delete_features_by_filters_sync(
            collection_id,
            replace_filters,
            bulk_import_job_id=bulk_import_job_id,
            on_progress=on_progress,
        )
        return
    incr_collection_bulk_activity(collection_id)
    engine: Engine | None = None
    try:
        settings = get_settings()
        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        _delete_features_batched_sync(
            engine,
            collection_id,
            bulk_import_job_id=bulk_import_job_id,
            on_progress=on_progress,
        )
        with SessionLocal() as session:
            session.execute(update(Collection).where(Collection.id == collection_id).values(feature_count=0))
            session.commit()
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        decr_collection_bulk_activity(collection_id)
