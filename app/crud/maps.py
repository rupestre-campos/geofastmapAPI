"""CRUD for user-created maps."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.map import Map
from app.schemas.map import MapCreate, MapDefinition, MapUpdate


async def list_maps(db: AsyncSession, limit: int = 100, offset: int = 0) -> list[Map]:
    """List maps, newest first."""
    result = await db.execute(
        select(Map).order_by(Map.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def get_map(db: AsyncSession, map_id: uuid.UUID) -> Map | None:
    """Get a map by id."""
    result = await db.execute(select(Map).where(Map.id == map_id))
    return result.scalar_one_or_none()


async def create_map(db: AsyncSession, data: MapCreate) -> Map:
    """Create a new map."""
    row = Map(
        id=uuid.uuid4(),
        name=data.name,
        description=data.description,
        thumbnail=data.thumbnail,
        definition=data.definition.model_dump(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_map(db: AsyncSession, map_id: uuid.UUID, data: MapUpdate) -> Map | None:
    """Update a map. Returns updated row or None if not found."""
    row = await get_map(db, map_id)
    if row is None:
        return None
    if data.name is not None:
        row.name = data.name
    if data.description is not None:
        row.description = data.description
    if data.thumbnail is not None:
        row.thumbnail = data.thumbnail
    if data.definition is not None:
        row.definition = data.definition.model_dump()
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_map(db: AsyncSession, map_id: uuid.UUID) -> bool:
    """Delete a map. Returns True if deleted."""
    row = await get_map(db, map_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
