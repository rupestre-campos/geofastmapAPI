"""Shared tile asset / Titiler context for raster collection edit and raster studio (avoid duplicating DB logic)."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import features as features_crud


def mosaic_version_id(collection_id: str, item_ids: list[str]) -> str | None:
    if len(item_ids) <= 1:
        return None
    raw = f"{collection_id}:{','.join(item_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def get_raster_style_edit_context(db: AsyncSession, collection_id: str) -> dict[str, Any]:
    """Returns tile_assets, default_tile_asset, mosaic_version_id, titiler_configured for template + JS."""
    q = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    item_ids = [r.id for r in q.fetchall()]
    mv = mosaic_version_id(collection_id, item_ids)
    tile_assets: list[dict[str, str]] = []
    if len(item_ids) > 1:
        tile_assets.append({"key": "__mosaic__", "title": "Mosaic (all items)"})
    for fid in item_ids:
        f = await features_crud.get_feature(db, collection_id, fid)
        title = str(fid)
        if f and isinstance(f.properties, dict):
            t = f.properties.get("title")
            if t:
                title = f"{t}"
        tile_assets.append({"key": str(fid), "title": title[:120]})
    default_tile_asset: str | None = None
    if len(item_ids) == 1:
        default_tile_asset = str(item_ids[0])
    elif len(item_ids) > 1:
        default_tile_asset = "__mosaic__"
    settings = get_settings()
    titiler_ok = bool(
        settings.titiler_internal_url and settings.titiler_internal_secret and settings.raster_internal_fetch_base_url
    )
    return {
        "tile_assets": tile_assets,
        "default_tile_asset": default_tile_asset,
        "mosaic_version_id": mv or "",
        "titiler_configured": titiler_ok,
    }
