"""Shared tile asset / Titiler context for raster collection edit and raster studio (avoid duplicating DB logic)."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import features as features_crud


def mosaic_version_id(collection_id: str, item_ids: list[str]) -> str | None:
    if not item_ids:
        return None
    raw = f"{collection_id}:{','.join(item_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _band_count_from_feature(f) -> int:
    if not f or not isinstance(getattr(f, "properties", None), dict):
        return 1
    meta = (f.properties.get("raster") or {}).get("meta") or {}
    if not isinstance(meta, dict):
        return 1
    n = meta.get("count")
    if isinstance(n, int) and n >= 1:
        return min(n, 512)
    return 1


async def get_raster_style_edit_context(db: AsyncSession, collection_id: str) -> dict[str, Any]:
    """Returns tile_assets, default_tile_asset, mosaic_version_id, titiler_configured, band_counts for template + JS."""
    q = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    item_ids = [r.id for r in q.fetchall()]
    mv = mosaic_version_id(collection_id, item_ids)
    band_counts: dict[str, int] = {}
    per_item_counts: list[int] = []
    tile_assets: list[dict[str, str]] = []
    # Full-collection mosaic: always offer first when there is at least one raster item.
    if item_ids:
        for fid in item_ids:
            f = await features_crud.get_feature(db, collection_id, fid)
            cnt = _band_count_from_feature(f)
            band_counts[str(fid)] = cnt
            per_item_counts.append(cnt)
            title = str(fid)
            if f and isinstance(f.properties, dict):
                t = f.properties.get("title")
                if t:
                    title = str(t)[:120]
            tile_assets.append({"key": str(fid), "title": f"Item — {title}"})
        band_counts["__mosaic__"] = max(per_item_counts) if per_item_counts else 1
        tile_assets.insert(0, {"key": "__mosaic__", "title": "Collection"})
    default_tile_asset: str | None = None
    if item_ids:
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
        "band_counts": band_counts,
    }
