"""CRUD for user-created maps."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import cast, exists, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.map import Map
from app.models.resource_share import ResourceShare
from app.schemas.map import MapCreate, MapDefinition, MapUpdate

if TYPE_CHECKING:
    from app.models.user import User

VISIBILITY_PUBLIC = "public"
VISIBILITY_LOGGED = "logged"
RESOURCE_TYPE_MAP = "map"


async def list_maps(
    db: AsyncSession,
    limit: int = 100,
    offset: int = 0,
    current_user: "User | None" = None,
) -> list[Map]:
    """List maps, newest first. Admin sees all; else visibility and sharing apply."""
    base = select(Map)
    if current_user is not None and current_user.is_admin:
        pass
    elif current_user is None:
        base = base.where(Map.visibility == VISIBILITY_PUBLIC)
    else:
        share_exists = (
            select(1)
            .where(ResourceShare.resource_type == RESOURCE_TYPE_MAP)
            .where(ResourceShare.resource_id == cast(Map.id, String))
            .where(ResourceShare.username == current_user.username)
        )
        base = base.where(
            or_(
                Map.visibility.in_([VISIBILITY_PUBLIC, VISIBILITY_LOGGED]),
                Map.owner_id == current_user.id,
                exists(share_exists),
            )
        )
    result = await db.execute(
        base.order_by(Map.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def get_map(db: AsyncSession, map_id: uuid.UUID) -> Map | None:
    """Get a map by id."""
    result = await db.execute(select(Map).where(Map.id == map_id))
    return result.scalar_one_or_none()


async def create_map(
    db: AsyncSession,
    data: MapCreate,
    *,
    owner_id: int | None = None,
    visibility: str = "private",
) -> Map:
    """Create a new map."""
    row = Map(
        id=uuid.uuid4(),
        name=data.name,
        description=data.description,
        thumbnail=data.thumbnail,
        definition=data.definition.model_dump(),
        owner_id=owner_id,
        visibility=visibility,
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
        # If setting an external URL, clear uploaded thumbnail
        if data.thumbnail:
            row.thumbnail_data = None
    if data.definition is not None:
        row.definition = data.definition.model_dump()
    if data.visibility is not None:
        from app.models.collection import VISIBILITY_LOGGED, VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
        if data.visibility in (VISIBILITY_PRIVATE, VISIBILITY_LOGGED, VISIBILITY_PUBLIC):
            row.visibility = data.visibility
    if data.viewer_can_edit is not None:
        row.viewer_can_edit = data.viewer_can_edit
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def set_map_thumbnail_data(db: AsyncSession, map_id: uuid.UUID, data: bytes) -> Map | None:
    """Store uploaded thumbnail bytes and clear external thumbnail URL."""
    row = await get_map(db, map_id)
    if row is None:
        return None
    row.thumbnail_data = data
    row.thumbnail = None
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
