"""Build MBTiles for a collection: export GeoJSONSeq (streaming), run tippecanoe, save and register."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
import threading
import time
from queue import Queue
from typing import Callable

import orjson

from app.core.config import get_settings
from app.services.collection_tiles_revision import compute_collection_tiles_revision
from app.services.tile_build_queue import TileBuildOptions
from app.utils.geo import mvt_layer_name

# Returned by build_pmtiles_sync when build was cancelled via stop_check (worker should not mark failed/completed).
BUILD_CANCELLED = "__cancelled__"

# Chunk size for DB streaming and queue between producer/consumer
_EXPORT_CHUNK_SIZE = 50_000
_QUEUE_MAX_SIZE = 8  # allow producer to read ahead so file write is not the bottleneck


def _stream_pipe(pipe, target, capture: list[str] | None = None) -> None:
    """Forward process output to target stream and optionally capture it."""
    try:
        while True:
            line = pipe.readline()
            if not line:
                break
            target.write(line)
            target.flush()
            if capture is not None:
                capture.append(line)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _consumer(queue: Queue, file_handle) -> None:
    """Consume (id, geometry_geojson_str, properties) chunks; build GeoJSONSeq lines; batch-write to file.
    Uses numeric feature ids (Mapbox tippecanoe requirement); original id kept in properties."""
    feature_index = 0
    while True:
        chunk = queue.get()
        if chunk is None:
            queue.task_done()
            break
        lines: list[bytes] = []
        for fid, geom_str, props in chunk:
            geom_dict = json.loads(geom_str) if geom_str else None
            props_dict = dict(props) if props else {}
            # Reserve "id" for our API feature id (UUID/string) so popups link correctly.
            # If source data already has a property named "id", move it aside to avoid conflicts.
            if "id" in props_dict and props_dict.get("id") != fid:
                key = "id_source"
                while key in props_dict:
                    key = key + "_1"
                props_dict[key] = props_dict.get("id")
            props_dict["id"] = fid
            # Mapbox tippecanoe requires numeric Feature id; keep original id in properties
            feat = {
                "type": "Feature",
                "id": feature_index,
                "geometry": geom_dict,
                "properties": props_dict,
            }
            feature_index += 1
            lines.append(orjson.dumps(feat, option=orjson.OPT_APPEND_NEWLINE))
        if lines:
            file_handle.write(b"".join(lines))
        queue.task_done()


def build_pmtiles_sync(
    collection_id: str,
    options: TileBuildOptions | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> str | None:
    """
    Export collection to GeoJSONSeq (streaming, producer-consumer), run tippecanoe, save and register.
    Returns error message, None on success, or BUILD_CANCELLED when stop_check() returned True (caller should cleanup job state).
    Uses sync DB; run in thread/worker process.
    options: optional overrides for min/max zoom, attributes, densest/smallest strategy; None = use config defaults.
    stop_check: optional callable; when it returns True, abort and cleanup intermediate/partial files, return BUILD_CANCELLED.
    """
    from datetime import datetime, timezone
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session, sessionmaker

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    tiles_dir = settings.tiles_storage_path
    os.makedirs(tiles_dir, exist_ok=True)
    out_path_final = os.path.join(tiles_dir, f"{collection_id}.mbtiles")
    out_path_tmp: str | None = None  # tippecanoe output; swapped into out_path_final only on success
    opts = options or TileBuildOptions()
    minz = opts.min_zoom if opts.min_zoom is not None else settings.tippecanoe_minzoom
    maxz = opts.max_zoom if opts.max_zoom is not None else settings.tippecanoe_maxzoom

    with SessionLocal() as session:
        row = session.execute(
            text("SELECT MAX(updated_at) AS m FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        max_updated = row.m if row and row.m else None
        count_row = session.execute(
            text("SELECT COUNT(DISTINCT id) AS n FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        feature_count = count_row.n if count_row and count_row.n else 0

    if feature_count == 0:
        try:
            if os.path.exists(out_path_final):
                os.unlink(out_path_final)
        except OSError:
            pass
        with SessionLocal() as s:
            s.execute(
                text("""
                    INSERT INTO collection_tiles (collection_id, pmtiles_path, built_at, features_updated_at, minzoom, maxzoom, tiles_revision)
                    VALUES (:cid, NULL, :now, NULL, NULL, NULL, NULL)
                    ON CONFLICT (collection_id) DO UPDATE SET
                        pmtiles_path = NULL, built_at = :now, features_updated_at = NULL, minzoom = NULL, maxzoom = NULL, tiles_revision = NULL
                """),
                {"cid": collection_id, "now": datetime.now(timezone.utc)},
            )
            s.commit()
        engine.dispose()
        return None

    def row_data(r):
        return (r.id, r.geometry, dict(r.properties) if r.properties else None)

    def stopped() -> bool:
        return stop_check is not None and stop_check()

    fd, geojsonl_path = tempfile.mkstemp(suffix=".geojsonl")
    tippecanoe_started = False
    try:
        with os.fdopen(fd, "wb") as f:
            queue: Queue = Queue(maxsize=_QUEUE_MAX_SIZE)
            consumer_thread = threading.Thread(target=_consumer, args=(queue, f))
            consumer_thread.start()

            total_features = 0
            export_cancelled = False
            with SessionLocal() as session:
                result = session.execute(
                    text(
                        "SELECT id, ST_AsGeoJSON(ST_Union(geometry))::text AS geometry, "
                        "(array_agg(properties ORDER BY part_index))[1] AS properties "
                        "FROM features WHERE collection_id = :cid GROUP BY id ORDER BY id"
                    ),
                    {"cid": collection_id},
                    execution_options={"stream_results": True},
                )
                for partition in result.partitions(_EXPORT_CHUNK_SIZE):
                    if stopped():
                        export_cancelled = True
                        break
                    chunk = [row_data(r) for r in partition]
                    queue.put(chunk)
                    total_features += len(chunk)
            queue.put(None)
            queue.join()
            consumer_thread.join()
            if export_cancelled:
                return BUILD_CANCELLED

        # Build into a temp file, then atomically replace the live MBTiles so clients never read a partial file.
        out_path_tmp = os.path.join(tiles_dir, f"{collection_id}.mbtiles.{uuid.uuid4().hex}.tmp")

        # Use sanitized layer name so it matches TileJSON vector_layers.id and frontend source-layer.
        layer_name = mvt_layer_name(collection_id)
        # -L requires "layername:file" (single argument per layer)
        # Optional: -r1 (no point dropping at low zooms), -ps/-pS/-pn/-pt (simplification). Defaults: -r1 and -ps on.
        cmd = [
            "tippecanoe",
            "--read-parallel",
            "-o", out_path_tmp,
            "-L", f"{layer_name}:{geojsonl_path}",
            f"--layer={layer_name}",
            f"-z{maxz}",
            f"-Z{minz}",
            "--force",
            "--detect-shared-borders",
            "--full-detail=12",
            "--low-detail=10",
            "--minimum-detail=8",
        ]
        if opts.no_point_dropping:
            cmd.append("-r1")
        if opts.no_line_simplification:
            cmd.append("-ps")
        if opts.simplify_only_low_zooms:
            cmd.append("-pS")
        if opts.no_shared_node_simplification:
            cmd.append("-pn")
        if opts.no_tiny_polygon_reduction:
            cmd.append("-pt")
        # Densest: drop (default) or coalesce
        if opts.densest == "coalesce":
            cmd.append("--coalesce-densest-as-needed")
        else:
            cmd.append("--drop-densest-as-needed")
        # Smallest: drop (default) or coalesce
        if opts.smallest == "coalesce":
            cmd.append("--coalesce-smallest-as-needed")
        else:
            cmd.append("--drop-smallest-as-needed")
        # Attribute filter: --include=attr (only these) or -x attr (exclude).
        # Always include "id" when using include_attributes so popup links use the real feature id (UUID), not tippecanoe's numeric index.
        if opts.include_attributes:
            include_set = {a for a in opts.include_attributes if a}
            if "id" not in include_set:
                include_set.add("id")
            for attr in sorted(include_set):
                cmd.append(f"--include={attr}")
        if opts.exclude_attributes:
            for attr in opts.exclude_attributes:
                if attr and attr != "id":
                    cmd.extend(["-x", attr])
            # Never exclude "id" so popup links use the real feature id (UUID)
        print(f"[tile_builder] Running tippecanoe for {collection_id} ({total_features} features)...", file=sys.stderr, flush=True)
        tippecanoe_started = True
        stderr_lines: list[str] = []
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_thread = threading.Thread(target=_stream_pipe, args=(proc.stdout, sys.stdout), daemon=True)
        stderr_thread = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, sys.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        while proc.poll() is None:
            time.sleep(1)
            if stopped():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if out_path_tmp and os.path.exists(out_path_tmp):
                    try:
                        os.unlink(out_path_tmp)
                    except OSError:
                        pass
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                return BUILD_CANCELLED
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if proc.returncode != 0:
            err_text = "".join(stderr_lines).strip()
            if out_path_tmp and os.path.exists(out_path_tmp):
                try:
                    os.unlink(out_path_tmp)
                except OSError:
                    pass
            if err_text:
                return f"tippecanoe failed: {err_text[-4000:]}"
            return f"tippecanoe failed with exit code {proc.returncode}"
        # Atomic install: readers keep using the previous file until this succeeds.
        try:
            os.replace(out_path_tmp, out_path_final)
        except OSError as e:
            print(f"[tile_builder] Failed to install MBTiles: {e}", file=sys.stderr, flush=True)
            if out_path_tmp and os.path.exists(out_path_tmp):
                try:
                    os.unlink(out_path_tmp)
                except OSError:
                    pass
            return f"failed to install tiles: {e}"
        out_path_tmp = None  # installed; do not delete in finally
    finally:
        try:
            os.unlink(geojsonl_path)
        except OSError:
            pass
        if out_path_tmp and os.path.exists(out_path_tmp):
            try:
                os.unlink(out_path_tmp)
            except OSError:
                pass

    # Remove previous file if DB pointed elsewhere (e.g. legacy path); live path is out_path_final.
    tiles_revision = compute_collection_tiles_revision(collection_id, out_path_final)
    with SessionLocal() as session:
        old = session.execute(
            text("SELECT pmtiles_path FROM collection_tiles WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        old_path = (old[0] if old else None)
        if old_path and old_path != out_path_final and os.path.exists(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        session.execute(
            text("""
                INSERT INTO collection_tiles (collection_id, pmtiles_path, built_at, features_updated_at, minzoom, maxzoom, tiles_revision)
                VALUES (:cid, :path, :now, :fua, :minz, :maxz, :rev)
                ON CONFLICT (collection_id) DO UPDATE SET
                    pmtiles_path = EXCLUDED.pmtiles_path,
                    built_at = EXCLUDED.built_at,
                    features_updated_at = EXCLUDED.features_updated_at,
                    minzoom = EXCLUDED.minzoom,
                    maxzoom = EXCLUDED.maxzoom,
                    tiles_revision = EXCLUDED.tiles_revision
            """),
            {
                "cid": collection_id,
                "path": out_path_final,
                "now": datetime.now(timezone.utc),
                "fua": max_updated,
                "minz": minz,
                "maxz": maxz,
                "rev": tiles_revision,
            },
        )
        session.commit()

    engine.dispose()
    return None
