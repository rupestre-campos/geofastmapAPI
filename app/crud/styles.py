"""CRUD for layer styles (public and collection-specific)."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.style import Style


PUBLIC_COLLECTION_ID = ""


async def list_public_styles(db: AsyncSession) -> Sequence[Style]:
    """List all public (global) styles."""
    result = await db.execute(
        select(Style).where(Style.collection_id == PUBLIC_COLLECTION_ID).order_by(Style.id)
    )
    return result.scalars().all()


async def list_collection_styles(db: AsyncSession, collection_id: str) -> Sequence[Style]:
    """List styles for a collection (collection-specific only)."""
    result = await db.execute(
        select(Style).where(Style.collection_id == collection_id).order_by(Style.id)
    )
    return result.scalars().all()


async def get_default_style(db: AsyncSession, collection_id: str) -> Style | None:
    """Get the default style for a collection, if any."""
    result = await db.execute(
        select(Style).where(
            Style.collection_id == collection_id,
            Style.is_default.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_public_style(db: AsyncSession, style_id: str) -> Style | None:
    """Get a public style by id."""
    result = await db.execute(
        select(Style).where(
            Style.collection_id == PUBLIC_COLLECTION_ID,
            Style.id == style_id,
        )
    )
    return result.scalar_one_or_none()


async def get_collection_style(
    db: AsyncSession, collection_id: str, style_id: str
) -> Style | None:
    """Get a collection style by id, or a public style by id if not found on collection."""
    result = await db.execute(
        select(Style).where(
            Style.collection_id == collection_id,
            Style.id == style_id,
        )
    )
    style = result.scalar_one_or_none()
    if style is not None:
        return style
    return await get_public_style(db, style_id)


async def create_style(
    db: AsyncSession,
    style_id: str,
    style_spec: dict,
    title: str | None = None,
    collection_id: str = PUBLIC_COLLECTION_ID,
    set_default: bool = False,
) -> Style:
    """Create a style. For collection styles, optionally set as default."""
    if collection_id and set_default:
        await _unset_default_for_collection(db, collection_id or PUBLIC_COLLECTION_ID)
    style = Style(
        collection_id=collection_id or PUBLIC_COLLECTION_ID,
        id=style_id,
        title=title,
        style_spec=style_spec,
        is_default=set_default,
    )
    db.add(style)
    await db.commit()
    await db.refresh(style)
    return style


async def replace_style(
    db: AsyncSession,
    collection_id: str,
    style_id: str,
    style_spec: dict,
    title: str | None = None,
    set_default: bool = False,
) -> Style | None:
    """Replace a collection style. Returns None if not found."""
    style = await get_collection_style(db, collection_id, style_id)
    if style is None or style.collection_id != collection_id:
        return None
    if set_default:
        await _unset_default_for_collection(db, collection_id)
    style.title = title
    style.style_spec = style_spec
    style.is_default = set_default
    await db.commit()
    await db.refresh(style)
    return style


async def patch_style(
    db: AsyncSession,
    collection_id: str,
    style_id: str,
    *,
    title: str | None = None,
    style_spec: dict | None = None,
    set_default: bool | None = None,
) -> Style | None:
    """Patch a collection style."""
    style = await get_collection_style(db, collection_id, style_id)
    if style is None or style.collection_id != collection_id:
        return None
    if title is not None:
        style.title = title
    if style_spec is not None:
        style.style_spec = style_spec
    if set_default is not None:
        if set_default:
            await _unset_default_for_collection(db, collection_id)
        style.is_default = set_default
    await db.commit()
    await db.refresh(style)
    return style


async def replace_public_style(
    db: AsyncSession,
    style_id: str,
    style_spec: dict,
    title: str | None = None,
) -> Style | None:
    """Replace a public style by id."""
    style = await get_public_style(db, style_id)
    if style is None:
        return None
    style.title = title
    style.style_spec = style_spec
    await db.commit()
    await db.refresh(style)
    return style


async def patch_public_style(
    db: AsyncSession,
    style_id: str,
    *,
    title: str | None = None,
    style_spec: dict | None = None,
) -> Style | None:
    """Patch a public style."""
    style = await get_public_style(db, style_id)
    if style is None:
        return None
    if title is not None:
        style.title = title
    if style_spec is not None:
        style.style_spec = style_spec
    await db.commit()
    await db.refresh(style)
    return style


async def delete_style(
    db: AsyncSession, collection_id: str, style_id: str
) -> bool:
    """Delete a style. For public styles use collection_id=''."""
    cid = collection_id or PUBLIC_COLLECTION_ID
    result = await db.execute(
        select(Style).where(
            Style.collection_id == cid,
            Style.id == style_id,
        )
    )
    style = result.scalar_one_or_none()
    if style is None:
        return False
    await db.delete(style)
    await db.commit()
    return True


async def delete_collection_styles(db: AsyncSession, collection_id: str) -> None:
    """Delete all styles for a collection (call when deleting collection)."""
    result = await db.execute(select(Style).where(Style.collection_id == collection_id))
    for style in result.scalars().all():
        await db.delete(style)
    await db.commit()


async def _unset_default_for_collection(db: AsyncSession, collection_id: str) -> None:
    from sqlalchemy import update
    stmt = (
        update(Style)
        .where(Style.collection_id == collection_id, Style.is_default.is_(True))
        .values(is_default=False)
    )
    await db.execute(stmt)
