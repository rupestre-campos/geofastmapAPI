from datetime import datetime

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.schemas.ogc import Link


class Extent(BaseModel):
    bbox: list[list[float]] = Field(
        ...,
        description="Array of bounding boxes represented as [minx, miny, maxx, maxy].",
    )
    crs: str | None = Field(
        default="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        description="Coordinate reference system of the extent.",
    )


class CollectionBase(BaseModel):
    id: str = Field(..., description="Identifier of the collection.")
    title: str | None = None
    description: str | None = None
    extent: Extent | None = None


class CollectionCreate(CollectionBase):
    pass


class CollectionReplace(BaseModel):
    """Full collection metadata for PUT (replace). id is fixed by path."""

    title: str | None = None
    description: str | None = None
    extent: Extent | None = None


class CollectionPatch(BaseModel):
    """Partial collection for PATCH (merge-patch+json). Only include fields to update."""

    title: str | None = None
    description: str | None = None
    extent: Extent | None = None


class CollectionRead(CollectionBase):
    created_at: datetime
    updated_at: datetime
    links: list[Link] | None = Field(default=None, description="OGC links (self, items).")

    model_config = ConfigDict(from_attributes=True)


class CollectionsList(BaseModel):
    collections: list[CollectionRead]
    links: list[Link] = Field(default_factory=list, description="OGC links (e.g. self).")

