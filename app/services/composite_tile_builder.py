"""Build a single MBTiles file for a composite collection (all members merged via tippecanoe)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from queue import Queue
from typing import Callable

import orjson

from app.core.config import get_settings
from app.services.collection_tiles_revision import compute_collection_tiles_revision
from app.services.composite_items import format_composite_item_id
from app.services.tile_builder import BUILD_CANCELLED, _EXPORT_CHUNK_SIZE, _QUEUE_MAX_SIZE, _stream_pipe
from app.utils.geo import mvt_layer_name


def _composite_consumer(queue: Queue, file_handle, member_id: str) -> None:
    """GeoJSONSeq writer that tags each feature with composite item id and member metadata."""
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
            if "id" in props_dict and props_dict.get("id") != fid:
                key = "id_source"
                while key in props_dict:
                    key = key + "_1"
                props_dict[key] = props_dict.get("id")
            comp_id = format_composite_item_id(member_id, str(fid))
            props_dict["id"] = comp_id
            props_dict["_member_collection_id"] = member_id
            props_dict["_member_feature_id"] = str(fid)
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


def build_composite_pmtiles_sync(
    composite_id: str,
    member_ids: list[str],
    options=None,
    stop_check: Callable[[], bool] | None = None,
) -> str | None:
    """
    Export all member collections into one GeoJSONSeq, run tippecanoe once, register on composite_id.
    Returns error message, None on success, or BUILD_CANCELLED.
    """
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.services.tile_build_queue import TileBuildOptions

    if not member_ids:
        return "Composite has no members"

    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    tiles_dir = settings.tiles_storage_path
    os.makedirs(tiles_dir, exist_ok=True)
    out_path_final = os.path.join(tiles_dir, f"{composite_id}.mbtiles")
    out_path_tmp: str | None = None
    opts = options or TileBuildOptions()
    minz = opts.min_zoom if opts.min_zoom is not None else settings.tippecanoe_minzoom
    maxz = opts.max_zoom if opts.max_zoom is not None else settings.tippecanoe_maxzoom

    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT MAX(features_last_updated_at) AS m FROM collections WHERE id = ANY(:ids)"
            ),
            {"ids": member_ids},
        ).first()
        max_updated = row.m if row and row.m else None
        count_row = session.execute(
            text("SELECT COUNT(DISTINCT id) AS n FROM features WHERE collection_id = ANY(:ids)"),
            {"ids": member_ids},
        ).first()
        feature_count = count_row.n if count_row and count_row.n else 0

    if feature_count == 0:
        if os.path.exists(out_path_final):
            engine.dispose()
            return "No features in member collections (existing tiles kept)"
        engine.dispose()
        return "No features in member collections"

    def row_data(r):
        return (r.id, r.geometry, dict(r.properties) if r.properties else None)

    def stopped() -> bool:
        return stop_check is not None and stop_check()

    fd, geojsonl_path = tempfile.mkstemp(suffix=".geojsonl")
    tippecanoe_started = False
    try:
        with os.fdopen(fd, "wb") as f:
            total_features = 0
            export_cancelled = False
            for member_id in member_ids:
                if stopped():
                    export_cancelled = True
                    break
                queue: Queue = Queue(maxsize=_QUEUE_MAX_SIZE)
                consumer_thread = threading.Thread(
                    target=_composite_consumer, args=(queue, f, member_id)
                )
                consumer_thread.start()
                with SessionLocal() as session:
                    result = session.execute(
                        text(
                            "SELECT id, ST_AsGeoJSON(ST_Union(geometry))::text AS geometry, "
                            "(array_agg(properties ORDER BY part_index))[1] AS properties "
                            "FROM features WHERE collection_id = :cid GROUP BY id ORDER BY id"
                        ),
                        {"cid": member_id},
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
                    break
            if export_cancelled:
                return BUILD_CANCELLED

        out_path_tmp = os.path.join(tiles_dir, f"{composite_id}.mbtiles.{uuid.uuid4().hex}.tmp")
        layer_name = mvt_layer_name(composite_id)
        cmd = [
            "tippecanoe",
            "--read-parallel",
            "-o",
            out_path_tmp,
            "-L",
            f"{layer_name}:{geojsonl_path}",
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
        if opts.densest == "coalesce":
            cmd.append("--coalesce-densest-as-needed")
        else:
            cmd.append("--drop-densest-as-needed")
        if opts.smallest == "coalesce":
            cmd.append("--coalesce-smallest-as-needed")
        else:
            cmd.append("--drop-smallest-as-needed")
        if opts.include_attributes:
            include_set = {a for a in opts.include_attributes if a}
            for key in ("id", "_member_collection_id", "_member_feature_id"):
                include_set.add(key)
            for attr in sorted(include_set):
                cmd.append(f"--include={attr}")
        if opts.exclude_attributes:
            for attr in opts.exclude_attributes:
                if attr and attr not in ("id", "_member_collection_id", "_member_feature_id"):
                    cmd.extend(["-x", attr])
        print(
            f"[composite_tile_builder] Running tippecanoe for {composite_id} "
            f"({total_features} features from {len(member_ids)} members)...",
            file=sys.stderr,
            flush=True,
        )
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
        try:
            os.replace(out_path_tmp, out_path_final)
        except OSError as e:
            if out_path_tmp and os.path.exists(out_path_tmp):
                try:
                    os.unlink(out_path_tmp)
                except OSError:
                    pass
            return f"failed to install tiles: {e}"
        out_path_tmp = None
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

    tiles_revision = compute_collection_tiles_revision(composite_id, out_path_final)
    with SessionLocal() as session:
        old = session.execute(
            text("SELECT pmtiles_path FROM collection_tiles WHERE collection_id = :cid"),
            {"cid": composite_id},
        ).first()
        old_path = old[0] if old else None
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
                "cid": composite_id,
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
