"""Worker-side storage deletes: free disk first, then fast DB cleanup (DROP partition)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.crud import raster_views as raster_views_crud
from app.db.features_partitions import drop_collection_features_data_sync
from app.db.session import AsyncSessionLocal
from app.services import storage_usage as su
from app.services.dynamic_tile_cache import invalidate_collection_cache
from app.services.job_store import update_job
from app.services.static_tiles_path import default_mbtiles_path
from app.services.storage_delete_queue import StorageDeletePayload


def _unlink_mbtiles(collection_id: str, pmtiles_path: str | None) -> None:
    paths: list[Path] = []
    if pmtiles_path:
        paths.append(Path(pmtiles_path))
    paths.append(default_mbtiles_path(collection_id))
    seen: set[str] = set()
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


async def _clear_collection_tiles_async(collection_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await tiles_crud.clear_static_tiles(db, collection_id)


async def _delete_collection_async(collection_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        return await collections_crud.delete_collection(db, collection_id)


async def _delete_mosaic_async(view_id: str, json_relative_path: str | None) -> bool:
    async with AsyncSessionLocal() as db:
        return await raster_views_crud.delete_view(db, view_id)


def delete_collection_tiles_disk_sync(collection_id: str) -> None:
    """Remove MBTiles files only (fast disk reclaim for tiles-only delete)."""
    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    pmtiles_path: str | None = None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT pmtiles_path FROM collection_tiles WHERE collection_id = :cid"),
                {"cid": collection_id},
            ).first()
            if row and row[0]:
                pmtiles_path = str(row[0])
    finally:
        engine.dispose()
    _unlink_mbtiles(collection_id, pmtiles_path)
    invalidate_collection_cache(collection_id)


def delete_collection_disk_sync(collection_id: str) -> None:
    """Remove MBTiles files and raster directory (before DB delete)."""
    delete_collection_tiles_disk_sync(collection_id)
    su.delete_collection_raster_dir(collection_id)


def _drop_features_partition_sync(collection_id: str) -> str | None:
    settings = get_settings()
    engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
    try:
        return drop_collection_features_data_sync(engine, collection_id)
    finally:
        engine.dispose()


def process_storage_delete(payload: StorageDeletePayload) -> None:
    action = payload.action
    target = payload.target_id
    job_id = payload.job_id

    update_job(job_id, status="running", message="Starting…")
    try:
        if action == "delete_tiles":
            update_job(job_id, message="Removing tiles from disk…")
            delete_collection_tiles_disk_sync(target)
            asyncio.run(_clear_collection_tiles_async(target))
            su.patch_row_tiles_cleared(target)
            update_job(job_id, status="completed", message=f"Tiles removed for {target}.")
            return

        if action == "delete_collection":
            update_job(job_id, message="Removing tiles and rasters from disk…")
            delete_collection_disk_sync(target)
            su.patch_row_tiles_and_rasters_cleared(target)
            update_job(
                job_id,
                message="Disk freed; dropping features partition (DETACH + DROP TABLE)…",
            )
            dropped = _drop_features_partition_sync(target)
            update_job(
                job_id,
                message=(
                    f"Partition dropped ({dropped or 'none'}); removing collection metadata…"
                ),
            )
            # Features already gone; delete_collection will no-op the drop and remove the row.
            ok = asyncio.run(_delete_collection_async(target))
            if not ok:
                update_job(
                    job_id,
                    status="failed",
                    message=f"Collection {target} not found in database.",
                )
                return
            su.remove_row_from_snapshot("collection", target)
            update_job(
                job_id,
                status="completed",
                message=f"Collection {target} deleted (disk + DROP partition + metadata).",
            )
            return

        if action == "delete_mosaic":
            update_job(job_id, message="Removing mosaic file from disk…")
            su.delete_mosaic_files(payload.mosaic_json_path)
            su.patch_mosaic_file_cleared(target)
            update_job(job_id, message="Removing mosaic database record…")
            ok = asyncio.run(_delete_mosaic_async(target, payload.mosaic_json_path))
            if not ok:
                update_job(
                    job_id,
                    status="failed",
                    message=f"Mosaic {target} not found in database.",
                )
                return
            su.remove_row_from_snapshot("mosaic", target)
            update_job(job_id, status="completed", message=f"Mosaic {target} deleted.")
            return

        if action == "delete_orphan":
            kind = payload.orphan_kind or ""
            update_job(job_id, message=f"Removing orphan {kind}…")
            ok = False
            if kind == "orphan_tiles":
                ok = su.delete_orphan_tiles_file(target)
            elif kind == "orphan_rasters":
                ok = su.delete_orphan_raster_dir(target)
            elif kind == "orphan_mosaic":
                ok = su.delete_orphan_mosaic_file(target)
            if not ok:
                update_job(
                    job_id,
                    status="failed",
                    message=f"Could not delete orphan {target}.",
                )
                return
            su.remove_row_from_snapshot(kind, target)
            update_job(job_id, status="completed", message=f"Orphan {target} deleted.")
            return

        update_job(job_id, status="failed", message=f"Unknown storage delete action: {action}")
    except Exception as e:
        update_job(
            job_id,
            status="failed",
            message=f"Storage delete failed: {type(e).__name__}: {e}",
        )
        raise
