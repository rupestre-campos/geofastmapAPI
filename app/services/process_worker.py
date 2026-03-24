"""OGC API - Processes worker: intersection and erase between two collections or single feature vs layers."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

# Chunk size when streaming intersection results into DB (minimize features in memory).
_INTERSECTION_STREAM_INSERT_CHUNK = 200

from shapely import wkb
from shapely.ops import unary_union
from shapely.strtree import STRtree
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.services.process_queue import ProcessJobPayload
from app.services.job_store import get_job, update_job


class ProcessCancelled(Exception):
    """Raised when the job is marked cancelled in job_store (cooperative stop)."""


def _raise_if_cancelled(job_id: str) -> None:
    if not job_id:
        return
    j = get_job(job_id)
    if j is not None and j.status == "cancelled":
        raise ProcessCancelled()


def _cleanup_after_process_cancel(engine: Engine, payload: ProcessJobPayload, result_id: str) -> None:
    """Remove partial result data after a cancelled process job."""
    if not result_id:
        return
    # In-place measure updates: do not delete the target collection
    if payload.process_id == "measure":
        return
    try:
        if payload.update_existing:
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            session = SessionLocal()
            try:
                _clear_result_collection_sync(session, result_id)
            finally:
                session.close()
            try:
                _update_feature_count_sync(engine, result_id)
                collections_crud.recompute_and_update_collection_extent_sync(engine, result_id)
            except Exception:
                pass
        else:
            _cleanup_result_collection_sync(engine, result_id)
    except Exception:
        pass
    try:
        cleanup_process_worker_temp_dir()
    except Exception:
        pass

_MAX_RESULT_COLLECTION_ID_LEN = 60
_RESULT_HASH_LEN = 12


def _hash_for_result(s: str) -> str:
    """Deterministic short hash for result collection naming (first 12 hex chars of SHA-256)."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:_RESULT_HASH_LEN]


def _sanitize_result_collection_id(raw: str) -> str:
    """Sanitize user-provided collection id for use as result: alphanumeric, underscore, hyphen; max length."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", raw).strip("_") or "result"
    return s[:_MAX_RESULT_COLLECTION_ID_LEN]


def _default_result_collection_id(process_id: str, collection_id_a: str, collection_id_b: str) -> str:
    """Deterministic result collection id for collection-vs-collection: process_id + hash(id_a|id_b) (sorted)."""
    a, b = sorted([collection_id_a, collection_id_b])
    h = _hash_for_result(f"{a}|{b}")
    return f"{process_id}_{h}"[:_MAX_RESULT_COLLECTION_ID_LEN]


def _default_result_collection_id_feature(process_id: str, feature_id: str, collection_ids: list[str]) -> str:
    """Deterministic result collection id for feature-vs-layers: process_id + hash(feature_id|sorted(layers))."""
    layers = "|".join(sorted(collection_ids))
    h = _hash_for_result(f"{feature_id}|{layers}")
    return f"{process_id}_{h}"[:_MAX_RESULT_COLLECTION_ID_LEN]


def cleanup_process_worker_temp_dir() -> None:
    """Remove contents of the process worker temp directory at startup (leftovers from past runs or crashes)."""
    path = (get_settings().process_temp_path or "").strip()
    if not path:
        return
    # Restrict to under /tmp or /var/tmp to avoid accidental deletion of project dirs
    abs_path = os.path.abspath(path)
    if not (abs_path.startswith("/tmp") or abs_path.startswith("/var/tmp")):
        return
    try:
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path, ignore_errors=True)
        os.makedirs(abs_path, exist_ok=True)
    except OSError:
        pass


def _stream_batches_by_size(
    result_iter: Iterator,
    max_bytes: int,
    max_rows: int,
    job_id: str = "",
) -> Iterator[list[tuple[str, bytes | None, dict]]]:
    """Yield batches of (id, geom_wkb, props) until total geometry size >= max_bytes or row count >= max_rows (if > 0).
    If job_id is set, checks cooperative cancellation every ~80 rows while streaming (so cancel works during long SELECTs)."""
    batch_a: list[tuple[str, bytes | None, dict]] = []
    batch_bytes = 0
    row_num = 0
    for row in result_iter:
        row_num += 1
        if job_id and row_num % 80 == 0:
            _raise_if_cancelled(job_id)
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


def _ensure_collection_and_partition_sync(
    engine: Engine,
    result_id: str,
    title: str,
    description: str | None = None,
) -> None:
    """Create collection row and ensure features partition exists (sync). Idempotent."""
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO collections (id, title, description, extent, created_at, updated_at)
                VALUES (:id, :title, :description, NULL, :now, :now)
                ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description, updated_at = EXCLUDED.updated_at
            """),
            {"id": result_id, "title": title, "description": description, "now": datetime.now(timezone.utc)},
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


