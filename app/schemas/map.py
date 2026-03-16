"""Schemas for user-created maps (gallery, create, edit, view)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class MapLayer(BaseModel):
    """Single layer in a map definition. Style is stored with the map (not linked to collection or public styles)."""

    collection_id: str = Field(..., description="Collection id")
    color: str | None = Field(None, description="Fill/line color hex (legacy); use style_spec when present")
    order: int = Field(0, description="Display order (0 = bottom)")
    style_spec: dict | None = Field(
        None,
        description="Map-layer style override: fillColor, lineColor, fillOpacity, lineOpacity, lineWidth, linePattern, fillEnabled, lineEnabled, pointEnabled, pointColor, pointSize, pointOpacity, pointIcon; and optionally fillOpacityZoom, lineWidthZoom, lineOpacityZoom, pointSizeZoom, pointOpacityZoom (zoom breakpoints). Stored with the map, not with the collection.",
    )
    popup: bool = Field(False, description="Show popup on click for this layer")
    popup_id_property: str | None = Field(None, description="Property name to show as the identifier in popups (e.g. name, code). When unset, feature id is used.")
    tiles_url: str | None = Field(None, description="When set, use this vector tile URL (dynamic tiles) instead of TileJSON")
    layer_id: str | None = Field(None, description="Optional unique id for this layer when using tiles_url (e.g. items-{coll}, item-{id})")


class MapDefinition(BaseModel):
    """Map definition JSON (layers, optional initial bbox and basemap)."""

    layers: list[MapLayer] = Field(default_factory=list, description="Layers in display order")
    bbox: list[float] | None = Field(None, description="Initial map extent [minx, miny, maxx, maxy] WGS84")
    basemap: str | None = Field(None, description="Initial basemap key e.g. osm, satellite")


class MapCreate(BaseModel):
    """Body for POST /maps."""

    name: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(None, max_length=10000)
    thumbnail: str | None = Field(None, max_length=2000)
    definition: MapDefinition = Field(default_factory=MapDefinition)


class MapUpdate(BaseModel):
    """Body for PUT /maps/{id}. Omitted fields are left unchanged."""

    name: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = Field(None, max_length=10000)
    thumbnail: str | None = Field(None, max_length=2000)
    definition: MapDefinition | None = None


class MapRead(BaseModel):
    """Map response (JSON)."""

    id: UUID
    name: str
    description: str | None
    thumbnail: str | None
    definition: dict
    created_at: str
    updated_at: str
