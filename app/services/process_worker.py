"""OGC API - Processes worker: intersection and erase between two collections. Streams from DB, parallel batch workers, low RAM."""
from __future__ import annotations

import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

# Chunk size when streaming intersection results from temp file into DB (minimize features in memory).
_INTERSECTION_STREAM_INSERT_CHUNK = 200
# Delimiter for (id_a, id_b) pair lines in temp file (for sort -u).
_PAIR_SEP = "\t"

from shapely import wkb
from shapely.ops import unary_union
from shapely.strtree import STRtree
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.services.process_queue import ProcessJobPayload
from app.services.job_store import update_job

_MAX_RESULT_COLLECTION_ID_LEN = 60


def _stream_batches_by_size(
    result_iter: Iterator,
    max_bytes: int,
    max_rows: int,
) -> Iterator[list[tuple[str, bytes | None, dict]]]:
    """Yield batches of (id, geom_wkb, props) until total geometry size >= max_bytes or row count >= max_rows (if > 0)."""
    batch_a: list[tuple[str, bytes | None, dict]] = []
    batch_bytes = 0
    for row in result_iter:
        geom_bytes = _geom_to_wkb_bytes(row.geometry)
        row_bytes = len(geom_bytes) if geom_bytes else 0
        # Flush before adding if this row would exceed limit (keeps batch under cap)
        if batch_a and (
            batch_bytes + row_bytes > max_bytes
            or (max_rows > 0 and len(batch_a) >= max_rows)
        ):
            yield batch_a
            batch_a = []
            batch_bytes = 0
        batch_a.append((row.id, geom_bytes, dict(row.properties) if row.properties else {}))
        batch_bytes += row_bytes
    if batch_a:
        yield batch_a


def _safe_result_collection_id(process_id: str, id_a: str, id_b: str) -> str:
    """Build a valid result collection id: operation_collection_id_a_collection_id_b (sanitized, max length)."""
    def sanitize(s: str, max_len: int = 18) -> str:
        s = re.sub(r"[^a-zA-Z0-9_]", "_", s).strip("_") or "x"
        return s[:max_len]
    a = sanitize(id_a)
    b = sanitize(id_b)
    raw = f"{process_id}_{a}_{b}"
    if len(raw) <= _MAX_RESULT_COLLECTION_ID_LEN:
        return raw
    return raw[:_MAX_RESULT_COLLECTION_ID_LEN]


def _geom_to_shapely(geom: Any):
    """Convert DB geometry (hex WKB str or bytes) to Shapely geometry."""
    if geom is None:
        return None
    if isinstance(geom, str):
        return wkb.loads(geom, hex=True)
    if isinstance(geom, (bytes, memoryview, bytearray)):
        return wkb.loads(bytes(geom))
    try:
        from geoalchemy2.shape import to_shape
        return to_shape(geom)
    except Exception:
        return wkb.loads(bytes(geom))


def _geom_to_wkb_bytes(geom: Any) -> bytes | None:
    """Return geometry as WKB bytes for passing across threads (no session ref)."""
    if geom is None:
        return None
    if isinstance(geom, bytes):
        return geom
    if isinstance(geom, (memoryview, bytearray)):
        return bytes(geom)
    if isinstance(geom, str):
        return wkb.loads(geom, hex=True).wkb
    try:
        from geoalchemy2.shape import to_shape
        shp = to_shape(geom)
        return shp.wkb if shp is not None else None
    except Exception:
        return None


