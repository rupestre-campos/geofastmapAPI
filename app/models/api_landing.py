"""API landing page content: title, description, contact. Single row, user-editable without redeploy."""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.db.base import Base

# Single row id
API_LANDING_ID = "default"


class ApiLanding(Base):
    """Stored copy of API landing page text (title, description, contact)."""

    __tablename__ = "api_landing"

    id: str = Column(String, primary_key=True)  # API_LANDING_ID
    title: str = Column(String, nullable=False)
    description: str | None = Column(Text, nullable=True)
    contact: str | None = Column(Text, nullable=True)

    updated_at: datetime = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
