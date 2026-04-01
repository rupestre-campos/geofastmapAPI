"""Saved MosaicJSON / Titiler view definitions on disk."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base
from app.models.collection import VISIBILITY_PRIVATE


class RasterView(Base):
    __tablename__ = "raster_views"

    id: str = Column(String(64), primary_key=True, index=True)
    title: str = Column(String(512), nullable=False)
    owner_id: int | None = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    visibility: str = Column(String(32), nullable=False, default=VISIBILITY_PRIVATE, server_default="private")
    json_relative_path: str = Column(String(1024), nullable=False)

    created_at: datetime = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: datetime = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
