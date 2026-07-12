"""Composite (merged) collection helpers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import collection_tiles as tiles_crud
from app.models.collection import COLLECTION_TYPE_COMPOSITE, COLLECTION_TYPE_VECTOR, Collection
from app.services.collection_tiles_revision import compute_collection_tiles_revision
from app.services.static_tiles_path import has_static_mbtiles_file, resolve_mbtiles_path
from app.services.mvt_merge import compute_composite_tiles_revision


def parse_composite_members(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("collection_id"):
            out.append({"collection_id": str(item["collection_id"]).strip()})
        elif isinstance(item, str) and item.strip():
            out.append({"collection_id": item.strip()})
    return out


def member_collection_ids(members: list[dict[str, str]]) -> list[str]:
    return [m["collection_id"] for m in members if m.get("collection_id")]


def is_composite_collection(collection: Collection) -> bool:
    return getattr(collection, "collection_type", "") == COLLECTION_TYPE_COMPOSITE


async def validate_composite_members(
    db: AsyncSession,
    composite_id: str,
    members: list[dict[str, str]],
) -> None:
    from fastapi import HTTPException, status

    if not members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Composite collection requires at least one member",
        )
    seen: set[str] = set()
    for m in members:
        cid = m["collection_id"]
        if cid == composite_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Composite cannot include itself as a member",
            )
        if cid in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate member: {cid}",
            )
        seen.add(cid)
        row = await db.get(Collection, cid)
        if not row:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Member collection not found: {cid}",
            )
        if getattr(row, "collection_type", "") == COLLECTION_TYPE_COMPOSITE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Nested composite members are not supported: {cid}",
            )
        if getattr(row, "collection_type", COLLECTION_TYPE_VECTOR) != COLLECTION_TYPE_VECTOR:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Member must be a vector collection: {cid}",
            )


async def member_tile_status(
    db: AsyncSession,
    members: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Per-member tile build status for composite admin UI."""
    out: list[dict[str, Any]] = []
    for m in members:
        cid = m["collection_id"]
        coll = await db.get(Collection, cid)
        result = await db.execute(
            text(
                """
                SELECT pmtiles_path, built_at, minzoom, maxzoom, tiles_revision
                FROM collection_tiles WHERE collection_id = :cid
                """
            ),
            {"cid": cid},
        )
        tile_row = result.first()
        path = tile_row.pmtiles_path if tile_row else None
        has_static = has_static_mbtiles_file(cid, path)
        out.append(
            {
                "collection_id": cid,
                "title": coll.title if coll else None,
                "feature_count": int(coll.feature_count or 0) if coll else 0,
                "has_static_tiles": has_static,
                "tiles_revision": tile_row.tiles_revision if tile_row else None,
                "minzoom": tile_row.minzoom if tile_row else None,
                "maxzoom": tile_row.maxzoom if tile_row else None,
                "built_at": tile_row.built_at.isoformat() if tile_row and tile_row.built_at else None,
            }
        )
    return out


async def composite_tiles_revision(db: AsyncSession, members: list[dict[str, str]]) -> str:
    revisions: list[str | None] = []
    for m in members:
        cid = m["collection_id"]
        result = await db.execute(
            text("SELECT tiles_revision FROM collection_tiles WHERE collection_id = :cid"),
            {"cid": cid},
        )
        row = result.first()
        revisions.append(row.tiles_revision if row else None)
    return compute_composite_tiles_revision(revisions)


async def composite_has_own_static_tiles(db: AsyncSession, composite_id: str) -> bool:
    rec = await tiles_crud.get_collection_tiles(db, composite_id)
    return has_static_mbtiles_file(composite_id, rec.pmtiles_path if rec else None)


async def composite_own_tile_record(db: AsyncSession, composite_id: str):
    return await tiles_crud.get_collection_tiles(db, composite_id)


async def composite_members_max_feature_updated_at(
    db: AsyncSession,
    members: list[dict[str, str]],
) -> Any:
    ids = member_collection_ids(members)
    if not ids:
        return None
    result = await db.execute(
        text("SELECT MAX(features_last_updated_at) AS m FROM collections WHERE id = ANY(:ids)"),
        {"ids": ids},
    )
    row = result.first()
    return row.m if row and row.m else None


async def composite_resolved_static_revision(
    db: AsyncSession,
    composite_id: str,
    members: list[dict[str, str]],
) -> str | None:
    """Revision for static tile URLs: composite MBTiles when built, else merged member revisions."""
    rec = await tiles_crud.get_collection_tiles(db, composite_id)
    resolved = resolve_mbtiles_path(composite_id, rec.pmtiles_path if rec else None)
    if resolved:
        if rec and rec.tiles_revision:
            return rec.tiles_revision
        return compute_collection_tiles_revision(composite_id, str(resolved))
    rev = await composite_tiles_revision(db, members)
    return rev or None


async def composite_has_static_tiles(
    db: AsyncSession,
    composite_id: str,
    members: list[dict[str, str]],
) -> bool:
    if await composite_has_own_static_tiles(db, composite_id):
        return True
    statuses = await member_tile_status(db, members)
    return any(s.get("has_static_tiles") for s in statuses)


async def mark_composite_static_stale(db: AsyncSession, composite_id: str) -> None:
    """Drop composite-owned MBTiles so the next build exports all current members."""
    rec = await tiles_crud.get_collection_tiles(db, composite_id)
    if rec and rec.pmtiles_path:
        path = Path(rec.pmtiles_path)
        if path.is_file():
            try:
                os.unlink(path)
            except OSError:
                pass
    await tiles_crud.clear_static_tiles(db, composite_id)


async def composite_dynamic_revision(db: AsyncSession, members: list[dict[str, str]]) -> str:
    """Revision for merged dynamic tiles (member feature update timestamps)."""
    parts: list[str] = []
    for m in members:
        cid = m["collection_id"]
        coll = await db.get(Collection, cid)
        stamp = ""
        if coll and coll.features_last_updated_at:
            stamp = coll.features_last_updated_at.isoformat()
        parts.append(f"{cid}:{stamp}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
