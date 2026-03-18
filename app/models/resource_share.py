"""Sharing: grant viewer/editor access to a resource by username."""

from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.db.base import Base


# Resource type and id identify the resource. For styles use "collection_id:style_id".
RESOURCE_TYPE_COLLECTION = "collection"
RESOURCE_TYPE_MAP = "map"
RESOURCE_TYPE_STYLE = "style"

ROLE_VIEWER = "viewer"
ROLE_EDITOR = "editor"


def style_resource_id(collection_id: str, style_id: str) -> str:
    return f"{collection_id}:{style_id}"


class ResourceShare(Base):
    __tablename__ = "resource_shares"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    resource_type: str = Column(String(32), nullable=False, index=True)
    resource_id: str = Column(String(512), nullable=False, index=True)
    username: str = Column(String(255), nullable=False, index=True)
    role: str = Column(String(32), nullable=False)  # viewer | editor

    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "username",
            name="uq_resource_share_resource_username",
        ),
    )
