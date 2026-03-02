from datetime import datetime

from sqlalchemy import Column, Computed, DateTime, ForeignKey, String, Text
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB
from uuid6 import uuid7

from app.db.base import Base


class Feature(Base):
    """OGC API - Features 'item' (feature) entity. Table is partitioned by collection_id (LIST)."""

    __tablename__ = "features"

    # UUID v7: time-sortable (timestamp prefix), good for indexing and ordering by creation
    id = Column(
        String,
        primary_key=True,
        default=lambda: str(uuid7()),
    )

    # Partition key; must be part of PK for PostgreSQL partitioned tables
    collection_id = Column(
        String,
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )

    # Spatial column (PostGIS)
    geometry = Column(
        Geometry(geometry_type="GEOMETRY", srid=4326),
        nullable=True
    )

    properties = Column(JSONB, nullable=True)

    # Generated column (see migration 0002): flatten properties to text for full-text/trigram search.
    # Mark as Computed so SQLAlchemy does not include it in INSERT/UPDATE.
    properties_flat = Column(Text, Computed("jsonb_flat_text(properties)"), nullable=True)

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