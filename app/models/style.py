"""Layer style: public (no collection) or collection-specific. OGC API - Styles style."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String

from app.db.base import Base


class Style(Base):
    """
    Vector tile style: symbolizing instructions for a layer.
    - collection_id is '' => public style (reusable with any layer).
    - collection_id set => style for that collection only (shareable later).
    - is_default: for collection styles, one per collection can be default.
    - style_spec: JSON with fillColor, lineColor, fillOpacity, lineOpacity, lineWidth, linePattern, fillEnabled, lineEnabled, pointEnabled, pointColor, pointSize, pointIcon, etc.
    """

    __tablename__ = "styles"

    # Composite PK (collection_id, id): use collection_id='' for public (global) styles.
    collection_id: str = Column(String, primary_key=True, default="")
    id: str = Column(String, primary_key=True, index=True)
    title: str | None = Column(String, nullable=True)
    is_default: bool = Column(Boolean, nullable=False, default=False, server_default="false")
    style_spec: dict = Column(JSON, nullable=False)  # fillColor, lineColor, fillOpacity, etc.
    owner_id: int | None = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    visibility: str = Column(String(32), nullable=False, default="private", server_default="private")
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
