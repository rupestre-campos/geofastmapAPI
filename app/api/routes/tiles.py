"""OGC API Tiles: static MBTiles build + serve (ZXY and file), TileJSON with dynamic tiles from PostGIS (MVT)."""
from __future__ import annotations

import asyncio
import gzip
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.utils.property_filters import PropertyFilter, parse_filter_param
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.db.session import get_db
from app.services.dynamic_tile_cache import (
    get_tile as get_cached_tile,
    set_tile as set_cached_tile,
    get_tile_with_params,
    set_tile_with_params,
    _params_key_from_query,
)
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
    summary="Request static MBTiles build",
    description="Enqueue build of MBTiles for this collection. Returns job_id to poll status. One build per collection at a time; duplicate requests return existing job.",
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
    summary="Delete static tiles",
    description="Remove the built MBTiles file from disk (if it exists) and clear the static tiles record. TileJSON will then only show dynamic tiles until you run POST .../tiles/build again.",
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
    description="Returns TileJSON with links to static MBTiles (if built) and dynamic vector tiles from the database.",
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
    # Prefer static ZXY URL when static tiles (MBTiles) exist so clients can use a single tile endpoint
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


async def _get_property_keys(
    db: AsyncSession,
    collection_id: str,
    feature_ids: list[str] | None = None,
) -> list[str]:
    """Return distinct top-level keys from features.properties.
    When feature_ids is given, only those rows are scanned (fast for single-item/small id list).
    """
    if feature_ids:
        r = await db.execute(
            text("""
            SELECT DISTINCT key
            FROM features, jsonb_object_keys(properties) AS key
            WHERE collection_id = :cid AND id = ANY(:ids)
            ORDER BY 1
            LIMIT :limit
            """),
            {"cid": collection_id, "ids": feature_ids, "limit": _MVT_MAX_PROPERTY_KEYS},
        )
    else:
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


def _safe_json_key(s: str) -> str:
    """Allow only alphanumeric and underscore for JSON key (SQL injection safety)."""
    return "".join(c for c in s if c.isalnum() or c == "_")[:200]


def _order_by_sql(
    sortby: str | None, sortdesc: bool
) -> tuple[str, dict]:
    """Build ORDER BY fragment for dynamic tile (same semantics as GET items). Returns (sql_fragment, params)."""
    if not sortby or sortby == "id":
        return ("id DESC" if sortdesc else "id ASC", {})
    if sortby == "created_at":
        return ("created_at DESC" if sortdesc else "created_at ASC", {})
    key = _safe_json_key(sortby)
    if not key:
        return ("id ASC", {})
    direction = "DESC" if sortdesc else "ASC"
    return (f"(properties ->> :order_key) {direction}", {"order_key": key})


