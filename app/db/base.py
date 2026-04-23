from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


# Import models here so Alembic can discover them via Base.metadata
from app.models import (
    api_landing,
    basemap,
    collection,
    collection_tiles,
    feature,
    map as map_model,
    observability,
    runtime_setting,
    resource_share,
    style,
    user,
)  # noqa: E402,F401

