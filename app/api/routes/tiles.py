"""OGC API Tiles: static PMTiles build + serve, TileJSON with dynamic tiles from PostGIS (MVT)."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.tile_build_queue import (
    create_tile_build_job,
    enqueue_tile_build,
    get_latest_tile_build_job,
    get_pending_job_id,
)

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.post(
    "/{collection_id}/tiles/build",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request static PMTiles build",
    description="Enqueue build of PMTiles for this collection. Returns job_id to poll status. One build per collection at a time; duplicate requests return existing job.",
)
async def build_tiles(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tile build requires Redis (BULK_QUEUE_TYPE=redis)",
        )
    # Dedup: if a build is already queued or building, return that job_id
    pending_job_id = get_pending_job_id(collection_id)
    if pending_job_id:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Tile build already queued or in progress.",
                "collection_id": collection_id,
                "job_id": pending_job_id,
            },
        )
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    max_updated = await tiles_crud.get_max_feature_updated_at(db, collection_id)
    need_build = False
    if max_updated is None and rec and rec.built_at:
        need_build = True  # clear previous build
    elif max_updated is not None:
        if rec is None or rec.features_updated_at is None or max_updated > rec.features_updated_at:
            need_build = True
    if not need_build:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "message": "No build needed; tiles are up to date.",
                "collection_id": collection_id,
            },
        )
    job = create_tile_build_job(collection_id)
    enqueued = enqueue_tile_build(collection_id, job.job_id)
    if not enqueued:
        # Race: another request set pending; return that job
        existing = get_pending_job_id(collection_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Tile build queued.",
                "collection_id": collection_id,
                "job_id": existing or job.job_id,
            },
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "Tile build queued.",
            "collection_id": collection_id,
            "job_id": job.job_id,
        },
    )


@router.get(
    "/{collection_id}/tiles/build/status",
    summary="Tile build job status",
    description="Returns the latest tile build job for this collection (queued, building, completed, failed).",
)
async def get_tile_build_status(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tile build status requires Redis (BULK_QUEUE_TYPE=redis)",
        )
    job = get_latest_tile_build_job(collection_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No tile build job found for this collection.")
    return JSONResponse(content=job.to_dict())


@router.get(
    "/{collection_id}/tiles",
    summary="TileJSON for this collection",
    description="Returns TileJSON with links to static PMTiles (if built) and dynamic vector tiles from the database.",
)
async def get_tiles_tilejson(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    base = _base_url(request)
    settings = get_settings()
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    tilejson = {
        "tilejson": "2.2.0",
        "name": collection_id,
        "description": collection.description or "",
        "version": "1.0.0",
        "scheme": "xyz",
        "tiles": [f"{base}/collections/{collection_id}/tiles/dynamic/{{z}}/{{x}}/{{y}}.pbf"],
        "vector_layers": [{"id": collection_id, "description": "", "minzoom": 0, "maxzoom": 14}],
    }
    if rec and rec.pmtiles_path and Path(rec.pmtiles_path).exists():
        tilejson["pmtiles_url"] = f"{base}/collections/{collection_id}/tiles/pmtiles"
    return JSONResponse(content=tilejson)


def _mvt_layer_name(collection_id: str) -> str:
    """Safe MVT layer name: alphanumeric and underscore only."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)
    return safe if safe else "default"


@router.get(
    "/{collection_id}/tiles/dynamic/{z:int}/{x:int}/{y:int}.pbf",
    summary="Dynamic vector tile (MVT from PostGIS)",
    description="Returns Mapbox Vector Tile for this collection by querying the features table filtered by collection_id.",
)
async def get_tiles_dynamic(
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
):
    if z < 0 or z > 22:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid z")
    if x < 0 or x >= (1 << z) or y < 0 or y >= (1 << z):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid x or y for zoom")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    settings = get_settings()
    layer_name = _mvt_layer_name(collection_id)
    max_features = settings.tiles_mvt_max_features
    # PostGIS: MVT from features filtered by collection_id, tile envelope in 3857; limit rows to avoid overload
    result = await db.execute(
        text("""
        SELECT ST_AsMVT(tile, :layer_name, 4096, 'geom') AS mvt
        FROM (
            SELECT
                id,
                properties::text AS properties,
                ST_AsMVTGeom(
                    ST_Transform(ST_CurveToLine(geometry::geometry), 3857),
                    ST_TileEnvelope(:z, :x, :y),
                    4096,
                    256,
                    true
                ) AS geom
            FROM features
            WHERE collection_id = :cid
              AND geometry IS NOT NULL
              AND ST_Intersects(
                  geometry,
                  ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326)
              )
            LIMIT :max_features
        ) AS tile
        WHERE tile.geom IS NOT NULL
        """),
        {
            "layer_name": layer_name,
            "z": z,
            "x": x,
            "y": y,
            "cid": collection_id,
            "max_features": max_features,
        },
    )
    row = result.first()
    mvt = row.mvt if row and row.mvt else None
    if not mvt:
        return Response(
            content=b"",
            media_type="application/x-protobuf",
            headers={"Cache-Control": "public, max-age=60"},
        )
    return Response(
        content=bytes(mvt),
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get(
    "/{collection_id}/tiles/pmtiles",
    summary="Serve static PMTiles file",
    description="Returns the built PMTiles file for this collection, or 404 if not built.",
)
async def get_tiles_pmtiles(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
):
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if not rec or not rec.pmtiles_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiles not built yet. POST to /tiles/build first.")
    path = Path(rec.pmtiles_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiles file missing.")
    return FileResponse(path, media_type="application/vnd.pmtiles", filename=f"{collection_id}.pmtiles")
