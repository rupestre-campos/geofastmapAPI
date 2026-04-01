"""Admin-registered STAC API roots for federated Item Search."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, JSON, String, Text

from app.db.base import Base


class StacCatalog(Base):
    __tablename__ = "stac_catalogs"

    id: str = Column(String(64), primary_key=True, index=True)
    title: str = Column(String(512), nullable=False)
    stac_api_root_url: str = Column(Text, nullable=False)
    enabled: bool = Column(Boolean, nullable=False, default=True, server_default="true")
    notes: str | None = Column(Text, nullable=True)
    default_collections: dict | list | None = Column(JSON, nullable=True)

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