def _build_dynamic_tile_where(
    *,
    bbox_tuple: tuple[float, float, float, float] | None,
    dt_start: datetime | None,
    dt_end: datetime | None,
    feature_ids: list[str] | None,
    structured_filters: list[PropertyFilter] | None,
    fulltext_q: str | None,
) -> tuple[str, dict]:
    """Build extra WHERE conditions and params for dynamic tile query. Returns (sql_fragment, params)."""
    conditions: list[str] = []
    params: dict = {}
    if bbox_tuple is not None:
        minx, miny, maxx, maxy = bbox_tuple
        conditions.append(
            "AND ST_Intersects(geometry, ST_MakeEnvelope(:bbox_minx, :bbox_miny, :bbox_maxx, :bbox_maxy, 4326))"
        )
        params["bbox_minx"] = minx
        params["bbox_miny"] = miny
        params["bbox_maxx"] = maxx
        params["bbox_maxy"] = maxy
    if dt_start is not None:
        conditions.append("AND created_at >= :dt_start")
        params["dt_start"] = dt_start
    if dt_end is not None:
        conditions.append("AND created_at <= :dt_end")
        params["dt_end"] = dt_end
    if feature_ids:
        conditions.append("AND id = ANY(:ids)")
        params["ids"] = feature_ids
    for i, pf in enumerate(structured_filters or []):
        key_safe = _safe_json_key(pf.key)
        if not key_safe:
            continue
        op = pf.op.value
        params[f"fk{i}"] = key_safe
        params[f"fv{i}"] = pf.value
        if op == "eq":
            conditions.append(f"AND (properties ->> :fk{i}) = :fv{i}")
        elif op == "ne":
            conditions.append(f"AND (properties ->> :fk{i}) IS DISTINCT FROM :fv{i}")
        elif op in ("like", "ilike"):
            esc = "ILIKE" if op == "ilike" else "LIKE"
            conditions.append(f"AND (properties ->> :fk{i}) IS NOT NULL AND (properties ->> :fk{i}) {esc} :fv{i} ESCAPE '\\\\'")
        elif op in ("gt", "gte", "lt", "lte"):
            try:
                num_val = float(pf.value)
            except ValueError:
                num_val = 0.0
            params[f"fv{i}_num"] = num_val
            if op == "gt":
                conditions.append(f"AND (properties ->> :fk{i}) IS NOT NULL AND (properties ->> :fk{i})::float > :fv{i}_num")
            elif op == "gte":
                conditions.append(f"AND (properties ->> :fk{i}) IS NOT NULL AND (properties ->> :fk{i})::float >= :fv{i}_num")
            elif op == "lt":
                conditions.append(f"AND (properties ->> :fk{i}) IS NOT NULL AND (properties ->> :fk{i})::float < :fv{i}_num")
            else:  # lte
                conditions.append(f"AND (properties ->> :fk{i}) IS NOT NULL AND (properties ->> :fk{i})::float <= :fv{i}_num")
        else:
            conditions.append(f"AND (properties ->> :fk{i}) = :fv{i}")
    if fulltext_q and fulltext_q.strip():
        q_esc = fulltext_q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{q_esc}%"
        conditions.append("AND properties_flat IS NOT NULL AND properties_flat ILIKE :q_pattern ESCAPE '\\\\'")
        params["q_pattern"] = pattern
    return (" ".join(conditions), params)


