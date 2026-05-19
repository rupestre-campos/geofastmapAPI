"""CRUD for resource shares (grant viewer/editor access by username)."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resource_share import ResourceShare, ROLE_EDITOR, ROLE_VIEWER


async def list_shares_for_resource_ids(
    db: AsyncSession,
    resource_type: str,
    resource_ids: list[str],
) -> dict[str, list[tuple[str, str]]]:
    """Return resource_id -> [(username, role), ...] for many resources."""
    ids = [rid for rid in resource_ids if rid]
    if not ids:
        return {}
    result = await db.execute(
        select(ResourceShare.resource_id, ResourceShare.username, ResourceShare.role).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id.in_(ids),
        ).order_by(ResourceShare.resource_id, ResourceShare.username)
    )
    out: dict[str, list[tuple[str, str]]] = {rid: [] for rid in ids}
    for row in result.all():
        out.setdefault(row.resource_id, []).append((row.username, row.role))
    return out


async def list_shares(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
) -> Sequence[tuple[str, str]]:
    """Return list of (username, role) for the given resource."""
    result = await db.execute(
        select(ResourceShare.username, ResourceShare.role).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
        ).order_by(ResourceShare.username)
    )
    return result.all()


async def add_share(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    username: str,
    role: str = ROLE_VIEWER,
) -> ResourceShare | None:
    """Add or update a share. role must be viewer or editor. Returns the share or None if user not found."""
    from app.crud import user as user_crud
    user = await user_crud.get_user_by_username(db, username.strip())
    if not user:
        return None
    username = user.username
    existing = await db.execute(
        select(ResourceShare).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
            ResourceShare.username == username,
        )
    )
    row = existing.scalar_one_or_none()
    if row:
        row.role = role if role in (ROLE_VIEWER, ROLE_EDITOR) else ROLE_VIEWER
        await db.commit()
        await db.refresh(row)
        return row
    share = ResourceShare(
        resource_type=resource_type,
        resource_id=resource_id,
        username=username,
        role=role if role in (ROLE_VIEWER, ROLE_EDITOR) else ROLE_VIEWER,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    return share


async def remove_share(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    username: str,
) -> bool:
    """Remove a share. Returns True if a row was deleted."""
    result = await db.execute(
        select(ResourceShare).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
            ResourceShare.username == username.strip(),
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    return True
