from datetime import datetime
from typing import Any

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


class CompositeMember(BaseModel):
    collection_id: str = Field(..., description="Member vector collection id")


class CollectionBase(BaseModel):
    id: str = Field(..., description="Identifier of the collection.")
    title: str | None = None
    description: str | None = None
    extent: Extent | None = None
    stac_source: dict[str, Any] | None = Field(
        default=None,
        description="Optional link to external STAC: catalog_id, collection_id.",
    )
    raster_settings: dict[str, Any] | None = Field(
        default=None,
        description="Raster collection settings, e.g. is_dem and dem_encoding.",
    )
    collection_type: str = Field(default="vector", description="Collection type: vector, raster, or composite.")
    composite_members: list[CompositeMember] | None = Field(
        default=None,
        description="For composite collections: ordered member vector collection ids.",
    )


class CollectionCreate(CollectionBase):
    pass


class CollectionReplace(BaseModel):
    """Full collection metadata for PUT (replace). id is fixed by path."""

    title: str | None = None
    description: str | None = None
    extent: Extent | None = None
    stac_source: dict[str, Any] | None = None
    raster_settings: dict[str, Any] | None = None
    collection_type: str = "vector"
    composite_members: list[CompositeMember] | None = None


class CollectionPatch(BaseModel):
    """Partial collection for PATCH (merge-patch+json). Only include fields to update."""

    title: str | None = None
    description: str | None = None
    extent: Extent | None = None
    stac_source: dict[str, Any] | None = None
    raster_settings: dict[str, Any] | None = None
    visibility: str | None = None  # private | logged | public
    viewer_can_edit: bool | None = None  # when True, everyone who can view can edit
    collection_type: str | None = None  # vector | raster | composite
    composite_members: list[CompositeMember] | None = None


class CompositeMemberStatus(BaseModel):
    collection_id: str
    title: str | None = None
    feature_count: int = 0
    has_static_tiles: bool = False
    tiles_revision: str | None = None
    minzoom: int | None = None
    maxzoom: int | None = None
    built_at: str | None = None


class CollectionRead(CollectionBase):
    created_at: datetime
    updated_at: datetime
    feature_count: int | None = Field(
        default=None,
        description="Total number of features in this collection (cached).",
    )
    features_last_updated_at: datetime | None = Field(
        default=None,
        description="Latest feature row update in this collection (geometries and attributes). "
        "Stored on the collection row and kept in sync by database triggers; distinct from updated_at (metadata only).",
    )
    member_status: list[CompositeMemberStatus] | None = Field(
        default=None,
        description="For composite collections: per-member tile and feature status.",
    )
    links: list[Link] | None = Field(default=None, description="OGC links (self, items).")

    model_config = ConfigDict(from_attributes=True)


class CollectionsList(BaseModel):
    collections: list[CollectionRead]
    links: list[Link] = Field(default_factory=list, description="OGC links (e.g. self).")

