"""Parallel GeoJSONSeq / shapefile parse + PostgreSQL COPY into staging tables."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from uuid6 import uuid7

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.db.features_partitions import ensure_features_partition_sync
from app.services.bulk_collection_activity import (
    decr_collection_bulk_activity,
    incr_collection_bulk_activity,
)
from app.services.bulk_import import (
    BulkImportCancelled,
    _driver_for_path,
    _explode_to_simple_parts,
    _resolve_zip_shapefile,
    list_shp_in_zip,
)
from app.services.bulk_staging import (
    create_staging_table_sync,
    drop_staging_table_sync,
    promote_staging_sync,
    staging_table_name,
)
from app.services.bulk_triggers import refresh_collection_features_last_updated_sync
from app.services.job_store import get_job
from app.utils.feature_subdivide import MAX_COORDS_FOR_DB_SUBDIVIDE, subdivide_geometry_by_vertices, _coord_count
from app.utils.geometry_limits import geometry_exceeds_limit
from shapely import set_srid, to_wkb
from shapely.geometry import shape


def _raise_if_cancelled(job_id: str | None) -> None:
    if not job_id:
        return
    job = get_job(job_id)
    if job and job.status == "cancelled":
        raise BulkImportCancelled()


def _parser_worker_count() -> int:
    settings = get_settings()
    configured = int(getattr(settings, "bulk_copy_parser_workers", 0) or 0)
    if configured > 0:
        return configured
    cpus = os.cpu_count() or 2
    return max(1, cpus - 1)


def _copy_batch_size() -> int:
    return max(1000, int(getattr(get_settings(), "bulk_copy_batch_rows", 50000) or 50000))


def _geojson_seq_extensions() -> frozenset[str]:
    return frozenset({".geojsonl", ".geojsonseq", ".jsonl"})


def _is_geojson_seq_path(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in _geojson_seq_extensions()


def _feature_rows_from_record(
    rec: dict,
    *,
    collection_id: str,
    job_id: str | None,
    now: datetime,
    max_vertices: int,
) -> tuple[list[tuple], int]:
    """Return (rows, failed_count) where each row is COPY-ready tuple."""
    rows: list[tuple] = []
    failed = 0
    geom_dict = rec.get("geometry")
    props = dict(rec.get("properties") or {})
    fid = rec.get("id")
    if fid is not None:
        props.setdefault("id", fid)
    if not geom_dict:
        return rows, 1
    try:
        base_geom = shape(geom_dict)
        geoms = _explode_to_simple_parts(base_geom)
        if not geoms:
            return rows, 1
    except Exception:
        return rows, 1
    feature_id = str(fid) if fid is not None else str(uuid7())
    ts = now.isoformat()
    job_tag = job_id or None
    props_json = json.dumps(props, separators=(",", ":"), ensure_ascii=False)
    part_index = 0
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if geometry_exceeds_limit(geom):
            failed += 1
            continue
        parts: list
        if _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
            parts = [
                p
                for p in subdivide_geometry_by_vertices(geom, max_vertices)
                if p is not None and not p.is_empty and not geometry_exceeds_limit(p)
            ]
        else:
            parts = [geom]
        if not parts:
            failed += 1
            continue
        for part in parts:
            wkb_hex = to_wkb(set_srid(part, 4326), hex=True, include_srid=True)
            rows.append(
                (
                    feature_id,
                    collection_id,
                    part_index,
                    wkb_hex,
                    props_json,
                    job_tag,
                    ts,
                    ts,
                )
            )
            part_index += 1
    if part_index == 0:
        return rows, max(1, failed)
    return rows, failed


def _parse_line_bytes(
    line: bytes,
    *,
    collection_id: str,
    job_id: str | None,
    now_iso: str,
    max_vertices: int,
) -> tuple[list[tuple], int]:
    import orjson

    now = datetime.fromisoformat(now_iso)
    if not line.strip():
        return [], 0
    try:
        rec = orjson.loads(line)
    except Exception:
        return [], 1
    if not isinstance(rec, dict):
        return [], 1
    return _feature_rows_from_record(
        rec,
        collection_id=collection_id,
        job_id=job_id,
        now=now,
        max_vertices=max_vertices,
    )


def _split_file_line_chunks(path: str, n_parts: int) -> list[tuple[int, int]]:
    """Byte ranges aligned to newline boundaries for parallel read."""
    size = os.path.getsize(path)
    if size <= 0 or n_parts <= 1:
        return [(0, size)]
    chunk = max(1, size // n_parts)
    ranges: list[tuple[int, int]] = []
    with open(path, "rb") as f:
        start = 0
        while start < size:
            end = min(size, start + chunk)
            if end < size:
                f.seek(end)
                f.readline()
                end = f.tell()
            ranges.append((start, end))
            start = end
    return ranges


def _read_chunk_lines(path: str, start: int, end: int) -> list[bytes]:
    lines: list[bytes] = []
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(max(0, end - start))
    if start > 0:
        first_nl = data.find(b"\n")
        if first_nl >= 0:
            data = data[first_nl + 1 :]
        else:
            data = b""
    for line in data.split(b"\n"):
        if line:
            lines.append(line)
    return lines


def _dbapi_connection(conn) -> object:
    sa_conn = conn.connection
    if hasattr(sa_conn, "dbapi_connection") and sa_conn.dbapi_connection is not None:
        return sa_conn.dbapi_connection
    if hasattr(sa_conn, "connection"):
        return sa_conn.connection
    return sa_conn


def _copy_rows_to_staging(conn, staging: str, rows: list[tuple]) -> None:
    if not rows:
        return
    raw_conn = _dbapi_connection(conn)
    buf = io.StringIO()
    for row in rows:
        parts = []
        for val in row:
            if val is None:
                parts.append("\\N")
            else:
                s = str(val)
                s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                parts.append(s)
        buf.write("\t".join(parts) + "\n")
    buf.seek(0)
    cur = raw_conn.cursor()
    try:
        cur.copy_expert(
            f"""
            COPY "{staging}" (
                id, collection_id, part_index, geometry, properties,
                bulk_import_job_id, created_at, updated_at
            )
            FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')
            """,
            buf,
        )
    finally:
        cur.close()


def _load_geojson_seq_into_staging(
    engine: Engine,
    *,
    path: str,
    collection_id: str,
    job_id: str,
    staging: str,
    on_progress: Callable[[str, int, int | None], None] | None,
) -> tuple[int, int]:
    settings = get_settings()
    max_vertices = max(1, int(settings.features_subdivide_max_vertices or 256))
    batch_size = _copy_batch_size()
    heartbeat = max(0.0, float(getattr(settings, "bulk_progress_heartbeat_seconds", 5.0) or 5.0))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    workers = _parser_worker_count()
    created = 0
    failed = 0
    last_hb = time.monotonic()

    chunks = _split_file_line_chunks(path, workers)
    pending_rows: list[tuple] = []

    def flush(force: bool = False) -> None:
        nonlocal pending_rows, created, last_hb
        if not pending_rows:
            return
        if not force and len(pending_rows) < batch_size:
            return
        _raise_if_cancelled(job_id)
        with engine.begin() as conn:
            _copy_rows_to_staging(conn, staging, pending_rows)
        created += len(pending_rows)
        pending_rows = []
        if on_progress and heartbeat > 0 and (time.monotonic() - last_hb) >= heartbeat:
            on_progress("running", created, None)
            last_hb = time.monotonic()

    if len(chunks) <= 1:
        with open(path, "rb") as f:
            for raw_line in f:
                _raise_if_cancelled(job_id)
                rows, fail = _parse_line_bytes(
                    raw_line,
                    collection_id=collection_id,
                    job_id=job_id,
                    now_iso=now_iso,
                    max_vertices=max_vertices,
                )
                failed += fail
                pending_rows.extend(rows)
                if len(pending_rows) >= batch_size:
                    flush(force=True)
        flush(force=True)
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = []
            for start, end in chunks:
                lines = _read_chunk_lines(path, start, end)
                futures.append(
                    pool.submit(
                        _parse_lines_batch,
                        lines,
                        collection_id,
                        job_id,
                        now_iso,
                        max_vertices,
                    )
                )
            for fut in as_completed(futures):
                _raise_if_cancelled(job_id)
                rows, fail = fut.result()
                failed += fail
                pending_rows.extend(rows)
                while len(pending_rows) >= batch_size:
                    batch = pending_rows[:batch_size]
                    pending_rows = pending_rows[batch_size:]
                    with engine.begin() as conn:
                        _copy_rows_to_staging(conn, staging, batch)
                    created += len(batch)
                    if on_progress and heartbeat > 0 and (time.monotonic() - last_hb) >= heartbeat:
                        on_progress("running", created, None)
                        last_hb = time.monotonic()
        flush(force=True)

    return created, failed


def _parse_lines_batch(
    lines: list[bytes],
    collection_id: str,
    job_id: str | None,
    now_iso: str,
    max_vertices: int,
) -> tuple[list[tuple], int]:
    rows: list[tuple] = []
    failed = 0
    for line in lines:
        part_rows, fail = _parse_line_bytes(
            line,
            collection_id=collection_id,
            job_id=job_id,
            now_iso=now_iso,
            max_vertices=max_vertices,
        )
        rows.extend(part_rows)
        failed += fail
    return rows, failed


def _load_fiona_source_into_staging(
    engine: Engine,
    *,
    open_path: str,
    driver: str,
    collection_id: str,
    job_id: str,
    staging: str,
    on_progress: Callable[[str, int, int | None], None] | None,
) -> tuple[int, int]:
    import fiona

    settings = get_settings()
    max_vertices = max(1, int(settings.features_subdivide_max_vertices or 256))
    batch_size = _copy_batch_size()
    heartbeat = max(0.0, float(getattr(settings, "bulk_progress_heartbeat_seconds", 5.0) or 5.0))
    now = datetime.now(timezone.utc)
    created = 0
    failed = 0
    pending_rows: list[tuple] = []
    last_hb = time.monotonic()

    def flush(force: bool = False) -> None:
        nonlocal pending_rows, created, last_hb
        if not pending_rows:
            return
        if not force and len(pending_rows) < batch_size:
            return
        _raise_if_cancelled(job_id)
        with engine.begin() as conn:
            _copy_rows_to_staging(conn, staging, pending_rows)
        created += len(pending_rows)
        pending_rows = []
        if on_progress and heartbeat > 0 and (time.monotonic() - last_hb) >= heartbeat:
            on_progress("running", created, None)
            last_hb = time.monotonic()

    with fiona.open(open_path, driver=driver) as src:
        for feat in src:
            _raise_if_cancelled(job_id)
            props = dict(feat.get("properties") or {})
            geom_dict = feat.get("geometry")
            rec = {"type": "Feature", "geometry": geom_dict, "properties": props}
            fid = props.get("id") or feat.get("id")
            if fid is not None:
                rec["id"] = fid
            rows, fail = _feature_rows_from_record(
                rec,
                collection_id=collection_id,
                job_id=job_id,
                now=now,
                max_vertices=max_vertices,
            )
            failed += fail
            pending_rows.extend(rows)
            if len(pending_rows) >= batch_size:
                flush(force=True)
    flush(force=True)
    return created, failed


def _finalize_after_promote(engine: Engine, collection_id: str) -> None:
    from app.services.bulk_import import _update_feature_count_sync

    _update_feature_count_sync(engine, collection_id)
    settings = get_settings()
    extent_mode = str(getattr(settings, "bulk_extent_update_mode", "deferred") or "deferred").lower()
    if extent_mode != "deferred":
        try:
            collections_crud.recompute_and_update_collection_extent_sync(engine, collection_id)
        except Exception:
            if extent_mode == "immediate":
                raise
    if getattr(settings, "bulk_skip_features_touch_trigger", True):
        refresh_collection_features_last_updated_sync(engine, collection_id)


def run_bulk_copy_import_sync(
    file_path: str,
    collection_id: str,
    mode: str,
    job_id: str,
    on_progress: Callable[[str, int, int | None], None] | None = None,
    zip_inner_shp_paths: list[str] | None = None,
) -> tuple[int, int, str | None]:
    """
    COPY-based bulk import into staging, then promote to live partition.
    mode: append | replace (replace_filtered not supported).
    """
    if mode not in ("append", "replace"):
        return 0, 0, f"Unsupported bulk copy mode: {mode}"

    if not os.path.isfile(file_path):
        return 0, 0, f"Upload file not found: {file_path}"
    try:
        if os.path.getsize(file_path) <= 0:
            return 0, 0, f"Upload file is empty: {file_path}"
    except OSError as e:
        return 0, 0, f"Cannot read upload file {file_path}: {e}"

    incr_collection_bulk_activity(collection_id)
    engine: Engine | None = None
    created = 0
    failed = 0
    try:
        settings = get_settings()
        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        ensure_features_partition_sync(engine, collection_id)
        with engine.begin() as conn:
            staging = create_staging_table_sync(conn, job_id)

        if on_progress:
            on_progress("running", 0, None)

        is_zip = file_path.lower().endswith(".zip")

        if is_zip:
            inner = zip_inner_shp_paths
            if not inner:
                try:
                    inner = list_shp_in_zip(file_path)
                except Exception:
                    inner = None
            if not inner:
                try:
                    open_path, driver = _resolve_zip_shapefile(file_path)
                    inner_paths = [(open_path, driver)]
                except ValueError as e:
                    return 0, 0, str(e)
            else:
                zip_path = os.path.abspath(file_path)
                inner_paths = [(f"/vsizip/{zip_path}/{shp}", "ESRI Shapefile") for shp in inner]
            for i, (open_path, driver) in enumerate(inner_paths):
                if on_progress and len(inner_paths) > 1:
                    on_progress("running", created, None)
                c, f = _load_fiona_source_into_staging(
                    engine,
                    open_path=open_path,
                    driver=driver,
                    collection_id=collection_id,
                    job_id=job_id,
                    staging=staging,
                    on_progress=on_progress,
                )
                created += c
                failed += f
        elif _is_geojson_seq_path(file_path):
            created, failed = _load_geojson_seq_into_staging(
                engine,
                path=file_path,
                collection_id=collection_id,
                job_id=job_id,
                staging=staging,
                on_progress=on_progress,
            )
        else:
            driver = _driver_for_path(file_path)
            if not driver:
                return 0, 0, "Unsupported file type for COPY ingest"
            created, failed = _load_fiona_source_into_staging(
                engine,
                open_path=file_path,
                driver=driver,
                collection_id=collection_id,
                job_id=job_id,
                staging=staging,
                on_progress=on_progress,
            )

        _raise_if_cancelled(job_id)
        if on_progress:
            on_progress("running", created, None)

        promote_staging_sync(engine, collection_id=collection_id, job_id=job_id, mode=mode)
        _finalize_after_promote(engine, collection_id)
        return created, failed, None
    except BulkImportCancelled:
        if engine is not None:
            try:
                drop_staging_table_sync(engine, job_id)
            except Exception:
                pass
        return created, failed, "cancelled"
    except Exception as e:
        if engine is not None:
            try:
                drop_staging_table_sync(engine, job_id)
            except Exception:
                pass
        return created, failed, str(e)
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        decr_collection_bulk_activity(collection_id)
