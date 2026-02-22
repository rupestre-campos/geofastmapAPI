"""Build PMTiles for a collection: export GeoJSONSeq, run tippecanoe, save and register."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.utils.geo import geometry_to_geojson


def build_pmtiles_sync(collection_id: str) -> str | None:
    """
    Export collection to GeoJSONSeq, run tippecanoe, save to tiles_storage_path.
    Returns error message or None on success.
    Uses sync DB; run in thread/worker process.
    """
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
        # Get max updated_at for this collection
        row = session.execute(
            text("SELECT MAX(updated_at) AS m FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        ).first()
        max_updated = row.m if row and row.m else None

        # Stream features as GeoJSONSeq
        result = session.execute(
            text("SELECT id, geometry, properties FROM features WHERE collection_id = :cid"),
            {"cid": collection_id},
        )
        rows = result.fetchall()

    if not rows:
        # No features: remove old pmtiles if any, clear record
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except OSError:
            pass
        from datetime import datetime, timezone
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

    fd, geojsonl_path = tempfile.mkstemp(suffix=".geojsonl")
    try:
        with os.fdopen(fd, "w") as f:
            for row in rows:
                geom = geometry_to_geojson(row.geometry)
                feat = {
                    "type": "Feature",
                    "id": row.id,
                    "geometry": geom,
                    "properties": dict(row.properties) if row.properties else {},
                }
                f.write(json.dumps(feat, ensure_ascii=False) + "\n")

        cmd = [
            "tippecanoe",
            "-o", out_path,
            "-L", collection_id,
            "-z", str(maxz),
            "-Z", str(minz),
            "--force",
            geojsonl_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            return proc.stderr or proc.stdout or "tippecanoe failed"
    finally:
        try:
            os.unlink(geojsonl_path)
        except OSError:
            pass

    # Delete old file if it was at a different path; then upsert new path
    from datetime import datetime, timezone
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
