"""Runtime key/value settings editable from admin pages."""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.db.base import Base


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: str = Column(String(128), primary_key=True)
    value: str = Column(Text, nullable=False, default="", server_default="")
    updated_at: datetime = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
