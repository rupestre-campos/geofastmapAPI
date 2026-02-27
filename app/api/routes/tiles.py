"""OGC API Tiles: static PMTiles build + serve (ZXY and file), TileJSON with dynamic tiles from PostGIS (MVT)."""
from __future__ import annotations

import asyncio
import gzip
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.dynamic_tile_cache import get_tile as get_cached_tile, set_tile as set_cached_tile
from app.services.tile_build_queue import (
    clear_pending,
    create_tile_build_job,
    enqueue_tile_build,
    get_latest_tile_build_job,
    get_pending_job_id,
    update_tile_build_job,
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
    request: Request,
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
    base = _base_url(request)
    # Dedup: if a build is already queued or building, return that job_id (unless stuck)
    pending_job_id = get_pending_job_id(collection_id)
    if pending_job_id:
        from datetime import datetime, timezone
        job = get_latest_tile_build_job(collection_id)
        # If job stuck in "pending" or "running" for >30 min, allow re-queue (worker may have died)
        if job and job.updated_at and job.status in ("pending", "running"):
            age_seconds = (datetime.now(timezone.utc) - job.updated_at).total_seconds()
            if age_seconds > 30 * 60:  # 30 minutes
                clear_pending(collection_id)
            else:
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "message": "Tile build already queued or in progress.",
                        "collection_id": collection_id,
                        "job_id": pending_job_id,
                        "status_url": f"{base}/jobs/{pending_job_id}",
                    },
                )
        else:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "message": "Tile build already queued or in progress.",
                    "collection_id": collection_id,
                    "job_id": pending_job_id,
                    "status_url": f"{base}/jobs/{pending_job_id}",
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
                "status_url": f"{base}/jobs/{existing or job.job_id}",
            },
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "Tile build queued.",
            "collection_id": collection_id,
            "job_id": job.job_id,
            "status_url": f"{base}/jobs/{job.job_id}",
        },
    )


@router.post(
    "/{collection_id}/tiles/build/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel / reset tile build",
    description="Clears the current build state so you can trigger a new build. Use when a build is stuck (queued or building).",
)
async def cancel_tile_build(
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
    job = get_latest_tile_build_job(collection_id)
    clear_pending(collection_id)
    if job and job.status in ("pending", "running"):
        update_tile_build_job(job.job_id, status="cancelled", message="Cancelled by user")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": "Tile build cancelled. You can request a new build with POST /collections/{collection_id}/tiles/build.",
            "collection_id": collection_id,
        },
    )


