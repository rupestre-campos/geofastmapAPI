from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, String, Text

from app.db.base import Base


class Collection(Base):
    """OGC API - Features collection metadata."""

    __tablename__ = "collections"

    id: str = Column(String, primary_key=True, index=True)
    title: str | None = Column(String, nullable=True)
    description: str | None = Column(Text, nullable=True)
    extent: dict | None = Column(JSON, nullable=True)

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

