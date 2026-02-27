"""Build PMTiles for a collection: export GeoJSONSeq (streaming), run tippecanoe, save and register."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from queue import Queue

import orjson

from app.core.config import get_settings

# Chunk size for DB streaming and queue between producer/consumer
_EXPORT_CHUNK_SIZE = 50_000
_QUEUE_MAX_SIZE = 8  # allow producer to read ahead so file write is not the bottleneck


def _consumer(queue: Queue, file_handle) -> None:
    """Consume (id, geometry_geojson_str, properties) chunks; build GeoJSONSeq lines; batch-write to file."""
    while True:
        chunk = queue.get()
        if chunk is None:
            queue.task_done()
            break
        lines: list[bytes] = []
        for fid, geom_str, props in chunk:
            geom_dict = json.loads(geom_str) if geom_str else None
            props_dict = dict(props) if props else {}
            if "id" not in props_dict:
                props_dict["id"] = fid
            feat = {
                "type": "Feature",
                "id": fid,
                "geometry": geom_dict,
                "properties": props_dict,
            }
            lines.append(orjson.dumps(feat, option=orjson.OPT_APPEND_NEWLINE))
        if lines:
            file_handle.write(b"".join(lines))
        queue.task_done()


def build_pmtiles_sync(collection_id: str) -> str | None:
    """
    Export collection to GeoJSONSeq (streaming, producer-consumer), run tippecanoe, save and register.
    Returns error message or None on success.
    Uses sync DB; run in thread/worker process.
    """
    from datetime import datetime, timezone
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session, sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    tiles_dir = settings.tiles_storage_path
    os.makedirs(tiles_dir, exist_ok=True)
    out_path = os.path.join(tiles_dir, f"{collection_id}.pmtiles")
    minz = settings.tippecanoe_minzoom
    maxz = settings.tippecanoe_maxzoom

    with SessionLocal() as session:
        row = session.execute(
            text("SELECT MAX(updated_at) AS m FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        max_updated = row.m if row and row.m else None
        count_row = session.execute(
            text("SELECT COUNT(*) AS n FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        feature_count = count_row.n if count_row and count_row.n else 0

    if feature_count == 0:
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except OSError:
            pass
        with SessionLocal() as s:
            s.execute(
                text("""
                    INSERT INTO collection_tiles (collection_id, pmtiles_path, built_at, features_updated_at)
                    VALUES (:cid, NULL, :now, NULL)
                    ON CONFLICT (collection_id) DO UPDATE SET
                        pmtiles_path = NULL, built_at = :now, features_updated_at = NULL
                """),
                {"cid": collection_id, "now": datetime.now(timezone.utc)},
            )
            s.commit()
        engine.dispose()
        return None

    def row_data(r):
        return (r.id, r.geometry, dict(r.properties) if r.properties else None)

    fd, geojsonl_path = tempfile.mkstemp(suffix=".geojsonl")
    try:
        with os.fdopen(fd, "wb") as f:
            queue: Queue = Queue(maxsize=_QUEUE_MAX_SIZE)
            consumer_thread = threading.Thread(target=_consumer, args=(queue, f))
            consumer_thread.start()

            total_features = 0
            with SessionLocal() as session:
                result = session.execute(
                    text(
                        "SELECT id, ST_AsGeoJSON(geometry)::text AS geometry, properties "
                        "FROM features WHERE collection_id = :cid"
                    ),
                    {"cid": collection_id},
                    execution_options={"stream_results": True},
                )
                for partition in result.partitions(_EXPORT_CHUNK_SIZE):
                    chunk = [row_data(r) for r in partition]
                    queue.put(chunk)
                    total_features += len(chunk)
            queue.put(None)
            queue.join()
            consumer_thread.join()

        # -L requires "layername:file" (single argument per layer)
        cmd = [
            "tippecanoe",
            "--read-parallel",
            "-o", out_path,
            "-L", f"{collection_id}:{geojsonl_path}",
            "-z", str(maxz),
            "-Z", str(minz),
            "--force",
            "--grid-low-zooms",
            "--no-simplification-of-shared-nodes",
            "--coalesce-densest-as-needed",
            
        ]
        print(f"[tile_builder] Running tippecanoe for {collection_id} ({total_features} features)...", file=sys.stderr, flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            err = proc.stderr or proc.stdout or "tippecanoe failed"
            print(f"[tile_builder] tippecanoe failed: {err[:500]}", file=sys.stderr, flush=True)
            return err
    finally:
        try:
            os.unlink(geojsonl_path)
        except OSError:
            pass

    # Delete old file if it was at a different path; then upsert new path
    with SessionLocal() as session:
        old = session.execute(
            text("SELECT pmtiles_path FROM collection_tiles WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        old_path = (old[0] if old else None)
        if old_path and old_path != out_path and os.path.exists(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        session.execute(
            text("""
                INSERT INTO collection_tiles (collection_id, pmtiles_path, built_at, features_updated_at)
                VALUES (:cid, :path, :now, :fua)
                ON CONFLICT (collection_id) DO UPDATE SET
                    pmtiles_path = EXCLUDED.pmtiles_path,
                    built_at = EXCLUDED.built_at,
                    features_updated_at = EXCLUDED.features_updated_at
            """),
            {"cid": collection_id, "path": out_path, "now": datetime.now(timezone.utc), "fua": max_updated},
        )
        session.commit()

    engine.dispose()
    return None
