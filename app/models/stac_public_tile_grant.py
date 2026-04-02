"""User consent: allow anonymous tile access for a STAC item (public maps)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class StacPublicTileGrant(Base):
    __tablename__ = "stac_public_tile_grants"
    __table_args__ = (
        UniqueConstraint(
            "catalog_id",
            "stac_collection_id",
            "stac_item_id",
            name="uq_stac_public_tile_grants_item",
        ),
    )

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    catalog_id: str = Column(String(64), nullable=False, index=True)
    stac_collection_id: str = Column(String(512), nullable=False)
    stac_item_id: str = Column(String(512), nullable=False)
    granted_by_user_id: int | None = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: datetime = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
