"""Parallel GeoJSONSeq / shapefile parse + PostgreSQL COPY into staging tables."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

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
    staging_row_count_sync,
    staging_table_name,
)

# Returned as err when load succeeded and promote was queued (not an error).
FINALIZE_QUEUED = "finalize_queued"
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
    return max(1000, int(getattr(get_settings(), "bulk_copy_batch_rows", 20000) or 20000))


def _parse_batch_lines() -> int:
    """Lines per parse task shipped to a worker process (bounds per-task RAM)."""
    return max(500, int(getattr(get_settings(), "bulk_copy_parse_batch_lines", 8000) or 8000))


def _max_inflight_batches(workers: int) -> int:
    """Cap parse tasks in flight so the whole file is never resident in RAM at once."""
    configured = int(getattr(get_settings(), "bulk_copy_max_inflight_batches", 0) or 0)
    if configured > 0:
        return configured
    return max(2, workers * 2)


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


def _dbapi_connection(conn) -> object:
    sa_conn = conn.connection
    if hasattr(sa_conn, "dbapi_connection") and sa_conn.dbapi_connection is not None:
        return sa_conn.dbapi_connection
    if hasattr(sa_conn, "connection"):
        return sa_conn.connection
    return sa_conn


def _encode_copy_row(row: tuple) -> str:
    parts = []
    for val in row:
        if val is None:
            parts.append("\\N")
        else:
            s = str(val)
            s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
            parts.append(s)
    return "\t".join(parts) + "\n"


class _RowCopyReader(io.TextIOBase):
    """
    Readable text stream that encodes row tuples to COPY text on demand.

    psycopg2 pulls ~8 KiB per read(), so the full batch is never materialized as
    one big string — only a small carry buffer plus the source row iterator.
    """

    def __init__(self, rows: Iterable[tuple]) -> None:
        self._it: Iterator[tuple] = iter(rows)
        self._carry = ""

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        if size is None or size < 0:
            chunks = [self._carry]
            self._carry = ""
            chunks.extend(_encode_copy_row(r) for r in self._it)
            return "".join(chunks)
        parts: list[str] = []
        have = len(self._carry)
        if self._carry:
            parts.append(self._carry)
            self._carry = ""
        while have < size:
            try:
                row = next(self._it)
            except StopIteration:
                break
            enc = _encode_copy_row(row)
            parts.append(enc)
            have += len(enc)
        buf = "".join(parts)
        if len(buf) > size:
            self._carry = buf[size:]
            return buf[:size]
        return buf

    # copy_expert also probes readline() on some paths.
    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        if self._carry:
            nl = self._carry.find("\n")
            if nl >= 0:
                line = self._carry[: nl + 1]
                self._carry = self._carry[nl + 1 :]
                return line
        try:
            row = next(self._it)
        except StopIteration:
            line = self._carry
            self._carry = ""
            return line
        line = self._carry + _encode_copy_row(row)
        self._carry = ""
        return line


_COPY_SQL_TEMPLATE = (
    'COPY "{staging}" (\n'
    "    id, collection_id, part_index, geometry, properties,\n"
    "    bulk_import_job_id, created_at, updated_at\n"
    ")\n"
    "FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
)


def _copy_rows_to_staging(conn, staging: str, rows: Iterable[tuple]) -> None:
    raw_conn = _dbapi_connection(conn)
    cur = raw_conn.cursor()
    try:
        cur.copy_expert(_COPY_SQL_TEMPLATE.format(staging=staging), _RowCopyReader(rows))
    finally:
        cur.close()


class _StagingCopier:
    """
    Persistent-connection COPY sink that flushes in bounded row batches.

    Keeps at most `batch_size` rows buffered, streams each flush straight to
    PostgreSQL, and commits per batch so memory (and the UNLOGGED WAL) stay flat
    regardless of total file size.
    """

    def __init__(self, engine: Engine, staging: str, batch_size: int) -> None:
        self._engine = engine
        self._staging = staging
        self._batch_size = max(1000, batch_size)
        self._buf: list[tuple] = []
        self._raw = engine.raw_connection()

    def add(self, rows: list[tuple]) -> int:
        """Buffer rows; flush full batches. Returns rows written to the DB this call."""
        if rows:
            self._buf.extend(rows)
        written = 0
        while len(self._buf) >= self._batch_size:
            batch = self._buf[: self._batch_size]
            del self._buf[: self._batch_size]
            written += self._flush_batch(batch)
        return written

    def flush_final(self) -> int:
        if not self._buf:
            return 0
        batch = self._buf
        self._buf = []
        return self._flush_batch(batch)

    def _flush_batch(self, batch: list[tuple]) -> int:
        cur = self._raw.cursor()
        try:
            cur.copy_expert(
                _COPY_SQL_TEMPLATE.format(staging=self._staging),
                _RowCopyReader(batch),
            )
            self._raw.commit()
        except Exception:
            try:
                self._raw.rollback()
            except Exception:
                pass
            raise
        finally:
            cur.close()
        return len(batch)

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass


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
    now_iso = datetime.now(timezone.utc).isoformat()
    workers = _parser_worker_count()
    parse_lines = _parse_batch_lines()
    created = 0
    failed = 0
    last_hb = time.monotonic()

    copier = _StagingCopier(engine, staging, batch_size)

    def _account(rows: list[tuple], fail: int) -> None:
        nonlocal created, failed, last_hb
        failed += fail
        if rows:
            created += len(rows)
            copier.add(rows)
        if on_progress and heartbeat > 0 and (time.monotonic() - last_hb) >= heartbeat:
            on_progress("running", created, None)
            last_hb = time.monotonic()

    try:
        if workers <= 1:
            with open(path, "rb") as f:
                for i, raw_line in enumerate(f):
                    if i % 2000 == 0:
                        _raise_if_cancelled(job_id)
                    rows, fail = _parse_line_bytes(
                        raw_line,
                        collection_id=collection_id,
                        job_id=job_id,
                        now_iso=now_iso,
                        max_vertices=max_vertices,
                    )
                    _account(rows, fail)
        else:
            max_inflight = _max_inflight_batches(workers)
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                futures: deque = deque()

                def _drain_one() -> None:
                    fut = futures.popleft()
                    rows, fail = fut.result()
                    _raise_if_cancelled(job_id)
                    _account(rows, fail)
                    del rows

                batch: list[bytes] = []
                with open(path, "rb") as f:
                    for raw_line in f:
                        if not raw_line.strip():
                            continue
                        batch.append(raw_line)
                        if len(batch) >= parse_lines:
                            futures.append(
                                pool.submit(
                                    _parse_lines_batch,
                                    batch,
                                    collection_id,
                                    job_id,
                                    now_iso,
                                    max_vertices,
                                )
                            )
                            batch = []
                            while len(futures) >= max_inflight:
                                _drain_one()
                if batch:
                    futures.append(
                        pool.submit(
                            _parse_lines_batch,
                            batch,
                            collection_id,
                            job_id,
                            now_iso,
                            max_vertices,
                        )
                    )
                    batch = []
                while futures:
                    _drain_one()

        copier.flush_final()
    finally:
        copier.close()

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
    last_hb = time.monotonic()
    cancel_check_interval = 2000

    copier = _StagingCopier(engine, staging, batch_size)
    try:
        with fiona.open(open_path, driver=driver) as src:
            for i, feat in enumerate(src):
                if i % cancel_check_interval == 0:
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
                if rows:
                    created += len(rows)
                    copier.add(rows)
                if on_progress and heartbeat > 0 and (time.monotonic() - last_hb) >= heartbeat:
                    on_progress("running", created, None)
                    last_hb = time.monotonic()
        copier.flush_final()
    finally:
        copier.close()
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

        settings = get_settings()
        if (
            settings.bulk_queue_type == "redis"
            and bool(getattr(settings, "bulk_finalize_queue_enabled", True))
        ):
            return created, failed, FINALIZE_QUEUED

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
                staged = staging_row_count_sync(engine, job_id)
                if staged > 0 and mode == "replace":
                    print(
                        f"[bulk-copy] promote failed but keeping staging ({staged} rows) "
                        f"job_id={job_id}: {e}",
                        flush=True,
                    )
                else:
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
