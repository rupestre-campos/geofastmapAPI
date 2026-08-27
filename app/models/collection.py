from datetime import datetime

from sqlalchemy import Boolean, Integer, JSON, Column, DateTime, ForeignKey, String, Text

from app.db.base import Base

VISIBILITY_PUBLIC = "public"
VISIBILITY_LOGGED = "logged"
VISIBILITY_PRIVATE = "private"
COLLECTION_TYPE_VECTOR = "vector"
COLLECTION_TYPE_RASTER = "raster"
COLLECTION_TYPE_COMPOSITE = "composite"


class Collection(Base):
    """OGC API - Features collection metadata."""

    __tablename__ = "collections"

    id: str = Column(String, primary_key=True, index=True)
    title: str | None = Column(String, nullable=True)
    description: str | None = Column(Text, nullable=True)
    extent: dict | None = Column(JSON, nullable=True)
    # Optional link to external STAC: {"catalog_id": "...", "collection_id": "..."}
    stac_source: dict | None = Column(JSON, nullable=True)
    # Raster-only options, e.g. {"is_dem": true, "dem_encoding": "terrainrgb"}.
    raster_settings: dict | None = Column(JSON, nullable=True)
    # Cached total feature count; used when listing items with no filters to avoid COUNT queries.
    feature_count: int = Column(Integer, nullable=False, default=0, server_default="0")
    owner_id: int | None = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    visibility: str = Column(String(32), nullable=False, default=VISIBILITY_PRIVATE, server_default="private")
    # When True, everyone who can see the collection (by visibility) can edit; when False, only owner + explicit editor shares can edit.
    viewer_can_edit: bool = Column(Boolean, nullable=False, default=False, server_default="false")
    collection_type: str = Column(String(16), nullable=False, default=COLLECTION_TYPE_VECTOR, server_default=COLLECTION_TYPE_VECTOR)
    # Ordered member list for composite collections: [{"collection_id": "..."}, ...]
    composite_members: dict | None = Column(JSON, nullable=True)
    # Property keys to index on features.properties for this collection only (expression btree).
    property_index_fields: list | None = Column(JSON, nullable=True)

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
    # Denormalized MAX(features.updated_at); maintained by DB triggers (see migration 0033).
    features_last_updated_at: datetime | None = Column(DateTime(timezone=True), nullable=True)

