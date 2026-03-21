from datetime import datetime

from pydantic import BaseModel, Field, model_validator
from pydantic.config import ConfigDict

from app.schemas.ogc import Link

# Valid geographic range for extent bbox (WGS84): lon [-180, 180], lat [-90, 90]
LON_MIN, LON_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0


def clamp_bbox(minx: float, miny: float, maxx: float, maxy: float) -> list[float]:
    """Clamp bbox to valid WGS84 range so it displays correctly on maps."""
    return [
        max(LON_MIN, min(LON_MAX, minx)),
        max(LAT_MIN, min(LAT_MAX, miny)),
        max(LON_MIN, min(LON_MAX, maxx)),
        max(LAT_MIN, min(LAT_MAX, maxy)),
    ]


class Extent(BaseModel):
    bbox: list[list[float]] = Field(
        ...,
        description="Array of bounding boxes represented as [minx, miny, maxx, maxy].",
    )
    crs: str | None = Field(
        default="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        description="Coordinate reference system of the extent.",
    )

    @model_validator(mode="after")
    def clamp_bbox_to_valid_range(self) -> "Extent":
        """Clamp bbox to valid lon/lat range so extent displays correctly on maps."""
        clamped = []
        for box in self.bbox:
            if len(box) >= 4:
                clamped.append(
                    clamp_bbox(
                        float(box[0]),
                        float(box[1]),
                        float(box[2]),
                        float(box[3]),
                    )
                )
            else:
                clamped.append(list(box))
        return self.model_copy(update={"bbox": clamped})


class ExtentRecomputeResponse(BaseModel):
    """Response for POST /collections/{id}/extent/recompute."""

    extent: Extent | None = Field(
        default=None,
        description="Computed extent from feature geometries, or null if no features with geometry.",
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
    visibility: str | None = None  # private | logged | public
    viewer_can_edit: bool | None = None  # when True, everyone who can view can edit
    editing_enabled: bool | None = Field(
        default=None,
        description="When False, only administrators may edit this collection and its features. Only admins may change this flag.",
    )


class CollectionRead(CollectionBase):
    created_at: datetime
    updated_at: datetime
    feature_count: int | None = Field(
        default=None,
        description="Total number of features in this collection (cached).",
    )
    editing_enabled: bool = Field(
        default=True,
        description="When False, only administrators may edit this collection and its features.",
    )
    links: list[Link] | None = Field(default=None, description="OGC links (self, items).")

    model_config = ConfigDict(from_attributes=True)


class CollectionsList(BaseModel):
    collections: list[CollectionRead]
    links: list[Link] = Field(default_factory=list, description="OGC links (e.g. self).")