def _update_feature_count_sync(engine: Engine, collection_id: str) -> None:
    """Recompute and update collections.feature_count for a given collection id."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(DISTINCT id) AS n "
                "FROM features WHERE collection_id = :cid"
            ),
            {"cid": collection_id},
        ).first()
        n = int(row.n) if row and row.n is not None else 0
        conn.execute(
            text(
                "UPDATE collections "
                "SET feature_count = :n "
                "WHERE id = :cid"
            ),
            {"cid": collection_id, "n": n},
        )
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

    from app.utils.feature_subdivide import (
    MAX_COORDS_FOR_DB_SUBDIVIDE,
    _coord_count,
    insert_feature_parts_batched,
    insert_feature_subdivided_sql,
    subdivide_geometry_by_vertices,
)
    from shapely.geometry import GeometryCollection, MultiPoint, MultiLineString, MultiPolygon, Point, LineString, Polygon
    from shapely.ops import unary_union
    from shapely.validation import make_valid

    if batch_size is None:
        batch_size = max(1, get_settings().process_insert_batch_size)
    max_vertices = get_settings().features_subdivide_max_vertices
    def _split_geometry_by_type(geom):
        if geom is None or geom.is_empty:
            return []
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom is None or geom.is_empty:
            return []
        if isinstance(geom, GeometryCollection):
            pts = []
            lines = []
            polys = []
            for g in geom.geoms:
                if g is None or g.is_empty:
                    continue
                if not g.is_valid:
                    g = make_valid(g)
                    if g is None or g.is_empty:
                        continue
                if isinstance(g, (Polygon, MultiPolygon)):
                    polys.append(g)
                elif isinstance(g, (LineString, MultiLineString)):
                    lines.append(g)
                elif isinstance(g, (Point, MultiPoint)):
                    pts.append(g)
            out = []
            if polys:
                out.append(unary_union(polys))
            if lines:
                out.append(unary_union(lines))
            if pts:
                out.append(unary_union(pts))
            return [g for g in out if g and not g.is_empty]
        return [geom]

    now = datetime.now(timezone.utc)
    for i in range(0, len(features), batch_size):
        batch = features[i : i + batch_size]
        for geom_shapely, props in batch:
            if geom_shapely is None or geom_shapely.is_empty:
                continue
            for geom in _split_geometry_by_type(geom_shapely):
                if geom is None or geom.is_empty:
                    continue
                fid = str(uuid7())
                if _coord_count(geom) > MAX_COORDS_FOR_DB_SUBDIVIDE:
                    parts = subdivide_geometry_by_vertices(geom, max_vertices)
                    wkt_list = [p.wkt for p in parts if p is not None and not p.is_empty]
                    if wkt_list:
                        for sql, params in insert_feature_parts_batched(fid, result_id, wkt_list, props, now):
                            session.execute(text(sql), params)
                else:
                    wkt = geom.wkt
                    sql, params = insert_feature_subdivided_sql(fid, result_id, wkt, props, now, max_vertices)
                    session.execute(text(sql), params)
        session.commit()


def _stream_intersection_pairs_chunks(
    session: Session,
    collection_id_a: str,
    collection_id_b: str,
    chunk_size: int,
    on_progress: Callable[[int], None] | None = None,
) -> Iterator[list[tuple[str, str]]]:
    """Stream unique (id_a, id_b) pairs from DB in memory chunks. No temp file.
    Uses DISTINCT; on_progress(total) called every ~50k rows."""
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
    chunk: list[tuple[str, str]] = []
    total = 0
    progress_interval = 50_000
    for row in result:
        id_a = getattr(row, "id_a", None) or row[0]
        id_b = getattr(row, "id_b", None) or row[1]
        if id_a and id_b:
            chunk.append((id_a, id_b))
            total += 1
            if on_progress and total % progress_interval == 0:
                on_progress(total)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
    if chunk:
        yield chunk


def _get_single_feature_geometry_sync(
    engine: Engine,
    collection_id: str,
    feature_id: str,
) -> tuple[Any, dict] | tuple[None, None]:
    """Fetch one logical feature by id; return (shapely_geom, properties) or (None, None)."""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()
    try:
        rows = session.execute(
            text("""
                SELECT id, part_index, geometry, properties
                FROM features
                WHERE collection_id = :cid AND id = :fid AND geometry IS NOT NULL
                ORDER BY part_index
            """),
            {"cid": collection_id, "fid": feature_id},
        ).fetchall()
        if not rows:
            return (None, None)
        geoms = []
        props = dict(rows[0].properties) if rows[0].properties else {}
        for r in rows:
            shp = _geom_to_shapely(r.geometry)
            if shp and not shp.is_empty:
                geoms.append(shp)
        if not geoms:
            return (None, None)
        union_geom = unary_union(geoms)
        if union_geom is None or union_geom.is_empty:
            return (None, None)
        return (union_geom, props)
    finally:
        session.close()


def _get_single_geometry_from_geojson(geojson_dict: dict) -> Any | None:
    """Extract one Shapely geometry from GeoJSON Feature or FeatureCollection. Returns None if invalid."""
    from shapely.geometry import shape
    if not geojson_dict:
        return None
    kind = geojson_dict.get("type")
    if kind == "Feature":
        geom = geojson_dict.get("geometry")
        if not geom:
            return None
        try:
            return shape(geom)
        except Exception:
            return None
    if kind == "FeatureCollection":
        features = geojson_dict.get("features") or []
        geoms = []
        for f in features:
            if f.get("type") == "Feature" and f.get("geometry"):
                try:
                    g = shape(f["geometry"])
                    if g and not g.is_empty:
                        geoms.append(g)
                except Exception:
                    pass
        if not geoms:
            return None
        return unary_union(geoms)
    if kind in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"):
        try:
            return shape(geojson_dict)
        except Exception:
            return None
    return None


def _run_intersection_feature_vs_layers_sync(
    engine: Engine,
    result_id: str,
    geom_a: Any,
    props_a: dict,
    collection_ids: list[str],
    insert_session: Session,
    job_id: str = "",
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    """Single geometry vs multiple layers: for each layer, find intersecting features; intersect; insert. Returns count."""
    from shapely.validation import make_valid
    total = 0
    n_layers = len(collection_ids)
    minx, miny, maxx, maxy = geom_a.bounds
    for i, cid in enumerate(collection_ids):
        _raise_if_cancelled(job_id)
        if progress_callback:
            progress_callback(i + 1, n_layers, cid)
        session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
        try:
            # Bbox pre-filter (uses GIST index) before exact ST_Intersects — reduces load and keeps API responsive.
            rows = session.execute(
                text("""
                    SELECT id, geometry, properties
                    FROM features
                    WHERE collection_id = :cid AND geometry IS NOT NULL
                    AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
                    AND ST_Intersects(geometry, ST_GeomFromText(:wkt, 4326))
                """),
                {"cid": cid, "wkt": geom_a.wkt, "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
            ).fetchall()
            batch = []
            for r in rows:
                shp_b = _geom_to_shapely(r.geometry)
                if shp_b is None or shp_b.is_empty:
                    continue
                try:
                    if not geom_a.intersects(shp_b):
                        continue
                    inter = geom_a.intersection(shp_b)
                    if inter.is_empty:
                        continue
                    if not inter.is_valid:
                        inter = make_valid(inter)
                    if inter is None or inter.is_empty:
                        continue
                    props = {**props_a, **_dict(r.properties), "_layer": cid, "_id_b": r.id}
                    for part in _split_geometry_by_type_for_insert(inter):
                        if part is None or part.is_empty:
                            continue
                        batch.append((part, props))
                except Exception:
                    continue
            if batch:
                _insert_features_sync(insert_session, result_id, batch)
                total += len(batch)
        finally:
            session.close()
    return total


def _run_erase_feature_vs_layers_sync(
    engine: Engine,
    result_id: str,
    geom_a: Any,
    props_a: dict,
    collection_ids: list[str],
    insert_session: Session,
    job_id: str = "",
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    """Single geometry vs multiple layers: geom_result = geom_a minus (union of all features in layers). Insert result. Returns count."""
    from shapely.validation import make_valid
    current = geom_a
    n_layers = len(collection_ids)
    for i, cid in enumerate(collection_ids):
        _raise_if_cancelled(job_id)
        if progress_callback:
            progress_callback(i + 1, n_layers, cid)
        minx, miny, maxx, maxy = current.bounds
        session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
        try:
            # Bbox pre-filter (uses GIST index) before exact ST_Intersects — reduces load and keeps API responsive.
            rows = session.execute(
                text("""
                    SELECT id, geometry
                    FROM features
                    WHERE collection_id = :cid AND geometry IS NOT NULL
                    AND geometry && ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326)
                    AND ST_Intersects(geometry, ST_GeomFromText(:wkt, 4326))
                """),
                {"cid": cid, "wkt": current.wkt, "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
            ).fetchall()
            list_b = []
            for r in rows:
                shp_b = _geom_to_shapely(r.geometry)
                if shp_b and not shp_b.is_empty:
                    list_b.append(shp_b)
            if list_b:
                try:
                    union_b = unary_union(list_b)
                    current = current.difference(union_b)
                    if current is None or current.is_empty:
                        break
                    if not current.is_valid:
                        current = make_valid(current)
                except Exception:
                    pass
        finally:
            session.close()
    if current is None or current.is_empty:
        return 0
    total = 0
    for part in _split_geometry_by_type_for_insert(current):
        if part is None or part.is_empty:
            continue
        _insert_features_sync(insert_session, result_id, [(part, dict(props_a))])
        total += 1
    return total


def _dict(x: Any) -> dict:
    return dict(x) if x else {}


def _split_geometry_by_type_for_insert(geom: Any) -> list[Any]:
    """Split GeometryCollection into points/lines/polygons for insert; return list of Shapely geoms."""
    from shapely.geometry import GeometryCollection, MultiPoint, MultiLineString, MultiPolygon, Point, LineString, Polygon
    from shapely.validation import make_valid
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, GeometryCollection):
        pts, lines, polys = [], [], []
        for g in geom.geoms:
            if g is None or g.is_empty:
                continue
            if not g.is_valid:
                g = make_valid(g)
                if g is None or g.is_empty:
                    continue
            if isinstance(g, (Polygon, MultiPolygon)):
                polys.append(g)
            elif isinstance(g, (LineString, MultiLineString)):
                lines.append(g)
            elif isinstance(g, (Point, MultiPoint)):
                pts.append(g)
        out = []
        if polys:
            out.append(unary_union(polys))
        if lines:
            out.append(unary_union(lines))
        if pts:
            out.append(unary_union(pts))
        return [g for g in out if g and not g.is_empty]
    return [geom]


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


def _run_intersection_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
    batch_max_bytes: int,
    batch_max_rows: int,
    job_id: str = "",
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
            for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, job_id):
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


def _process_buffer_batch(
    batch_a: list[tuple[str, bytes | None, dict]],
    distance_degrees: float,
) -> list[tuple[Any, dict]]:
    """Process one batch of features: buffer each geometry by distance_degrees. Returns list of (shapely_geom, props)."""
    if distance_degrees <= 0:
        return []
    out: list[tuple[Any, dict]] = []
    for fid, geom_wkb, props in batch_a:
        if geom_wkb is None:
            continue
        try:
            shp = wkb.loads(geom_wkb)
        except Exception:
            continue
        if shp is None or shp.is_empty:
            continue
        try:
            buf = shp.buffer(distance_degrees)
        except Exception:
            continue
        if buf is None or buf.is_empty:
            continue
        if not buf.is_valid:
            try:
                buf = buf.buffer(0)
            except Exception:
                continue
        props_out = dict(props or {})
        props_out.setdefault("_buffer_degrees", distance_degrees)
        props_out.setdefault("_source_id", fid)
        out.append((buf, props_out))
    return out


def _process_explode_batch(
    batch_a: list[tuple[str, bytes | None, dict]],
) -> list[tuple[Any, dict]]:
    """Process one batch of features: explode multi/collection geometries into single-part features."""
    from shapely.geometry import GeometryCollection, MultiPoint, MultiLineString, MultiPolygon

    out: list[tuple[Any, dict]] = []
    for fid, geom_wkb, props in batch_a:
        if geom_wkb is None:
            continue
        try:
            shp = wkb.loads(geom_wkb)
        except Exception:
            continue
        if shp is None or shp.is_empty:
            continue
        geom_list: list[Any] = []
        if isinstance(shp, (MultiPoint, MultiLineString, MultiPolygon, GeometryCollection)):
            for part in shp.geoms:
                if part is None or part.is_empty:
                    continue
                geom_list.append(part)
        else:
            geom_list.append(shp)
        if not geom_list:
            continue
        for idx, g in enumerate(geom_list):
            if g is None or g.is_empty:
                continue
            props_out = dict(props or {})
            props_out.setdefault("_parent_id", fid)
            props_out.setdefault("_part_index", idx)
            out.append((g, props_out))
    return out


def _flatten_make_valid_geometry(geom: Any) -> list[Any]:
    """
    After make_valid: split GeometryCollection and Multi* into atomic Point, LineString, Polygon
    (one output row each), same idea as exploding multi-polygons into separate polygons.
    """
    from shapely.geometry import (
        GeometryCollection,
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
    )

    if geom is None or geom.is_empty:
        return []
    gt = getattr(geom, "geom_type", None)
    if gt == "GeometryCollection":
        out: list[Any] = []
        for g in geom.geoms:
            out.extend(_flatten_make_valid_geometry(g))
        return out
    if gt == "MultiPoint":
        return [p for p in geom.geoms if p is not None and not p.is_empty]
    if gt == "MultiLineString":
        return [ln for ln in geom.geoms if ln is not None and not ln.is_empty]
    if gt == "MultiPolygon":
        return [poly for poly in geom.geoms if poly is not None and not poly.is_empty]
    if gt in ("Point", "LineString", "Polygon"):
        return [geom]
    if gt == "LinearRing":
        try:
            return [LineString(geom.coords)]
        except Exception:
            return []
    return []


def _process_make_valid_batch(
    batch_a: list[tuple[str, bytes | None, dict]],
) -> list[tuple[Any, dict]]:
    """Apply shapely.make_valid per logical feature; split GeometryCollection / Multi* into single-part rows."""
    from shapely.validation import make_valid

    out: list[tuple[Any, dict]] = []
    for fid, geom_wkb, props in batch_a:
        if geom_wkb is None:
            continue
        try:
            shp = wkb.loads(geom_wkb)
        except Exception:
            continue
        if shp is None or shp.is_empty:
            continue
        try:
            fixed = make_valid(shp)
        except Exception:
            continue
        if fixed is None or fixed.is_empty:
            continue
        parts = _flatten_make_valid_geometry(fixed)
        for idx, g in enumerate(parts):
            if g is None or g.is_empty:
                continue
            props_out = dict(props or {})
            props_out.setdefault("_source_id", fid)
            props_out.setdefault("_part_index", idx)
            out.append((g, props_out))
    return out


def _run_erase_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
    batch_max_bytes: int,
    batch_max_rows: int,
    job_id: str = "",
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
            for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, job_id):
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


def process_process_job_sync(payload: ProcessJobPayload) -> tuple[str | None, int, int, str]:
    """
    Run intersection or erase process (collection vs collection or single feature vs layers).
    Returns (None, count, items_in, result_id) on success, (error_message, 0, 0, "") on failure.
    """
    import os
    import time
    from sqlalchemy import create_engine

    settings = get_settings()
    connect_args: dict = {}
    timeout = getattr(settings, "process_worker_statement_timeout_seconds", 0) or 0
    if timeout > 0:
        connect_args["options"] = f"-c statement_timeout={timeout * 1000}"  # milliseconds
    engine = create_engine(
        settings.database_sync_url,
        pool_pre_ping=True,
        future=True,
        pool_size=5,
        max_overflow=2,
        connect_args=connect_args,
    )

    if payload.is_feature_vs_layers:
        result_id = ""
        try:
            if payload.feature_ref:
                geom_a, props_a = _get_single_feature_geometry_sync(
                    engine,
                    payload.feature_ref["collection_id"],
                    payload.feature_ref["feature_id"],
                )
            else:
                geom_a = _get_single_geometry_from_geojson(payload.feature_geojson or {})
                props_a = {}
            if geom_a is None or geom_a.is_empty:
                engine.dispose()
                return ("Feature geometry is empty or invalid.", 0, 0, "")
            if payload.update_existing and payload.result_collection_id:
                result_id = payload.result_collection_id
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT id FROM collections WHERE id = :cid"),
                        {"cid": result_id},
                    ).first()
                    if not row:
                        engine.dispose()
                        return (f"Collection to update not found: {result_id}", 0, 0, "")
            elif payload.result_collection_id:
                result_id = _sanitize_result_collection_id(payload.result_collection_id)
            else:
                feature_id = payload.feature_ref.get("feature_id", "geojson") if payload.feature_ref else "geojson"
                result_id = _default_result_collection_id_feature(
                    payload.process_id, feature_id, payload.collection_ids
                )
            layer_list = ", ".join(payload.collection_ids)
            title = result_id
            description = f"Between a feature and {len(payload.collection_ids)} layers: {layer_list}."
            if not payload.update_existing:
                _ensure_collection_and_partition_sync(engine, result_id, title, description=description)
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                _clear_result_collection_sync(insert_session, result_id)
                op_label = "intersection" if payload.process_id == "intersection" else "erase"
                last_progress = [0.0]

                def on_layer_progress(i: int, n: int, cid: str) -> None:
                    import time
                    now = time.monotonic()
                    if now - last_progress[0] < 2.0:
                        return
                    last_progress[0] = now
                    update_job(
                        payload.job_id,
                        status="running",
                        message=f"Computing {op_label}… layer {i}/{n} ({cid})",
                    )

                if payload.process_id == "intersection":
                    count = _run_intersection_feature_vs_layers_sync(
                        engine,
                        result_id,
                        geom_a,
                        props_a,
                        payload.collection_ids,
                        insert_session,
                        payload.job_id,
                        progress_callback=on_layer_progress,
                    )
                else:
                    count = _run_erase_feature_vs_layers_sync(
                        engine,
                        result_id,
                        geom_a,
                        props_a,
                        payload.collection_ids,
                        insert_session,
                        payload.job_id,
                        progress_callback=on_layer_progress,
                    )
            finally:
                insert_session.close()
            try:
                _update_feature_count_sync(engine, result_id)
            except Exception:
                pass
            # Store bbox on the collection immediately (same engine, post-commit inserts are visible).
            try:
                collections_crud.recompute_and_update_collection_extent_sync(engine, result_id)
            except Exception:
                pass
            engine.dispose()
            return (None, count, 1, result_id)
        except ProcessCancelled:
            _cleanup_after_process_cancel(engine, payload, result_id)
            engine.dispose()
            return ("cancelled", 0, 0, result_id)
        except Exception as e:
            engine.dispose()
            return (str(e), 0, 0, "")

    # First engine was only for feature-vs-layers; main path uses a larger pool below.
    engine.dispose()
    cpu_count = getattr(os, "cpu_count", lambda: 4)() or 4
    max_workers = max(1, settings.process_batch_workers or cpu_count)
    batch_max_bytes = max(1024, settings.process_batch_max_bytes)
    batch_max_rows = max(0, settings.process_batch_max_rows)
    pool_size = max(4, min(max_workers + 2, 8))  # cap to leave headroom for API connections
    _connect_args: dict = {}
    _timeout = getattr(settings, "process_worker_statement_timeout_seconds", 0) or 0
    if _timeout > 0:
        _connect_args["options"] = f"-c statement_timeout={_timeout * 1000}"
    engine = create_engine(
        settings.database_sync_url,
        pool_pre_ping=True,
        future=True,
        pool_size=pool_size,
        max_overflow=min(2, max_workers),
        connect_args=_connect_args,
    )

    if payload.update_existing and payload.result_collection_id:
        result_id = payload.result_collection_id
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM collections WHERE id = :cid"),
                {"cid": result_id},
            ).first()
            if not row:
                engine.dispose()
                return (f"Collection to update not found: {result_id}", 0, 0, "")
    elif payload.result_collection_id:
        result_id = _sanitize_result_collection_id(payload.result_collection_id)
    else:
        result_id = _default_result_collection_id(
            payload.process_id, payload.collection_id_a, payload.collection_id_b
        )
    # Measure tool updates properties in-place; do not create/clear collections.
    if payload.process_id == "measure":
        try:
            # Require in-place update target (collection_id_a == result_id in typical usage).
            target_cid = result_id
            field_name = (payload.measure_field or "").strip()
            op = (payload.measure_op or "").strip().lower()
            unit = (payload.measure_unit or "").strip().lower()
            if not field_name:
                engine.dispose()
                return ("measure_field is required (properties key to write).", 0, 0, "")
            if op not in ("area", "length", "perimeter"):
                engine.dispose()
                return ("measure_op must be one of: area, length, perimeter.", 0, 0, "")
            # Unit conversion factor (base units: meters for length/perimeter, m^2 for area)
            if op == "area":
                factors = {"m2": 1.0, "sqm": 1.0, "ha": 1.0 / 10_000.0, "hectare": 1.0 / 10_000.0, "ac": 1.0 / 4046.8564224, "acre": 1.0 / 4046.8564224, "km2": 1.0 / 1_000_000.0, "sqkm": 1.0 / 1_000_000.0}
            else:
                factors = {"m": 1.0, "meter": 1.0, "km": 1.0 / 1000.0, "kilometer": 1.0 / 1000.0}
            factor = factors.get(unit)
            if factor is None:
                engine.dispose()
                return (f"Unsupported measure_unit for {op}: {unit}", 0, 0, "")
            # Optional filter by feature ids (match UI behavior of other single-layer tools)
            ids = list(payload.feature_ids or [])
            update_job(payload.job_id, status="running", message=f"Computing {op}…")
            _raise_if_cancelled(payload.job_id)
            with engine.connect() as conn:
                params: dict[str, Any] = {"cid": target_cid, "field": field_name, "factor": float(factor), "now": datetime.now(timezone.utc)}
                ids_clause = ""
                if ids:
                    ids_clause = " AND id = ANY(:ids)"
                    params["ids"] = ids
                # Compute per logical feature id using unioned geometry, then update all part rows for that id.
                sql = f"""
                    WITH u AS (
                        SELECT id, ST_UnaryUnion(ST_Collect(geometry)) AS g
                        FROM features
                        WHERE collection_id = :cid AND geometry IS NOT NULL {ids_clause}
                        GROUP BY id
                    ),
                    m AS (
                        SELECT id,
                            CASE
                                WHEN :op = 'area' THEN ST_Area(g::geography)
                                WHEN :op = 'length' THEN ST_Length(g::geography)
                                ELSE (
                                    CASE
                                        WHEN GeometryType(g) = 'POLYGON' THEN ST_Length(ST_ExteriorRing(g)::geography)
                                        WHEN GeometryType(g) = 'MULTIPOLYGON' THEN (
                                            SELECT COALESCE(SUM(ST_Length(ST_ExteriorRing((d).geom)::geography)), 0)
                                            FROM ST_Dump(g) AS d
                                        )
                                        ELSE ST_Length(g::geography)
                                    END
                                )
                            END AS v
                        FROM u
                    )
                    UPDATE features f
                    SET properties = jsonb_set(COALESCE(f.properties, '{{}}'::jsonb), ARRAY[:field], to_jsonb((m.v * :factor)::double precision), true),
                        updated_at = :now
                    FROM m
                    WHERE f.collection_id = :cid AND f.id = m.id
                """
                params["op"] = op
                res = conn.execute(text(sql), params)
                conn.commit()
                updated_rows = int(getattr(res, "rowcount", 0) or 0)
            # Rowcount is parts updated; report logical features updated (distinct ids)
            items_in = len(ids) if ids else 0
            update_job(payload.job_id, status="running", message=f"Updated {field_name} for {updated_rows} rows.")
            try:
                collections_crud.recompute_and_update_collection_extent_sync(engine, target_cid)
            except Exception:
                pass
            engine.dispose()
            return (None, updated_rows, items_in, target_cid)
        except ProcessCancelled:
            engine.dispose()
            return ("cancelled", 0, 0, result_id)
        except Exception as e:
            engine.dispose()
            return (str(e), 0, 0, "")
    if payload.process_id == "buffer":
        title = f"Buffer of {payload.collection_id_a}"
    elif payload.process_id == "make_valid":
        title = f"Make valid of {payload.collection_id_a}"
    else:
        title = f"{payload.process_id.title()} of {payload.collection_id_a} and {payload.collection_id_b}"

    try:
        if not payload.update_existing:
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
            _raise_if_cancelled(payload.job_id)
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
            # Stream (id_a, id_b) pairs from DB in chunks; for each chunk fetch A/B, intersect, write directly to result partition. No temp files, no aggregation.
            pair_chunk_size = max(1, getattr(settings, "process_intersection_pair_chunk_size", 400))
            stream_session = SessionLocal()
            insert_session = SessionLocal()
            try:
                def on_pairs_progress(n: int) -> None:
                    maybe_update("Finding pairs…")

                for pair_chunk in _stream_intersection_pairs_chunks(
                    stream_session,
                    payload.collection_id_a,
                    payload.collection_id_b,
                    pair_chunk_size,
                    on_progress=on_pairs_progress,
                ):
                    _raise_if_cancelled(payload.job_id)
                    items_in += len(pair_chunk)
                    maybe_update("Computing intersection…")
                    chunk_results = _process_intersection_pairs_chunk(
                        engine, payload.collection_id_a, payload.collection_id_b, pair_chunk
                    )
                    if chunk_results:
                        _insert_features_sync(insert_session, result_id, chunk_results)
                        items_out += len(chunk_results)
                count = items_out
            finally:
                stream_session.close()
                insert_session.close()

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
                        for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, payload.job_id):
                            if not batch_a:
                                continue
                            _raise_if_cancelled(payload.job_id)
                            items_in += len(batch_a)
                            maybe_update("Computing erase…")
                            while len(pending) >= max_pending:
                                _raise_if_cancelled(payload.job_id)
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
                            _raise_if_cancelled(payload.job_id)
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
        elif payload.process_id == "buffer":
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                session = SessionLocal()
                try:
                    params: dict[str, Any] = {"cid": payload.collection_id_a}
                    sql = """
                        SELECT id, geometry, properties
                        FROM features
                        WHERE collection_id = :cid AND geometry IS NOT NULL
                    """
                    if payload.feature_ids:
                        sql += " AND id = ANY(:ids)"
                        params["ids"] = payload.feature_ids
                    result = session.execute(
                        text(sql),
                        params,
                        execution_options={"stream_results": True},
                    )
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        pending: set = set()
                        max_pending = max(max_workers * 2, 4)
                        for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, payload.job_id):
                            if not batch_a:
                                continue
                            _raise_if_cancelled(payload.job_id)
                            items_in += len(batch_a)
                            maybe_update("Computing buffer…")
                            while len(pending) >= max_pending:
                                _raise_if_cancelled(payload.job_id)
                                done = [f for f in pending if f.done()]
                                for f in done:
                                    pending.discard(f)
                                    chunk_results = f.result()
                                    if chunk_results:
                                        _insert_features_sync(insert_session, result_id, chunk_results)
                                        items_out += len(chunk_results)
                                        maybe_update("Writing results…")
                            fut = executor.submit(_process_buffer_batch, batch_a, float(payload.buffer_distance_degrees or 0.0))
                            pending.add(fut)
                        for fut in as_completed(pending):
                            _raise_if_cancelled(payload.job_id)
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
        elif payload.process_id == "explode":
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                session = SessionLocal()
                try:
                    params: dict[str, Any] = {"cid": payload.collection_id_a}
                    sql = """
                        SELECT id, geometry, properties
                        FROM features
                        WHERE collection_id = :cid AND geometry IS NOT NULL
                    """
                    if payload.feature_ids:
                        sql += " AND id = ANY(:ids)"
                        params["ids"] = payload.feature_ids
                    result = session.execute(
                        text(sql),
                        params,
                        execution_options={"stream_results": True},
                    )
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        pending: set = set()
                        max_pending = max(max_workers * 2, 4)
                        for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, payload.job_id):
                            if not batch_a:
                                continue
                            _raise_if_cancelled(payload.job_id)
                            items_in += len(batch_a)
                            maybe_update("Exploding geometries…")
                            while len(pending) >= max_pending:
                                _raise_if_cancelled(payload.job_id)
                                done = [f for f in pending if f.done()]
                                for f in done:
                                    pending.discard(f)
                                    chunk_results = f.result()
                                    if chunk_results:
                                        _insert_features_sync(insert_session, result_id, chunk_results)
                                        items_out += len(chunk_results)
                                        maybe_update("Writing results…")
                            fut = executor.submit(_process_explode_batch, batch_a)
                            pending.add(fut)
                        for fut in as_completed(pending):
                            _raise_if_cancelled(payload.job_id)
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
        elif payload.process_id == "make_valid":
            # One row per logical feature: union parts, then make_valid in Python; split collections / multiparts.
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                session = SessionLocal()
                try:
                    params: dict[str, Any] = {"cid": payload.collection_id_a}
                    sql = """
                        SELECT id,
                               ST_UnaryUnion(ST_Collect(geometry)) AS geometry,
                               (array_agg(properties ORDER BY part_index))[1] AS properties
                        FROM features
                        WHERE collection_id = :cid AND geometry IS NOT NULL
                    """
                    if payload.feature_ids:
                        sql += " AND id = ANY(:ids)"
                        params["ids"] = list(payload.feature_ids)
                    sql += " GROUP BY id ORDER BY id"
                    result = session.execute(
                        text(sql),
                        params,
                        execution_options={"stream_results": True},
                    )
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        pending: set = set()
                        max_pending = max(max_workers * 2, 4)
                        for batch_a in _stream_batches_by_size(result, batch_max_bytes, batch_max_rows, payload.job_id):
                            if not batch_a:
                                continue
                            _raise_if_cancelled(payload.job_id)
                            items_in += len(batch_a)
                            maybe_update("Making geometries valid…")
                            while len(pending) >= max_pending:
                                _raise_if_cancelled(payload.job_id)
                                done = [f for f in pending if f.done()]
                                for f in done:
                                    pending.discard(f)
                                    chunk_results = f.result()
                                    if chunk_results:
                                        _insert_features_sync(insert_session, result_id, chunk_results)
                                        items_out += len(chunk_results)
                                        maybe_update("Writing results…")
                            fut = executor.submit(_process_make_valid_batch, batch_a)
                            pending.add(fut)
                        for fut in as_completed(pending):
                            _raise_if_cancelled(payload.job_id)
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
        elif payload.process_id == "union":
            # Single-layer union (dissolve). Aggregate per batch in SQL, then merge into result table.
            # One UPDATE per group per batch (not per feature) to avoid 20GB+ disk bloat from MVCC.
            union_batch_size = max(1, getattr(settings, "process_union_batch_size", 2000))
            # Union removes overlaps. Then we explode into individual simple geometries and insert each
            # as its own feature to keep items/tiles responsive (avoid one massive logical feature).
            # For extremely large parts, slice in Python down to MAX_COORDS_FOR_DB_SUBDIVIDE before insert,
            # then let the DB do its own ST_Subdivide work (≤ max_vertices).
            from app.utils.feature_subdivide import (
                MAX_COORDS_FOR_DB_SUBDIVIDE,
                _coord_count,
                subdivide_geometry_by_vertices,
            )
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
            insert_session = SessionLocal()
            try:
                with engine.connect() as conn:
                    safe_id = payload.job_id.replace("-", "_")
                    tmp_table = f"tmp_union_{safe_id}"
                    batch_tmp = f"tmp_union_batch_{safe_id}"
                    conn.execute(text(
                        f"CREATE TEMP TABLE {tmp_table} (group_val text PRIMARY KEY, geom geometry(Geometry,4326))"
                    ))
                    conn.execute(text(
                        f"CREATE TEMP TABLE {batch_tmp} (group_val text, geom geometry(Geometry,4326))"
                    ))
                    conn.commit()
                    params: dict[str, Any] = {"cid": payload.collection_id_a}
                    sql = """
                        SELECT id, ST_AsBinary(geometry) AS geom_wkb, properties
                        FROM features
                        WHERE collection_id = :cid AND geometry IS NOT NULL
                    """
                    if payload.feature_ids:
                        sql += " AND id = ANY(:ids)"
                        params["ids"] = payload.feature_ids
                    offset = 0
                    while True:
                        _raise_if_cancelled(payload.job_id)
                        batch_sql = sql + f" ORDER BY id, part_index LIMIT {union_batch_size} OFFSET {offset}"
                        rows_batch = conn.execute(text(batch_sql), params).fetchall()
                        if not rows_batch:
                            break
                        conn.execute(text(f"TRUNCATE {batch_tmp}"))
                        batch_has_rows = False
                        for row in rows_batch:
                            props = getattr(row, "properties", None) or row[2] or {}
                            gval = "_all"
                            if payload.group_by_property:
                                v = (props or {}).get(payload.group_by_property)
                                gval = "" if v is None else str(v)
                            raw = getattr(row, "geom_wkb", None) or row[1]
                            if raw is None:
                                continue
                            # Normalize to bytes: driver may return bytes, memoryview, or hex str (e.g. \x0103...)
                            if isinstance(raw, (memoryview, bytearray)):
                                gbytes = bytes(raw)
                            elif isinstance(raw, str):
                                s = raw.strip()
                                if s.startswith("\\x") or s.startswith("\\\\x"):
                                    gbytes = bytes.fromhex(s.replace("\\x", "").replace("\\\\x", ""))
                                else:
                                    try:
                                        gbytes = wkb.loads(s, hex=True).wkb
                                    except Exception:
                                        continue
                            else:
                                gbytes = raw if isinstance(raw, bytes) else None
                            if not gbytes or len(gbytes) == 0:
                                continue
                            conn.execute(
                                text(f"INSERT INTO {batch_tmp} (group_val, geom) VALUES (:g, ST_GeomFromWKB(:ggeom, 4326))"),
                                {"g": gval, "ggeom": gbytes},
                            )
                            items_in += 1
                            batch_has_rows = True
                        # One union per group for this batch, then merge into tmp_table (one UPDATE per group).
                        if not batch_has_rows:
                            offset += union_batch_size
                            if len(rows_batch) < union_batch_size:
                                break
                            continue
                        conn.execute(text(f"""
                            WITH batch_union AS (
                                SELECT group_val, ST_UnaryUnion(ST_Collect(geom)) AS geom
                                FROM {batch_tmp}
                                GROUP BY group_val
                            ),
                            merged AS (
                                UPDATE {tmp_table} t
                                SET geom = ST_UnaryUnion(ST_Collect(t.geom, b.geom))
                                FROM batch_union b
                                WHERE t.group_val = b.group_val
                                RETURNING t.group_val
                            )
                            INSERT INTO {tmp_table} (group_val, geom)
                            SELECT b.group_val, b.geom
                            FROM batch_union b
                            WHERE b.group_val NOT IN (SELECT group_val FROM merged)
                        """))
                        maybe_update("Computing union (dissolve)…")
                        conn.commit()
                        offset += union_batch_size
                        if len(rows_batch) < union_batch_size:
                            break
                    rows = conn.execute(text(f"SELECT group_val, ST_AsBinary(geom) FROM {tmp_table}")).fetchall()
                union_feats = []
                for row in rows:
                    _raise_if_cancelled(payload.job_id)
                    gval = row[0]
                    geom_wkb = row[1]
                    if not geom_wkb:
                        continue
                    try:
                        if isinstance(geom_wkb, str):
                            s = geom_wkb.strip().replace("\\x", "").replace("\\\\x", "")
                            shp = wkb.loads(s, hex=True)
                        else:
                            b = bytes(geom_wkb) if isinstance(geom_wkb, (memoryview, bytearray)) else geom_wkb
                            shp = wkb.loads(b)
                    except Exception:
                        continue
                    if shp is None or shp.is_empty:
                        continue
                    props_out: dict[str, Any] = {}
                    if payload.group_by_property and gval != "":
                        props_out[payload.group_by_property] = gval
                    # Explode multiparts/collections into individual geometries (one feature per geom).
                    exploded: list[Any] = []
                    try:
                        if getattr(shp, "geom_type", None) in ("MultiPolygon", "MultiLineString", "MultiPoint", "GeometryCollection"):
                            exploded = [g for g in getattr(shp, "geoms", []) if g is not None and not g.is_empty]
                        else:
                            exploded = [shp]
                    except Exception:
                        exploded = [shp]
                    # If any single geometry is still too large for safe DB-side handling, slice it down first
                    # so downstream queries never have to materialize an enormous geometry.
                    for geom_piece in exploded:
                        if geom_piece is None or geom_piece.is_empty:
                            continue
                        if _coord_count(geom_piece) > MAX_COORDS_FOR_DB_SUBDIVIDE:
                            sliced = subdivide_geometry_by_vertices(geom_piece, MAX_COORDS_FOR_DB_SUBDIVIDE)
                            for s in sliced:
                                if s is None or s.is_empty:
                                    continue
                                union_feats.append((s, props_out))
                        else:
                            union_feats.append((geom_piece, props_out))
                if union_feats:
                    _insert_features_sync(insert_session, result_id, union_feats)
                    items_out += len(union_feats)
                    maybe_update("Writing results…")
                count = items_out
            finally:
                try:
                    insert_session.close()
                except Exception:
                    pass
        else:
            engine.dispose()
            return (f"Unknown process: {payload.process_id}", 0, 0, "")

        # Update cached feature_count and stored extent (bbox) from written geometries.
        try:
            _update_feature_count_sync(engine, result_id)
        except Exception:
            # Don't fail the whole job if this bookkeeping step has an issue.
            pass
        try:
            collections_crud.recompute_and_update_collection_extent_sync(engine, result_id)
        except Exception:
            pass

        engine.dispose()
        return (None, count, items_in, result_id)
    except ProcessCancelled:
        _cleanup_after_process_cancel(engine, payload, result_id)
        engine.dispose()
        return ("cancelled", 0, 0, result_id)
    except Exception as e:
        engine.dispose()
        return (str(e), 0, 0, "")