@router.delete(
    "/{collection_id}/tiles/static",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete static PMTiles",
    description="Remove the built PMTiles file from disk (if it exists) and clear the static tiles record. TileJSON will then only show dynamic tiles until you run POST .../tiles/build again.",
)
async def delete_tiles_static(
    collection_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if rec and rec.pmtiles_path:
        path = Path(rec.pmtiles_path)
        if path.is_file():
            try:
                os.unlink(path)
            except OSError:
                pass
    await tiles_crud.clear_static_tiles(db, collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    has_static = bool(rec and rec.pmtiles_path and Path(rec.pmtiles_path).exists())
    # Prefer static ZXY URL when PMTiles exists so clients can use a single tile endpoint
    tile_urls = [f"{base}/collections/{collection_id}/tiles/dynamic/{{z}}/{{x}}/{{y}}.pbf"]
    if has_static:
        tile_urls.insert(0, f"{base}/collections/{collection_id}/tiles/static/{{z}}/{{x}}/{{y}}.pbf")
    tilejson = {
        "tilejson": "2.2.0",
        "name": collection_id,
        "description": collection.description or "",
        "version": "1.0.0",
        "scheme": "xyz",
        "tiles": tile_urls,
        "vector_layers": [{"id": collection_id, "description": "", "minzoom": 0, "maxzoom": 14}],
    }
    return JSONResponse(content=tilejson)


def _mvt_layer_name(collection_id: str) -> str:
    """Safe MVT layer name: alphanumeric and underscore only."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)
    return safe if safe else "default"


# Max property keys to expose as separate MVT attributes (avoids huge dynamic SQL)
_MVT_MAX_PROPERTY_KEYS = 200


def _pg_quote_identifier(name: str) -> str:
    """Escape a string for use as PostgreSQL double-quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


async def _get_property_keys(db: AsyncSession, collection_id: str) -> list[str]:
    """Return distinct top-level keys from features.properties for this collection (ordered, limited)."""
    r = await db.execute(
        text("""
        SELECT DISTINCT key
        FROM features, jsonb_object_keys(properties) AS key
        WHERE collection_id = :cid
        ORDER BY 1
        LIMIT :limit
        """),
        {"cid": collection_id, "limit": _MVT_MAX_PROPERTY_KEYS},
    )
    return [row[0] for row in r.fetchall()]


def _mvt_property_select_fragment(keys: list[str]) -> str:
    """Build SQL fragment: (properties ->> 'k1') AS "k1", (properties ->> 'k2') AS "k2", ..."""
    if not keys:
        return ""
    parts = []
    for k in keys:
        # Key as literal for ->> ; alias as quoted identifier (safe for MVT tag names)
        alias = _pg_quote_identifier(k)
        parts.append(f"(properties ->> {repr(k)}) AS {alias}")
    return ", ".join(parts)


@router.get(
    "/{collection_id}/tiles/dynamic/{z:int}/{x:int}/{y:int}.pbf",
    summary="Dynamic vector tile (MVT from PostGIS)",
    description="Returns Mapbox Vector Tile for this collection. Property keys are exposed as separate MVT attributes (not a single 'properties' JSON).",
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
    cache_headers = {"Cache-Control": "public, max-age=60"}

    # Short-lived Redis cache for dynamic tiles
    cached = get_cached_tile(collection_id, z, x, y)
    if cached is not None:
        return Response(
            content=cached,
            media_type="application/x-protobuf",
            headers=cache_headers,
        )

    layer_name = _mvt_layer_name(collection_id)
    max_features = settings.tiles_mvt_max_features

    # Flatten properties: each JSON key becomes a separate MVT attribute
    property_keys = await _get_property_keys(db, collection_id)
    prop_cols = _mvt_property_select_fragment(property_keys)

    # PostGIS: MVT from features; id + each property key as column (no single 'properties' blob)
    prop_select = f", {prop_cols}" if prop_cols else ""
    sql = f"""
        SELECT ST_AsMVT(tile, :layer_name, 4096, 'geom') AS mvt
        FROM (
            SELECT
                id{prop_select},
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
    """
    result = await db.execute(
        text(sql),
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
    payload = bytes(mvt) if mvt else b""
    set_cached_tile(collection_id, z, x, y, payload)
    return Response(
        content=payload,
        media_type="application/x-protobuf",
        headers=cache_headers,
    )


def _read_tile_from_pmtiles(path: Path, z: int, x: int, y: int) -> bytes | None:
    """Read a single tile from a PMTiles file. Sync, run in thread. Returns decompressed tile bytes or None."""
    from pmtiles.reader import Reader
    from pmtiles.reader import MmapSource
    from pmtiles.tile import Compression
    with open(path, "rb") as f:
        get_bytes = MmapSource(f)
        reader = Reader(get_bytes)
        header = reader.header()
        raw = reader.get(z, x, y)
    if raw is None:
        return None
    if header.get("tile_compression") == Compression.GZIP:
        raw = gzip.decompress(raw)
    return raw


@router.get(
    "/{collection_id}/tiles/static/{z:int}/{x:int}/{y:int}.pbf",
    summary="Static vector tile (Z/X/Y from PMTiles)",
    description="Returns the tile at z/x/y from the built PMTiles file for this collection.",
)
async def get_tiles_static_zxy(
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
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if not rec or not rec.pmtiles_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiles not built yet. POST to /tiles/build first.")
    path = Path(rec.pmtiles_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tiles file missing.")
    tile_bytes = await asyncio.to_thread(_read_tile_from_pmtiles, path, z, x, y)
    if tile_bytes is None:
        return Response(
            content=b"",
            media_type="application/x-protobuf",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return Response(
        content=tile_bytes,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=3600"},
    )


