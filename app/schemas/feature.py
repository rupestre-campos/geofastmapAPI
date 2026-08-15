from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_serializer
from pydantic.config import ConfigDict

from app.schemas.ogc import Link


class Geometry(BaseModel):
    """GeoJSON geometry. GeometryCollection uses ``geometries`` (no ``coordinates``)."""

    type: str
    coordinates: Any = None
    geometries: Any = None

    model_config = ConfigDict(extra="allow")

    @model_serializer(mode="wrap")
    def _drop_nulls(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class FeatureBase(BaseModel):
    type: str = Field(default="Feature")
    geometry: Geometry | None = None
    properties: dict[str, Any] | None = None


class FeatureCreate(FeatureBase):
    collection_id: str


class FeatureRead(FeatureBase):
    id: str
    collection_id: str
    created_at: datetime
    updated_at: datetime
    bbox: list[float] | None = Field(default=None, description="GeoJSON bbox [minx, miny, maxx, maxy] when geometry omitted for performance.")

    model_config = ConfigDict(from_attributes=True)


class FeatureGeoJSON(BaseModel):
    """Strict GeoJSON Feature for OGC single-item response (type, id, geometry, properties + links)."""

    type: str = Field(default="Feature", description="GeoJSON type.")
    id: str = Field(..., description="Feature identifier.")
    geometry: Geometry | None = None
    properties: dict[str, Any] | None = None
    links: list[Link] | None = Field(default=None, description="OGC links (self, collection).")


class FeatureReplace(BaseModel):
    """OGC Part 4: Full GeoJSON Feature for PUT (replace). id in body must match path."""

    type: str = Field(default="Feature")
    id: str = Field(..., description="Must match featureId in path.")
    geometry: Geometry | None = None
    properties: dict[str, Any] | None = None


class FeaturePatch(BaseModel):
    """OGC Part 4: Partial feature for PATCH (application/merge-patch+json). Only include fields to update."""

    geometry: Geometry | None = None
    properties: dict[str, Any] | None = None


class FeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[FeatureRead]
    bbox: list[float] | None = Field(default=None, description="GeoJSON bbox [minx, miny, maxx, maxy] of the features.")
    numberMatched: int | None = None
    numberReturned: int | None = None
    links: list[Link] = Field(default_factory=list, description="OGC links (e.g. self).")

