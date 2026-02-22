from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class Feature(Base):
    """OGC API - Features 'item' (feature) entity."""

    __tablename__ = "features"

    id = Column(
        String,
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    collection_id = Column(
        String,
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Spatial column (PostGIS)
    geometry = Column(
        Geometry(geometry_type="GEOMETRY", srid=4326),
        nullable=True
    )

    properties = Column(JSONB, nullable=True)

    # Generated column (see migration 0002): flatten properties to text for full-text/trigram search
    properties_flat = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    # GIST index on geometry is created in Alembic migration (idx_features_geometry)