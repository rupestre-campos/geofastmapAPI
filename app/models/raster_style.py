"""Raster style presets for raster collections (Titiler render parameters)."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String

from app.db.base import Base


class RasterStyle(Base):
    __tablename__ = "raster_styles"

    collection_id: str = Column(String, primary_key=True)
    id: str = Column(String, primary_key=True, index=True)
    title: str | None = Column(String, nullable=True)
    is_default: bool = Column(Boolean, nullable=False, default=False, server_default="false")
    style_spec: dict = Column(JSON, nullable=False)
    owner_id: int | None = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    visibility: str = Column(String(32), nullable=False, default="private", server_default="private")
    created_at: datetime = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
