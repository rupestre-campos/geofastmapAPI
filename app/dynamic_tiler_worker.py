"""Dynamic tile worker: HTTP service that fetches GeoJSON for a tile (DB + bbox), encodes MVT in a thread, returns .pbf.

Run with: uvicorn app.dynamic_tiler_worker:app --port 8001 --workers 1
Prefer multiple uvicorn workers or TILES_DYNAMIC_USE_QUEUE + tile_queue_worker for multi-core encode.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.dynamic_tile_geojson import get_geojson_for_tile
from app.services.mvt_encode import encode_geojson_to_mvt


app_router = APIRouter()


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    parts = [p.strip() for p in bbox.split(",")]
    if len(parts) != 4:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        return None


@app_router.get(
    "/collections/{collection_id}/tiles/dynamic/{z:int}/{x:int}/{y:int}.pbf",
    summary="Generate one vector tile (in-process MVT)",
    description="Same query params as GET items. Fetches GeoJSON for tile bbox, encodes MVT off the event loop, returns single tile. No cache; API caches.",
)
async def get_tile(
    request: Request,
    collection_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    limit: int | None = Query(None, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    sortby: str | None = Query(None),
    sortdesc: bool = Query(False),
    bbox: str | None = Query(None),
    datetime_param: str | None = Query(None, alias="datetime"),
    filter_param: list[str] | None = Query(None, alias="filter"),
    q: str | None = Query(None),
    ids: str | None = Query(None),
    properties: str | None = Query(None),
):
    if z < 0 or z > 22:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid z")
    if x < 0 or x >= (1 << z) or y < 0 or y >= (1 << z):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid x or y for zoom")

    ids_list = [i.strip() for i in ids.split(",") if i.strip()] if ids else None
    bbox_tuple = _parse_bbox(bbox)

    try:
        geojson_bytes = await get_geojson_for_tile(
            db,
            collection_id,
            z,
            x,
            y,
            limit=limit,
            offset=offset,
            sortby=sortby,
            sortdesc=sortdesc,
            bbox_user=bbox_tuple,
            datetime_param=datetime_param,
            filter_param=filter_param or None,
            q=q,
            ids=ids_list,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    try:
        tile_bytes = await asyncio.to_thread(
            encode_geojson_to_mvt, geojson_bytes, collection_id, z, x, y
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return Response(
        content=tile_bytes,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=60"},
    )


from fastapi import FastAPI

app = FastAPI(
    title="GeoFastMap Dynamic Tiler Worker",
    description="Generates vector tiles on demand via DB retrieval + off-loop MVT encoding.",
)
app.include_router(app_router)
