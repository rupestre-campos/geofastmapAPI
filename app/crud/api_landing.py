"""CRUD for API landing page content (single row)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_landing import API_LANDING_ID, ApiLanding

DEFAULT_TITLE = "GeoFastMap API"
DEFAULT_DESCRIPTION = (
    "OGC API – Features and Tiles. Browse feature collections and items (GeoJSON), "
    "view and edit on maps, and use vector tiles (static and dynamic) per collection."
)
DEFAULT_CONTACT = (
    "API owner and contact information can be edited from the landing page. "
    "Click **Edit API info** to set title, description, and contact details."
)


async def get_api_landing(db: AsyncSession) -> ApiLanding | None:
    """Return the single API landing row, or None if not yet created."""
    result = await db.execute(select(ApiLanding).where(ApiLanding.id == API_LANDING_ID))
    return result.scalar_one_or_none()


async def get_or_create_api_landing(db: AsyncSession) -> ApiLanding:
    """Return the API landing row; create with defaults if missing."""
    row = await get_api_landing(db)
    if row is not None:
        return row
    row = ApiLanding(
        id=API_LANDING_ID,
        title=DEFAULT_TITLE,
        description=DEFAULT_DESCRIPTION,
        contact=DEFAULT_CONTACT,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_api_landing(
    db: AsyncSession,
    *,
    title: str | None = None,
    description: str | None = None,
    contact: str | None = None,
) -> ApiLanding | None:
    """Update the API landing row. Omitted fields are left unchanged. Returns updated row or None."""
    row = await get_api_landing(db)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if description is not None:
        row.description = description
    if contact is not None:
        row.contact = contact
    await db.commit()
    await db.refresh(row)
    return row
