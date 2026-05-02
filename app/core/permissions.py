"""Visibility and edit permission: admin sees all; owner/editor can edit; viewer/owner/editor can see."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection, VISIBILITY_LOGGED, VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
from app.models.map import Map
from app.models.resource_share import (
    RESOURCE_TYPE_COLLECTION,
    RESOURCE_TYPE_MAP,
    RESOURCE_TYPE_RASTER_VIEW,
    RESOURCE_TYPE_STYLE,
    ROLE_EDITOR,
    ROLE_VIEWER,
)
from app.models.resource_share import ResourceShare, raster_style_resource_id, style_resource_id
from app.models.user import User


def _visibility_visible_to_anon(visibility: str) -> bool:
    return visibility == VISIBILITY_PUBLIC


def _visibility_visible_to_logged(visibility: str) -> bool:
    return visibility in (VISIBILITY_PUBLIC, VISIBILITY_LOGGED)


async def get_share_role(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    username: str,
) -> str | None:
    """Return 'viewer' or 'editor' if user has a share; else None."""
    if not username:
        return None
    r = await db.execute(
        select(ResourceShare.role).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
            ResourceShare.username == username,
        )
    )
    row = r.scalar_one_or_none()
    return row if row else None


async def can_see_collection(db: AsyncSession, collection: Collection, user: User | None) -> bool:
    if user and user.is_admin:
        return True
    if _visibility_visible_to_anon(collection.visibility):
        return True
    if not user:
        return False
    if _visibility_visible_to_logged(collection.visibility):
        return True
    if collection.owner_id == user.id:
        return True
    role = await get_share_role(db, RESOURCE_TYPE_COLLECTION, collection.id, user.username)
    return role in (ROLE_VIEWER, ROLE_EDITOR)


async def can_edit_collection(db: AsyncSession, collection: Collection, user: User | None) -> bool:
    if not user:
        return False
    if user.is_admin:
        return True
    if collection.owner_id == user.id:
        return True
    role = await get_share_role(db, RESOURCE_TYPE_COLLECTION, collection.id, user.username)
    if role == ROLE_EDITOR:
        return True
    # When viewer_can_edit is True, everyone who can see the collection (by visibility) can edit.
    if getattr(collection, "viewer_can_edit", False) and await can_see_collection(db, collection, user):
        return True
    return False


def _map_visibility_visible(visibility: str, user: User | None) -> bool:
    if visibility == VISIBILITY_PUBLIC:
        return True
    if user and visibility == VISIBILITY_LOGGED:
        return True
    return False


async def can_see_map(db: AsyncSession, map_owner_id: int | None, map_visibility: str, map_id: str, user: User | None) -> bool:
    if user and user.is_admin:
        return True
    if _map_visibility_visible(map_visibility, user):
        return True
    if user and map_owner_id == user.id:
        return True
    if user:
        role = await get_share_role(db, RESOURCE_TYPE_MAP, map_id, user.username)
        if role in (ROLE_VIEWER, ROLE_EDITOR):
            return True
    return False


async def can_edit_map(
    db: AsyncSession,
    map_owner_id: int | None,
    map_id: str,
    user: User | None,
    map_visibility: str | None = None,
    viewer_can_edit: bool | None = None,
) -> bool:
    if not user:
        return False
    if user.is_admin:
        return True
    if map_owner_id == user.id:
        return True
    role = await get_share_role(db, RESOURCE_TYPE_MAP, map_id, user.username)
    if role == ROLE_EDITOR:
        return True
    # When viewer_can_edit is True, everyone who can see the map (by visibility) can edit.
    if viewer_can_edit is None or map_visibility is None:
        try:
            uid = UUID(map_id)
        except ValueError:
            return False
        r = await db.execute(select(Map.visibility, Map.viewer_can_edit).where(Map.id == uid))
        row = r.one_or_none()
        if row:
            map_visibility = row.visibility
            viewer_can_edit = row.viewer_can_edit
    if viewer_can_edit and map_visibility and await can_see_map(db, map_owner_id, map_visibility, map_id, user):
        return True
    return False  # no explicit editor share and viewer_can_edit not granted


async def can_see_style(
    db: AsyncSession,
    style_owner_id: int | None,
    style_visibility: str,
    collection_id: str,
    style_id: str,
    user: User | None,
) -> bool:
    if user and user.is_admin:
        return True
    if style_visibility == VISIBILITY_PUBLIC:
        return True
    if user and style_visibility == VISIBILITY_LOGGED:
        return True
    if user and style_owner_id == user.id:
        return True
    if user:
        rid = style_resource_id(collection_id, style_id)
        role = await get_share_role(db, RESOURCE_TYPE_STYLE, rid, user.username)
        if role in (ROLE_VIEWER, ROLE_EDITOR):
            return True
    return False


async def can_see_raster_style(
    db: AsyncSession,
    style_owner_id: int | None,
    style_visibility: str,
    collection_id: str,
    style_id: str,
    user: User | None,
) -> bool:
    """Visibility + shares for raster style presets (collection-scoped or public with collection_id '')."""
    if user and user.is_admin:
        return True
    if style_visibility == VISIBILITY_PUBLIC:
        return True
    if user and style_visibility == VISIBILITY_LOGGED:
        return True
    if user and style_owner_id == user.id:
        return True
    if user:
        rid = raster_style_resource_id(collection_id, style_id)
        role = await get_share_role(db, RESOURCE_TYPE_STYLE, rid, user.username)
        if role in (ROLE_VIEWER, ROLE_EDITOR):
            return True
    return False


async def can_see_raster_view(
    db: AsyncSession,
    owner_id: int | None,
    visibility: str,
    view_id: str,
    user: User | None,
) -> bool:
    if user and user.is_admin:
        return True
    if visibility == VISIBILITY_PUBLIC:
        return True
    if user and visibility == VISIBILITY_LOGGED:
        return True
    if user and owner_id == user.id:
        return True
    if user:
        role = await get_share_role(db, RESOURCE_TYPE_RASTER_VIEW, view_id, user.username)
        if role in (ROLE_VIEWER, ROLE_EDITOR):
            return True
    return False


async def can_edit_raster_view(
    db: AsyncSession,
    owner_id: int | None,
    view_id: str,
    user: User | None,
) -> bool:
    if not user:
        return False
    if user.is_admin:
        return True
    if owner_id == user.id:
        return True
    role = await get_share_role(db, RESOURCE_TYPE_RASTER_VIEW, view_id, user.username)
    return role == ROLE_EDITOR


def can_access_raster_view_tiles_anonymous(*, visibility: str, allow_public_maps: bool) -> bool:
    """Anonymous tile access: public mosaic, or allow_public_maps for public map embed."""
    if visibility == VISIBILITY_PUBLIC:
        return True
    if allow_public_maps:
        return True
    return False


async def can_edit_style(
    db: AsyncSession,
    style_owner_id: int | None,
    collection_id: str,
    style_id: str,
    user: User | None,
) -> bool:
    if not user:
        return False
    if user.is_admin:
        return True
    if style_owner_id == user.id:
        return True
    rid = style_resource_id(collection_id, style_id)
    role = await get_share_role(db, RESOURCE_TYPE_STYLE, rid, user.username)
    return role == ROLE_EDITOR
