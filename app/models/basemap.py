"""Basemap: system-wide raster basemap definitions for maps."""

from sqlalchemy import Column, Integer, JSON, String

from app.db.base import Base


class Basemap(Base):
    """
    Raster basemap definition (OSM, Esri, Google, custom).
    Used across all map views; name, copyright, min_zoom, max_zoom respected in frontend.
    """

    __tablename__ = "basemaps"

    id: str = Column(String, primary_key=True, index=True)
    name: str = Column(String, nullable=False)
    copyright: str | None = Column(String, nullable=True)
    min_zoom: int = Column(Integer, nullable=False, default=0, server_default="0")
    max_zoom: int = Column(Integer, nullable=False, default=22, server_default="22")
    tiles: list = Column(JSON, nullable=False)
    labels: str | None = Column(String, nullable=True)
    sort_order: int = Column(Integer, nullable=False, default=0, server_default="0")