@router.get(
    "/{collection_id}/tiles/dynamic/{z:int}/{x:int}/{y:int}.pbf",
    summary="Dynamic vector tile (MVT from PostGIS)",
    description="Returns Mapbox Vector Tile. Only supports ids (comma-separated feature ids) and optional properties. Use for single-item or small id sets; items list map uses GeoJSON from the items query.",
)
async def get_tiles_dynamic(
    request: Request,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    ids: str | None = Query(None, description="Comma-separated feature ids (only these features in tile)."),
    properties: str | None = Query(None, description="Comma-separated property names to include in MVT."),
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

    feature_ids: list[str] | None = None
    if ids:
        feature_ids = [i.strip() for i in ids.split(",") if i.strip()]
    props_include: list[str] | None = None
    if properties:
        props_include = [p.strip() for p in properties.split(",") if p.strip()]

    has_query_params = bool(feature_ids) or bool(props_include)

    if not has_query_params:
        cached = get_cached_tile(collection_id, z, x, y)
        if cached is not None:
            return Response(
                content=cached,
                media_type="application/x-protobuf",
                headers=cache_headers,
            )

    params_key: str | None = None
    if has_query_params and settings.tiles_dynamic_cache_with_params:
        params_key = _params_key_from_query(ids=ids, properties=properties)
        cached = get_tile_with_params(collection_id, z, x, y, params_key)
        if cached is not None:
            return Response(
                content=cached,
                media_type="application/x-protobuf",
                headers=cache_headers,
            )

    layer_name = _mvt_layer_name(collection_id)
    max_features = settings.tiles_mvt_max_features

    if props_include:
        property_keys = [k for k in props_include if _safe_json_key(k) == k][:_MVT_MAX_PROPERTY_KEYS]
    else:
        property_keys = await _get_property_keys(db, collection_id, feature_ids)
    prop_cols = _mvt_property_select_fragment(property_keys)

    extra_where, extra_params = _build_dynamic_tile_where(
        bbox_tuple=None,
        dt_start=None,
        dt_end=None,
        feature_ids=feature_ids,
        structured_filters=[],
        fulltext_q=None,
    )

    tile_env = "ST_Transform(ST_TileEnvelope(:z, :x, :y), 4326)"
    prop_select = f", {prop_cols}" if prop_cols else ""

    only_ids_filter = bool(feature_ids)

    if only_ids_filter:
        # Force primary-key lookup first via MATERIALIZED CTE; then filter by tile intersection.
        prop_select_by_id = (
            prop_select.replace("(properties ", "(by_id.properties ") if prop_cols else ""
        )
        sql = f"""
        WITH by_id AS MATERIALIZED (
            SELECT id, geometry, properties
            FROM features
            WHERE collection_id = :cid AND id = ANY(:ids) AND geometry IS NOT NULL
        )
        SELECT ST_AsMVT(tile, :layer_name, 4096, 'geom') AS mvt
        FROM (
            SELECT
                by_id.id{prop_select_by_id},
                ST_AsMVTGeom(
                    ST_Transform(ST_CurveToLine(by_id.geometry::geometry), 3857),
                    ST_TileEnvelope(:z, :x, :y),
                    4096,
                    256,
                    true
                ) AS geom
            FROM by_id
            WHERE ST_Intersects(by_id.geometry, {tile_env})
            LIMIT :max_features
        ) AS tile
        WHERE tile.geom IS NOT NULL
        """
        params = {
            "layer_name": layer_name,
            "z": z,
            "x": x,
            "y": y,
            "cid": collection_id,
            "max_features": max_features,
            "ids": feature_ids,
        }
    else:
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
                  {tile_env}
              )
              {extra_where}
            LIMIT :max_features
        ) AS tile
        WHERE tile.geom IS NOT NULL
        """
        params = {
            "layer_name": layer_name,
            "z": z,
            "x": x,
            "y": y,
            "cid": collection_id,
            "max_features": max_features,
            **extra_params,
        }
    timeout_sec = getattr(settings, "tiles_dynamic_statement_timeout_seconds", 0) or 0
    if timeout_sec > 0:
        # SET does not accept bound params; value is from config (integer ms).
        await db.execute(text(f"SET statement_timeout = {int(timeout_sec * 1000)}"))
    try:
        result = await db.execute(text(sql), params)
        row = result.first()
        mvt = row.mvt if row and row.mvt else None
        payload = bytes(mvt) if mvt else b""
    finally:
        if timeout_sec > 0:
            await db.execute(text("RESET statement_timeout"))
    if not has_query_params:
        set_cached_tile(collection_id, z, x, y, payload)
    elif params_key is not None:
        set_tile_with_params(collection_id, z, x, y, params_key, payload)
    return Response(
        content=payload,
        media_type="application/x-protobuf",
        headers=cache_headers,
    )


def _read_tile_from_mbtiles(path: Path, z: int, x: int, y: int) -> bytes | None:
    """Read a single tile from an MBTiles (SQLite) file. Sync, run in thread. Returns decompressed tile bytes or None.
    MBTiles uses TMS row order (y=0 at bottom); we convert XYZ y to TMS row."""
    import sqlite3
    tms_row = (1 << z) - 1 - y
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, tms_row),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    raw = row[0]
    if raw is None:
        return None
    # MBTiles from tippecanoe typically store gzip-compressed tiles
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


@router.get(
    "/{collection_id}/tiles/static/{z:int}/{x:int}/{y:int}.pbf",
    summary="Static vector tile (Z/X/Y from MBTiles)",
    description="Returns the tile at z/x/y from the built MBTiles file for this collection.",
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
    tile_bytes = await asyncio.to_thread(_read_tile_from_mbtiles, path, z, x, y)
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


