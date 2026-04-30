"""Track built static tiles (MBTiles) per collection."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.db.base import Base


class CollectionTiles(Base):
    """One row per collection: path to MBTiles file and when it was built."""

    __tablename__ = "collection_tiles"

    collection_id = Column(
        String,
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pmtiles_path = Column(Text, nullable=True)
    built_at = Column(DateTime(timezone=True), nullable=True)
    features_updated_at = Column(DateTime(timezone=True), nullable=True)
    minzoom = Column(Integer, nullable=True)
    maxzoom = Column(Integer, nullable=True)
    tiles_revision = Column(String(64), nullable=True)
