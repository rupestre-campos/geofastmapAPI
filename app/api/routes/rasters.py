"""Raster upload: GeoTIFF → COG + feature."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.api.deps import get_current_user_required
from app.models.user import User
from app.core.config import get_settings
from app.core.permissions import can_edit_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.db.session import get_db
from app.schemas.feature import FeatureCreate, FeatureGeoJSON, Geometry
from app.schemas.ogc import Link
from app.services.coverages import cog_path_for, convert_geotiff_to_cog_4326

router = APIRouter()

_ALLOWED_SUFFIX = frozenset({".tif", ".tiff", ".geotiff"})


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.post(
    "/{collection_id}/rasters",
    summary="Upload a GeoTIFF as Cloud Optimized GeoTIFF (EPSG:4326)",
    description="Converts to COG on disk and creates one feature with geometry from the raster footprint.",
)
async def upload_raster(
    request: Request,
    collection_id: str,
    file: UploadFile = File(..., description="GeoTIFF in EPSG:4326"),
    title: str | None = Form(None, description="Optional title stored in properties"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    settings = get_settings()
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this collection")

    name = (file.filename or "upload.tif").lower()
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOW_SUFFIX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected .tif / .tiff file, got suffix {suffix!r}",
        )

    root = Path(settings.raster_storage_path)
    root.mkdir(parents=True, exist_ok=True)

    max_b = settings.raster_upload_max_bytes
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    tmp_path_p = Path(tmp_path)
    feature_id = str(uuid7())
    dst = cog_path_for(settings.raster_storage_path, collection_id, feature_id)

    try:
        with open(tmp_path, "wb") as out:
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_b:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File too large (max {max_b} bytes)",
                    )
                out.write(chunk)

        try:
            conv = convert_geotiff_to_cog_4326(tmp_path_p, dst)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        footprint = conv["footprint_geojson"]
        meta = conv["meta"]
        raster_props = {
            "cog_path": conv["cog_path"],
            "meta": meta,
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
        feature = await features_crud.create_feature_with_id(db, data, feature_id)
    except HTTPException:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        try:
            tmp_path_p.unlink(missing_ok=True)
        except OSError:
            pass

    base = _base_url(request)
    from app.utils.geo import geometry_to_geojson

    geom_dict = geometry_to_geojson(feature.geometry) if feature.geometry is not None else None

    fr = FeatureGeoJSON(
        id=feature.id,
        type="Feature",
        geometry=Geometry(**geom_dict) if geom_dict else None,
        properties=feature.properties,
        links=[
            Link(href=f"{base}/collections/{collection_id}/items/{feature.id}", rel="self", type="application/geo+json"),
            Link(href=f"{base}/collections/{collection_id}/coverages/{feature.id}", rel="related", type="image/tiff"),
        ],
    )
    return fr
