"""OGC API - Processes worker: intersection and erase between two collections. Computes in Python (Shapely), streams from DB."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from shapely import wkb
from shapely.ops import unary_union
from shapely.strtree import STRtree
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.services.process_queue import ProcessJobPayload

_BATCH_INSERT = 500
_CHUNK_STREAM = 5000
_MAX_RESULT_COLLECTION_ID_LEN = 60


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
    """Delete all features in the result collection (so we can refill)."""
    session.execute(text("DELETE FROM features WHERE collection_id = :cid"), {"cid": result_id})
    session.commit()


def _insert_features_sync(session: Session, result_id: str, features: list[tuple[Any, dict]]) -> None:
    """Insert (shapely_geom, properties) into features for result_id. Geometry stored as WKT."""
    from uuid6 import uuid7
    now = datetime.now(timezone.utc)
    for i in range(0, len(features), _BATCH_INSERT):
        batch = features[i : i + _BATCH_INSERT]
        for geom_shapely, props in batch:
            if geom_shapely is None or geom_shapely.is_empty:
                continue
            fid = str(uuid7())
            session.execute(
                text("""
                    INSERT INTO features (id, collection_id, geometry, properties, created_at, updated_at)
                    VALUES (:id, :cid, ST_GeomFromText(:wkt, 4326), :props::jsonb, :now, :now)
                """),
                {
                    "id": fid,
                    "cid": result_id,
                    "wkt": geom_shapely.wkt,
                    "props": json.dumps(props) if props else None,
                    "now": now,
                },
            )
        session.commit()


def _run_intersection_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
) -> int:
    """Load A with STRtree, stream B, compute intersections in parallel, insert. Returns count inserted."""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()

    # Load collection A into memory (id, shapely_geom, props)
    rows_a = list(
        session.execute(
            text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
            {"cid": collection_id_a},
        ).fetchall()
    )
    session.close()
    list_a = []
    for r in rows_a:
        shp = _geom_to_shapely(r.geometry)
        if shp is None or shp.is_empty:
            continue
        list_a.append((r.id, shp, dict(r.properties) if r.properties else {}))
    if not list_a:
        return 0

    tree = STRtree([x[1] for x in list_a])
    results = []

    def process_b_chunk(b_chunk: list) -> list:
        out = []
        for r in b_chunk:
            shp_b = _geom_to_shapely(r.geometry)
            if shp_b is None or shp_b.is_empty:
                continue
            for idx in tree.query(shp_b):
                _, shp_a, props_a = list_a[idx]
                if not shp_a.intersects(shp_b):
                    continue
                try:
                    inter = shp_a.intersection(shp_b)
                    if inter.is_empty or not inter.is_valid:
                        continue
                    props = {**props_a, **dict(r.properties or {})}
                    props["_id_a"] = list_a[idx][0]
                    props["_id_b"] = r.id
                    out.append((inter, props))
                except Exception:
                    continue
        return out

    session = SessionLocal()
    try:
        result = session.execute(
            text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
            {"cid": collection_id_b},
            execution_options={"stream_results": True},
        )
        for partition in result.partitions(_CHUNK_STREAM):
            chunk = list(partition)
            if not chunk:
                continue
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for f in as_completed([ex.submit(process_b_chunk, chunk)]):
                    results.extend(f.result())
    finally:
        session.close()

    session = SessionLocal()
    _insert_features_sync(session, result_id, results)
    session.close()
    return len(results)


def _run_erase_sync(
    engine: Engine,
    result_id: str,
    collection_id_a: str,
    collection_id_b: str,
    max_workers: int,
) -> int:
    """Stream A; for each a, get intersecting B (from DB or from preloaded B), compute a.difference(union(B)), insert."""
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = SessionLocal()

    # Load B into memory + STRtree for fast spatial lookup
    rows_b = list(
        session.execute(
            text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
            {"cid": collection_id_b},
        ).fetchall()
    )
    list_b = [(_geom_to_shapely(r.geometry), r.id) for r in rows_b]
    list_b = [(g, i) for g, i in list_b if g is not None and not g.is_empty]
    session.close()
    if not list_b:
        tree_b = None
    else:
        tree_b = STRtree([x[0] for x in list_b])

    results = []

    session = SessionLocal()
    result = session.execute(
        text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid AND geometry IS NOT NULL"),
        {"cid": collection_id_a},
        execution_options={"stream_results": True},
    )
    for partition in result.partitions(_CHUNK_STREAM):
        for row in partition:
            shp_a = _geom_to_shapely(row.geometry)
            if shp_a is None or shp_a.is_empty:
                continue
            if tree_b is None:
                diff = shp_a
            else:
                indices = tree_b.query(shp_a)
                if not indices:
                    diff = shp_a
                else:
                    geoms_b = [list_b[i][0] for i in indices]
                    union_b = unary_union(geoms_b)
                    try:
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
            props = dict(row.properties) if row.properties else {}
            props["_id_a"] = row.id
            results.append((diff, props))
    session.close()

    session = SessionLocal()
    _insert_features_sync(session, result_id, results)
    session.close()
    return len(results)


def process_process_job_sync(payload: ProcessJobPayload) -> tuple[str | None, int]:
    """
    Run intersection or erase process. Creates result collection, streams from DB, computes with Shapely, inserts.
    Returns (None, count) on success, (error_message, 0) on failure.
    """
    from sqlalchemy import create_engine

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    max_workers = max(1, min(settings.process_max_concurrent, 8))

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

        if payload.process_id == "intersection":
            count = _run_intersection_sync(
                engine, result_id,
                payload.collection_id_a, payload.collection_id_b,
                max_workers,
            )
        elif payload.process_id == "erase":
            count = _run_erase_sync(
                engine, result_id,
                payload.collection_id_a, payload.collection_id_b,
                max_workers,
            )
        else:
            engine.dispose()
            return (f"Unknown process: {payload.process_id}", 0)

        engine.dispose()
        return (None, count)
    except Exception as e:
        engine.dispose()
        return (str(e), 0)