def _ensure_collection_and_partition_sync(engine: Engine, result_id: str, title: str) -> None:
    """Create collection row and ensure features partition exists (sync). Idempotent."""
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO collections (id, title, description, extent, created_at, updated_at)
                VALUES (:id, :title, NULL, NULL, :now, :now)
                ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description, updated_at = EXCLUDED.updated_at
            """),
            {"id": result_id, "title": title, "now": datetime.now(timezone.utc)},
        )
        conn.commit()

    # Partition: reuse naming from features_partitions
    from app.db.features_partitions import _safe_partition_name
    part_name = _safe_partition_name(result_id)
    bound_target = "FOR VALUES IN ('" + result_id.replace("'", "''") + "')"
    with engine.connect() as conn:
        r = conn.execute(
            text("""
                SELECT pg_get_expr(c.relpartbound, c.oid) AS bound
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'features' AND c.relname != 'features_default'
            """)
        )
        exists = any((row.bound or "").replace(" ", "") == bound_target.replace(" ", "") for row in r)
        if not exists:
            conn.execute(text(f'CREATE TABLE "{part_name}" PARTITION OF features FOR VALUES IN (:cid)'), {"cid": result_id})
        conn.commit()


def _clear_result_collection_sync(session: Session, result_id: str) -> None:
    """Clear all features in the result collection (so we can refill). Uses TRUNCATE on the partition when possible (fast);
    falls back to DELETE if the partition is not found (e.g. default partition)."""
    from app.db.features_partitions import _safe_partition_name

    part_name = _safe_partition_name(result_id)
    try:
        # TRUNCATE partition directly: instant even with millions of rows (no row-by-row delete).
        session.execute(text(f'TRUNCATE TABLE "{part_name}"'))
        session.commit()
    except Exception:
        session.rollback()
        # Fallback: DELETE (slow on large tables but works for default partition or if name differs).
        session.execute(text("DELETE FROM features WHERE collection_id = :cid"), {"cid": result_id})
        session.commit()


def _cleanup_result_collection_sync(engine: Engine, result_id: str) -> None:
    """
    Fully remove a result collection and all its data (features, tiles record, styles).
    Use after worker restart for jobs that were 'running' so partial state is removed.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT pmtiles_path FROM collection_tiles WHERE collection_id = :cid"),
            {"cid": result_id},
        ).first()
        if row and row.pmtiles_path and os.path.isfile(row.pmtiles_path):
            try:
                os.unlink(row.pmtiles_path)
            except OSError:
                pass
        conn.execute(text("DELETE FROM features WHERE collection_id = :cid"), {"cid": result_id})
        conn.execute(text("DELETE FROM collection_tiles WHERE collection_id = :cid"), {"cid": result_id})
        conn.execute(text("DELETE FROM styles WHERE collection_id = :cid"), {"cid": result_id})
        conn.execute(text("DELETE FROM collections WHERE id = :cid"), {"cid": result_id})
        conn.commit()


def _insert_features_sync(session: Session, result_id: str, features: list[tuple[Any, dict]], batch_size: int | None = None) -> None:
    """Insert (shapely_geom, properties) into features for result_id. Geometries subdivided with ST_Subdivide (≤256 vertices per row)."""
    from uuid6 import uuid7

    from app.utils.feature_subdivide import insert_feature_subdivided_sql

    if batch_size is None:
        batch_size = max(1, get_settings().process_insert_batch_size)
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    for i in range(0, len(features), batch_size):
        batch = features[i : i + batch_size]
        for geom_shapely, props in batch:
            if geom_shapely is None or geom_shapely.is_empty:
                continue
            fid = str(uuid7())
            wkt = geom_shapely.wkt
            sql, params = insert_feature_subdivided_sql(fid, result_id, wkt, props, now, max_vertices)
            session.execute(text(sql), params)
        session.commit()


def _write_intersection_batch_to_file(
    file_handle: Any,
    chunk_results: list[tuple[Any, dict]],
) -> int:
    """Write one batch of (geom, props) to a JSONL file. One line per feature. Returns count written."""
    count = 0
    for geom_shapely, props in chunk_results:
        if geom_shapely is None or geom_shapely.is_empty:
            continue
        line = json.dumps({"wkt": geom_shapely.wkt, "props": props}) + "\n"
        file_handle.write(line)
        count += 1
    file_handle.flush()
    return count


