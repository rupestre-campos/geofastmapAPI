"""User-created map: name, description, thumbnail, definition (layers with collection_id, color, order)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.base import Base


class Map(Base):
    """Stored map definition for the gallery and viewer."""

    __tablename__ = "maps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    thumbnail = Column(Text, nullable=True)
    thumbnail_data = Column(LargeBinary, nullable=True)
    definition = Column(JSONB, nullable=False, server_default="{}")
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    visibility = Column(String(32), nullable=False, server_default="private")
    # When True, everyone who can see the map (by visibility) can edit; when False, only owner + explicit editor shares can edit.
    viewer_can_edit = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
