"""Raster bulk ingest: staging archive on bulk storage + background COG/feature creation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker
from uuid6 import uuid7

from app.core.config import get_settings
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import create_app_async_engine
from app.schemas.feature import FeatureCreate, Geometry
from app.services.coverages import cog_path_for, convert_geotiff_to_cog_4326
from app.services.job_store import update_job

_ALLOWED_SUFFIX = frozenset({".tif", ".tiff", ".geotiff"})
_DEM_ENCODINGS = frozenset({"terrainrgb", "terrarium"})
MANIFEST_VERSION = 1


def _normalize_dem_encoding(value: str | None) -> str:
    enc = (value or "terrainrgb").strip().lower()
    return enc if enc in _DEM_ENCODINGS else "terrainrgb"


def _collection_dem_settings(collection) -> tuple[bool, str]:
    rs = getattr(collection, "raster_settings", None) if collection is not None else None
    if not isinstance(rs, dict):
        return (False, "terrainrgb")
    return (bool(rs.get("is_dem", False)), _normalize_dem_encoding(rs.get("dem_encoding")))


def zip_tiff_members(zip_path: Path) -> list[str]:
    members: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            suffix = Path(info.filename).suffix.lower()
            if suffix in _ALLOWED_SUFFIX:
                members.append(info.filename)
    return members


def vsizip_member_path(zip_path: Path, member_name: str) -> str:
    safe_member = member_name.lstrip("/")
    return f"/vsizip/{zip_path.as_posix()}/{safe_member}"


def _safe_staged_name(original: str, index: int) -> str:
    base = os.path.basename(original) or f"file_{index}"
    base = base.replace("\\", "_").replace("/", "_")
    if ".." in base or base.startswith("."):
        base = f"file_{index}_{base.lstrip('.')}"
    return f"{index:04d}_{base}"


class RasterBatchUploadTooLargeError(Exception):
    """Raised when a single uploaded file exceeds configured max size."""


async def _save_upload_to_temp(file: UploadFile, *, suffix: str) -> Path:
    settings = get_settings()
    max_b = settings.raster_upload_max_bytes
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    out_path = Path(tmp_path)
    with open(out_path, "wb") as out:
        total = 0
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_b:
                raise RasterBatchUploadTooLargeError(f"File too large (max {max_b} bytes)")
            out.write(chunk)
    return out_path


async def write_raster_batch_archive(
    *,
    files: list[UploadFile],
    dest_path: str,
    is_dem: bool,
    dem_encoding: str | None,
    source_crs: str | None,
) -> tuple[int, list[dict]]:
    """Stream uploads into a zip at dest_path with manifest.json. Returns (entry_count, entries)."""
    entries: list[dict] = []
    with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, uf in enumerate(files):
            raw_name = uf.filename or f"upload_{i}"
            staged = _safe_staged_name(raw_name, i)
            suffix = Path(staged).suffix.lower()
            tmp: Path | None = None
            try:
                tmp = await _save_upload_to_temp(uf, suffix=suffix if suffix else ".bin")
                arcname = f"files/{staged}"
                zf.write(tmp, arcname=arcname)
                lower = staged.lower()
                if lower.endswith(".zip"):
                    entries.append({"kind": "zip", "name": arcname})
                elif suffix in _ALLOWED_SUFFIX:
                    entries.append({"kind": "geotiff", "name": arcname, "title": Path(staged).stem})
                else:
                    raise ValueError(f"Unsupported raster batch input {suffix!r}; expected TIFF or ZIP.")
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
        manifest = {
            "version": MANIFEST_VERSION,
            "is_dem": bool(is_dem),
            "dem_encoding": _normalize_dem_encoding(dem_encoding),
            "source_crs": source_crs,
            "entries": entries,
        }
        zf.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
    return len(entries), entries


async def create_raster_feature_from_source(
    db,
    *,
    collection_id: str,
    source_path: str | Path,
    feature_id: str,
    title: str | None,
    is_dem: bool = False,
    dem_encoding: str | None = None,
    source_crs: str | None = None,
):
    """Convert GeoTIFF to COG, insert feature. Commits internally (same as API route)."""
    settings = get_settings()
    dst = cog_path_for(settings.raster_storage_path, collection_id, feature_id)
    conv = convert_geotiff_to_cog_4326(source_path, dst, source_crs=source_crs)
    if not dst.is_file():
        raise RuntimeError(f"COG conversion did not produce output file at {dst}")

    footprint = conv["footprint_geojson"]
    meta = conv["meta"]
    raster_props = {
        "cog_path": conv["cog_path"],
        "meta": meta,
        "is_dem": bool(is_dem),
        "dem_encoding": _normalize_dem_encoding(dem_encoding),
    }
    if title:
        raster_props["title"] = title
    props: dict = {"raster": raster_props}
    if title:
        props["title"] = title

    data = FeatureCreate(
        collection_id=collection_id,
        geometry=Geometry(**footprint),
        properties=props,
    )
    try:
        return await features_crud.create_feature_with_id(db, data, feature_id)
    except Exception:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _raise_if_raster_job_cancelled(job_id: str) -> None:
    from app.services.bulk_import import BulkImportCancelled
    from app.services.job_store import get_job

    j = get_job(job_id)
    if j is not None and j.status == "cancelled":
        raise BulkImportCancelled()


async def _process_raster_batch_async(
    *,
    job_id: str,
    collection_id: str,
    extract_dir: Path,
    manifest: dict,
) -> tuple[int, int, str | None]:
    """Use a dedicated AsyncEngine per run so Redis/thread workers work with asyncio.run().

    The app-wide engine in ``app.db.session`` must not be shared across threads or across
    successive ``asyncio.run()`` calls: asyncpg connections are bound to the event loop
    that created them (\"Future attached to a different loop\").
    """
    entries = manifest.get("entries") or []
    crs_opt = manifest.get("source_crs")
    if isinstance(crs_opt, str):
        crs_opt = crs_opt.strip() or None
    is_dem_upload = bool(manifest.get("is_dem", False))
    dem_enc_manifest = manifest.get("dem_encoding")

    created = 0
    failed = 0
    last_err: str | None = None

    settings = get_settings()
    engine = create_app_async_engine(
        pool_size=settings.raster_batch_db_pool_size,
        max_overflow=settings.raster_batch_db_max_overflow,
    )
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with SessionLocal() as db:
            coll = await collections_crud.get_collection(db, collection_id)
            collection_is_dem, collection_dem_enc = _collection_dem_settings(coll)
            is_dem = is_dem_upload or collection_is_dem
            if is_dem_upload:
                dem_enc = _normalize_dem_encoding(dem_enc_manifest)
            elif collection_is_dem:
                dem_enc = collection_dem_enc
            else:
                dem_enc = _normalize_dem_encoding(dem_enc_manifest)

            for ent in entries:
                _raise_if_raster_job_cancelled(job_id)
                kind = str(ent.get("kind") or "")
                name = str(ent.get("name") or "")
                if ".." in name or name.startswith("/"):
                    failed += 1
                    last_err = "Invalid archive path in manifest"
                    continue
                path = extract_dir / name
                if not path.is_file():
                    failed += 1
                    last_err = f"Missing file in archive: {name}"
                    continue
                try:
                    if kind == "geotiff":
                        fid = str(uuid7())
                        title = ent.get("title") if isinstance(ent.get("title"), str) else None
                        if not title:
                            title = path.stem
                        await create_raster_feature_from_source(
                            db,
                            collection_id=collection_id,
                            source_path=path,
                            feature_id=fid,
                            title=title,
                            is_dem=is_dem,
                            dem_encoding=dem_enc,
                            source_crs=crs_opt,
                        )
                        created += 1
                    elif kind == "zip":
                        zpath = path
                        members = zip_tiff_members(zpath)
                        if not members:
                            failed += 1
                            last_err = f"ZIP has no GeoTIFFs: {name}"
                            continue
                        for member in members:
                            _raise_if_raster_job_cancelled(job_id)
                            fid = str(uuid7())
                            title = Path(member).stem
                            vsip = vsizip_member_path(zpath, member)
                            await create_raster_feature_from_source(
                                db,
                                collection_id=collection_id,
                                source_path=vsip,
                                feature_id=fid,
                                title=title,
                                is_dem=is_dem,
                                dem_encoding=dem_enc,
                                source_crs=crs_opt,
                            )
                            created += 1
                    else:
                        failed += 1
                        last_err = f"Unknown manifest entry kind: {kind}"
                except Exception as e:
                    failed += 1
                    last_err = str(e)[:500]
                update_job(job_id, status="running", items_created=created, items_failed=failed)
    finally:
        await engine.dispose()

    return created, failed, last_err


def run_raster_batch_job(*, job_id: str, collection_id: str, archive_path: str) -> None:
    """Sync entry for bulk worker: extract archive, run async ingest."""
    from app.services.bulk_import import BulkImportCancelled
    from app.services.job_store import get_job

    extract_dir = Path(tempfile.mkdtemp(prefix=f"raster_batch_{job_id}_"))
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)
        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Raster batch archive missing manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("version") or 0) != MANIFEST_VERSION:
            raise ValueError("Unsupported raster batch manifest version")

        update_job(job_id, status="running", message="Converting rasters to COG…")
        try:
            created, failed, last_err = asyncio.run(
                _process_raster_batch_async(
                    job_id=job_id,
                    collection_id=collection_id,
                    extract_dir=extract_dir,
                    manifest=manifest,
                )
            )
        except BulkImportCancelled:
            j = get_job(job_id)
            update_job(
                job_id,
                status="cancelled",
                message="Cancelled by user.",
                items_created=j.items_created if j else 0,
                items_failed=j.items_failed if j else 0,
            )
            return

        if failed and not created:
            update_job(
                job_id,
                status="failed",
                message=last_err or "All raster imports failed.",
                items_created=created,
                items_failed=failed,
            )
        elif failed:
            update_job(
                job_id,
                status="completed",
                message=f"Imported {created} raster item(s); {failed} failed."
                + (f" Last error: {last_err}" if last_err else ""),
                items_created=created,
                items_failed=failed,
            )
        else:
            update_job(
                job_id,
                status="completed",
                message=f"Imported {created} raster item(s).",
                items_created=created,
                items_failed=failed,
            )
        if created > 0:
            sync_engine = create_engine(get_settings().database_sync_url, pool_pre_ping=True)
            try:
                collections_crud.recompute_and_update_collection_extent_sync(sync_engine, collection_id)
            finally:
                sync_engine.dispose()
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