def _insert_features_from_wkt_sync(
    session: Session,
    result_id: str,
    rows: list[tuple[str, dict]],
    batch_size: int | None = None,
) -> int:
    """Insert (wkt_str, properties) into features for result_id. Subdivides geometry. Returns number inserted."""
    from uuid6 import uuid7

    from app.utils.feature_subdivide import insert_feature_subdivided_sql

    if batch_size is None:
        batch_size = max(1, get_settings().process_insert_batch_size)
    max_vertices = get_settings().features_subdivide_max_vertices
    now = datetime.now(timezone.utc)
    inserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        for wkt, props in batch:
            if not wkt or not str(wkt).strip():
                continue
            fid = str(uuid7())
            sql, params = insert_feature_subdivided_sql(fid, result_id, wkt, props, now, max_vertices)
            session.execute(text(sql), params)
            inserted += 1
        session.commit()
    return inserted


def _stream_intersection_pairs_to_file(
    session: Session,
    collection_id_a: str,
    collection_id_b: str,
    file_handle: Any,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Stream unique (id_a, id_b) pairs from DB where A geometry intersects B. Writes one line per pair.
    Uses DISTINCT so we get logical pairs only (no part-part duplicates); no sort needed.
    on_progress(count) called every ~50k rows so caller can update job status."""
    result = session.execute(
        text("""
            SELECT DISTINCT a.id AS id_a, b.id AS id_b
            FROM features a
            INNER JOIN features b
              ON b.collection_id = :cid_b AND b.geometry IS NOT NULL
              AND ST_Intersects(a.geometry, b.geometry)
            WHERE a.collection_id = :cid_a AND a.geometry IS NOT NULL
        """),
        {"cid_a": collection_id_a, "cid_b": collection_id_b},
        execution_options={"stream_results": True},
    )
    count = 0
    progress_interval = 50_000
    for row in result:
        id_a = getattr(row, "id_a", None) or row[0]
        id_b = getattr(row, "id_b", None) or row[1]
        if id_a and id_b:
            file_handle.write(f"{id_a}{_PAIR_SEP}{id_b}\n")
            count += 1
            if on_progress and count % progress_interval == 0:
                on_progress(count)
    file_handle.flush()
    return count


def _sort_unique_pairs_file(path: str) -> None:
    """Overwrite file with sorted unique lines (id_a\\tid_b). Reduces memory vs in-Python set."""
    import subprocess
    try:
        subprocess.run(
            ["sort", "-u", "-t", _PAIR_SEP, "-k", "1,2", "-o", path, path],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback: read lines, dedupe in memory (set of pairs), write back (may use more memory)
        with open(path, "r", encoding="utf-8") as f:
            seen: set[tuple[str, str]] = set()
            lines: list[str] = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(_PAIR_SEP, 1)
                if len(parts) != 2:
                    continue
                key = (parts[0], parts[1])
                if key not in seen:
                    seen.add(key)
                    lines.append(line + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(sorted(lines))


def _fetch_logical_features_by_ids(
    session: Session,
    collection_id: str,
    feature_ids: list[str],
) -> dict[str, tuple[Any, dict]]:
    """Fetch all parts for given feature ids in collection; return dict id -> (union_geom, props)."""
    if not feature_ids:
        return {}
    rows = session.execute(
        text("""
            SELECT id, part_index, geometry, properties
            FROM features
            WHERE collection_id = :cid AND id = ANY(:ids) AND geometry IS NOT NULL
            ORDER BY id, part_index
        """),
        {"cid": collection_id, "ids": feature_ids},
    ).fetchall()
    by_id: dict[str, list[tuple[Any, dict]]] = {}
    for r in rows:
        fid = r.id
        shp = _geom_to_shapely(r.geometry)
        if shp is None or shp.is_empty:
            continue
        props = dict(r.properties) if r.properties else {}
        if fid not in by_id:
            by_id[fid] = []
        by_id[fid].append((shp, props))
    out: dict[str, tuple[Any, dict]] = {}
    for fid, parts in by_id.items():
        if not parts:
            continue
        geoms = [p[0] for p in parts]
        props = parts[0][1]
        try:
            union_geom = unary_union(geoms)
            if union_geom is None or union_geom.is_empty:
                continue
            out[fid] = (union_geom, props)
        except Exception:
            continue
    return out


def _process_intersection_pairs_chunk(
    engine: Engine,
    collection_id_a: str,
    collection_id_b: str,
    pairs: list[tuple[str, str]],
) -> list[tuple[Any, dict]]:
    """Fetch only features for the given (id_a, id_b) pairs, intersect each pair, return (geom, props) list.
    Each worker touches only a bounded set of features (no 'A vs millions of B')."""
    if not pairs:
        return []
    ids_a = list({p[0] for p in pairs})
    ids_b = list({p[1] for p in pairs})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    try:
        features_a = _fetch_logical_features_by_ids(session, collection_id_a, ids_a)
        features_b = _fetch_logical_features_by_ids(session, collection_id_b, ids_b)
    finally:
        session.close()
    out = []
    for id_a, id_b in pairs:
        fa = features_a.get(id_a)
        fb = features_b.get(id_b)
        if not fa or not fb:
            continue
        geom_a, props_a = fa
        geom_b, props_b = fb
        try:
            if not geom_a.intersects(geom_b):
                continue
            inter = geom_a.intersection(geom_b)
            if inter.is_empty or not inter.is_valid:
                continue
            props = {**props_a, **props_b, "_id_a": id_a, "_id_b": id_b}
            out.append((inter, props))
        except Exception:
            continue
    return out


def _aggregate_intersection_result_sync(engine: Engine, result_id: str) -> int:
    """Final aggregation: GROUP BY (id_a, id_b), ST_Union(geometry), then replace result collection.
    Uses a session-scoped temp table so we never hold large geometry in Python. Small footprint."""
    from uuid6 import uuid7

    from app.utils.feature_subdivide import insert_feature_subdivided_sql

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    try:
        session.execute(
            text("""
                CREATE TEMP TABLE process_intersection_agg (
                    id_a text,
                    id_b text,
                    geom geometry(Geometry, 4326),
                    props jsonb
                )
            """)
        )
        session.execute(
            text("""
                INSERT INTO process_intersection_agg (id_a, id_b, geom, props)
                SELECT
                    f.properties->>'_id_a',
                    f.properties->>'_id_b',
                    ST_Union(f.geometry),
                    (array_agg(f.properties ORDER BY f.id))[1]
                FROM features f
                WHERE f.collection_id = :cid
                GROUP BY f.properties->>'_id_a', f.properties->>'_id_b'
            """),
            {"cid": result_id},
        )
        # Clear result partition before re-inserting aggregated rows (TRUNCATE = fast)
        _clear_result_collection_sync(session, result_id)

        max_vertices = get_settings().features_subdivide_max_vertices
        now = datetime.now(timezone.utc)
        insert_batch = max(1, get_settings().process_insert_batch_size)
        inserted = 0
        # Stream aggregated rows in chunks (fetchmany) to avoid loading all into memory
        fetch_chunk = max(100, insert_batch * 2)
        result_cursor = session.execute(
            text("SELECT id_a, id_b, ST_AsText(geom) AS wkt, props FROM process_intersection_agg")
        )
        while True:
            rows = result_cursor.fetchmany(fetch_chunk)
            if not rows:
                break
            for row in rows:
                wkt = getattr(row, "wkt", None)
                props = getattr(row, "props", None)
                if not wkt or not str(wkt).strip():
                    continue
                props_dict = dict(props) if props else {}
                fid = str(uuid7())
                sql, params = insert_feature_subdivided_sql(
                    fid, result_id, str(wkt), props_dict, now, max_vertices
                )
                session.execute(text(sql), params)
                inserted += 1
            session.commit()
        result_cursor.close()
        session.execute(text("DROP TABLE process_intersection_agg"))
        session.commit()
        return inserted
    finally:
        session.close()


def _run_intersection_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
    batch_max_bytes: int,
    batch_max_rows: int,
) -> int:
    """Stream A (only rows that intersect B, filtered in DB). Batches by geometry size (~200 MiB); parallel workers; low RAM."""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    insert_session = SessionLocal()
    total_inserted = 0
    try:
        result = session.execute(
            text("""
                SELECT a.id, a.geometry, a.properties
                FROM features a
                WHERE a.collection_id = :cid_a AND a.geometry IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM features b
                    WHERE b.collection_id = :cid_b AND ST_Intersects(a.geometry, b.geometry)
                )
            """),
            {"cid_a": collection_id_a, "cid_b": collection_id_b},
            execution_options={"stream_results": True},
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pending: set = set()
            max_pending = max(max_workers * 2, 4)
            for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows):
                if not batch_a:
                    continue
                while len(pending) >= max_pending:
                    done = [f for f in pending if f.done()]
                    for f in done:
                        pending.discard(f)
                        chunk_results = f.result()
                        if chunk_results:
                            _insert_features_sync(insert_session, result_id, chunk_results)
                            total_inserted += len(chunk_results)
                fut = executor.submit(_process_intersection_batch, batch_a, collection_id_b, engine)
                pending.add(fut)
            for fut in as_completed(pending):
                chunk_results = fut.result()
                if chunk_results:
                    _insert_features_sync(insert_session, result_id, chunk_results)
                    total_inserted += len(chunk_results)
    finally:
        session.close()
        insert_session.close()
    return total_inserted


def _process_erase_batch(
    batch_a: list[tuple[str, bytes | None, dict]],
    collection_id_b: str,
    engine: Engine,
) -> list[tuple[Any, dict]]:
    """Process one batch of A: load B intersecting bbox of batch, compute a - union(B). Returns list of (shapely_geom, props)."""
    list_a = []
    for fid, geom_wkb, props in batch_a:
        if geom_wkb is None:
            continue
        shp = wkb.loads(geom_wkb)
        if shp is None or shp.is_empty:
            continue
        list_a.append((fid, shp, props))
    if not list_a:
        return []
    minx = min(s.bounds[0] for _, s, _ in list_a)
    miny = min(s.bounds[1] for _, s, _ in list_a)
    maxx = max(s.bounds[2] for _, s, _ in list_a)
    maxy = max(s.bounds[3] for _, s, _ in list_a)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    try:
        rows_b = session.execute(
            text("""
                SELECT id, geometry FROM features b
                WHERE b.collection_id = :cid AND b.geometry IS NOT NULL
                AND ST_Intersects(b.geometry, ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326))
            """),
            {"cid": collection_id_b, "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
        ).fetchall()
    finally:
        session.close()
    list_b = []
    for r in rows_b:
        shp_b = _geom_to_shapely(r.geometry)
        if shp_b is None or shp_b.is_empty:
            continue
        list_b.append(shp_b)
    if not list_b:
        tree_b = None
    else:
        tree_b = STRtree(list_b)
    out = []
    for fid_a, shp_a, props_a in list_a:
        if tree_b is None:
            diff = shp_a
        else:
            indices = tree_b.query(shp_a)
            if not indices:
                diff = shp_a
            else:
                geoms_b = [list_b[i] for i in indices]
                try:
                    union_b = unary_union(geoms_b)
                    diff = shp_a.difference(union_b)
                except Exception:
                    continue
                if diff.is_empty:
                    continue
        if not diff.is_valid:
            try:
                diff = diff.buffer(0)
            except Exception:
                continue
        props = {**props_a, "_id_a": fid_a}
        out.append((diff, props))
    return out


def _run_erase_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
    batch_max_bytes: int,
    batch_max_rows: int,
) -> int:
    """Stream A in batches by geometry size; each worker loads B for bbox and computes erase. Low RAM."""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    insert_session = SessionLocal()
    total_inserted = 0
    try:
        result = session.execute(
            text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
            {"cid": collection_id_a},
            execution_options={"stream_results": True},
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pending: set = set()
            max_pending = max(max_workers * 2, 4)
            for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows):
                if not batch_a:
                    continue
                while len(pending) >= max_pending:
                    done = [f for f in pending if f.done()]
                    for f in done:
                        pending.discard(f)
                        chunk_results = f.result()
                        if chunk_results:
                            _insert_features_sync(insert_session, result_id, chunk_results)
                            total_inserted += len(chunk_results)
                fut = executor.submit(_process_erase_batch, batch_a, collection_id_b, engine)
                pending.add(fut)
            for fut in as_completed(pending):
                chunk_results = fut.result()
                if chunk_results:
                    _insert_features_sync(insert_session, result_id, chunk_results)
                    total_inserted += len(chunk_results)
    finally:
        session.close()
        insert_session.close()
    return total_inserted


def process_process_job_sync(payload: ProcessJobPayload) -> tuple[str | None, int, int]:
    """
    Run intersection or erase process. Streams from DB, parallel batch workers, low RAM.
    Returns (None, count) on success, (error_message, 0) on failure.
    """
    import os
    import time
    from sqlalchemy import create_engine

    settings = get_settings()
    cpu_count = getattr(os, "cpu_count", lambda: 4)() or 4
    max_workers = max(1, settings.process_batch_workers or cpu_count)
    batch_max_bytes = max(1024, settings.process_batch_max_bytes)
    batch_max_rows = max(0, settings.process_batch_max_rows)
    # Pool must cover: 1 stream session + 1 insert session + max_workers (each does B query)
    pool_size = max(5, max_workers + 3)
    engine = create_engine(
        settings.database_sync_url,
        pool_pre_ping=True,
        future=True,
        pool_size=pool_size,
        max_overflow=min(4, max_workers),
    )

    result_id = _safe_result_collection_id(
        payload.process_id, payload.collection_id_a, payload.collection_id_b
    )
    title = f"{payload.process_id.title()} of {payload.collection_id_a} and {payload.collection_id_b}"

    try:
        _ensure_collection_and_partition_sync(engine, result_id, title)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        session = SessionLocal()
        _clear_result_collection_sync(session, result_id)
        session.close()

        items_in = 0
        items_out = 0
        last_update = 0.0
        interval = float(getattr(settings, "process_progress_update_seconds", 2.0) or 2.0)

        def maybe_update(status_text: str) -> None:
            nonlocal last_update
            now = time.monotonic()
            if last_update and (now - last_update) < interval:
                return
            last_update = now
            update_job(
                payload.job_id,
                status="running",
                message=f"{status_text} Input: {items_in} • Output: {items_out}",
                items_in=items_in,
                items_created=items_out,
            )

        if payload.process_id == "intersection":
            # Pair-based flow: (1) query list of (id_a, id_b) that need processing, (2) stream to workers that fetch only those two features and intersect.
            # Avoids one feature from A intersecting with millions of B in a single worker.
            pair_chunk_size = max(1, getattr(settings, "process_intersection_pair_chunk_size", 400))
            stream_chunk = max(1, min(getattr(settings, "process_insert_batch_size", 200), _INTERSECTION_STREAM_INSERT_CHUNK))

            fd_pairs, pairs_path = tempfile.mkstemp(suffix=".pairs.txt", prefix="geofast_")
            fd_result, result_path = tempfile.mkstemp(suffix=".intersection.jsonl", prefix="geofast_")
            try:
                # Phase 1: stream unique (id_a, id_b) pairs from DB to file (DISTINCT = no sort needed)
                session = SessionLocal()
                try:
                    with os.fdopen(fd_pairs, "w", encoding="utf-8") as pairs_file:

                        def on_pairs_progress(n: int) -> None:
                            nonlocal items_in
                            items_in = n
                            maybe_update("Finding pairs…")

                        items_in = _stream_intersection_pairs_to_file(
                            session,
                            payload.collection_id_a,
                            payload.collection_id_b,
                            pairs_file,
                            on_progress=on_pairs_progress,
                        )
                finally:
                    session.close()
                maybe_update("Pairs found, computing…")

                # Phase 2: read pairs in chunks; for each chunk fetch only id_a and id_b features, intersect, write to result file
                with os.fdopen(fd_result, "w", encoding="utf-8") as result_file:
                    pair_chunk: list[tuple[str, str]] = []
                    with open(pairs_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.split(_PAIR_SEP, 1)
                            if len(parts) != 2:
                                continue
                            pair_chunk.append((parts[0], parts[1]))
                            if len(pair_chunk) >= pair_chunk_size:
                                maybe_update("Computing intersection…")
                                chunk_results = _process_intersection_pairs_chunk(
                                    engine, payload.collection_id_a, payload.collection_id_b, pair_chunk
                                )
                                items_out += _write_intersection_batch_to_file(result_file, chunk_results)
                                pair_chunk = []
                        if pair_chunk:
                            chunk_results = _process_intersection_pairs_chunk(
                                engine, payload.collection_id_a, payload.collection_id_b, pair_chunk
                            )
                            items_out += _write_intersection_batch_to_file(result_file, chunk_results)

                # Phase 3: stream result file to DB in small chunks
                maybe_update("Inserting from file…")
                insert_session = SessionLocal()
                try:
                    chunk_rows: list[tuple[str, dict]] = []
                    with open(result_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                wkt = rec.get("wkt") or ""
                                props = rec.get("props")
                                if isinstance(props, dict):
                                    chunk_rows.append((wkt, props))
                                else:
                                    chunk_rows.append((wkt, {}))
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if len(chunk_rows) >= stream_chunk:
                                _insert_features_from_wkt_sync(insert_session, result_id, chunk_rows, batch_size=stream_chunk)
                                chunk_rows = []
                        if chunk_rows:
                            _insert_features_from_wkt_sync(insert_session, result_id, chunk_rows, batch_size=stream_chunk)
                finally:
                    insert_session.close()
            finally:
                for p in (pairs_path, result_path):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

            maybe_update("Aggregating by A/B id…")
            count = _aggregate_intersection_result_sync(engine, result_id)

        elif payload.process_id == "erase":
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                session = SessionLocal()
                try:
                    result = session.execute(
                        text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
                        {"cid": payload.collection_id_a},
                        execution_options={"stream_results": True},
                    )
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        pending: set = set()
                        max_pending = max(max_workers * 2, 4)
                        for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows):
                            if not batch_a:
                                continue
                            items_in += len(batch_a)
                            maybe_update("Computing erase…")
                            while len(pending) >= max_pending:
                                done = [f for f in pending if f.done()]
                                for f in done:
                                    pending.discard(f)
                                    chunk_results = f.result()
                                    if chunk_results:
                                        _insert_features_sync(insert_session, result_id, chunk_results)
                                        items_out += len(chunk_results)
                                        maybe_update("Writing results…")
                            fut = executor.submit(_process_erase_batch, batch_a, payload.collection_id_b, engine)
                            pending.add(fut)
                        for fut in as_completed(pending):
                            chunk_results = fut.result()
                            if chunk_results:
                                _insert_features_sync(insert_session, result_id, chunk_results)
                                items_out += len(chunk_results)
                                maybe_update("Writing results…")
                finally:
                    session.close()
            finally:
                insert_session.close()
            count = items_out
        else:
            engine.dispose()
            return (f"Unknown process: {payload.process_id}", 0, 0)

        engine.dispose()
        return (None, count, items_in)
    except Exception as e:
        engine.dispose()
        return (str(e), 0, 0)
