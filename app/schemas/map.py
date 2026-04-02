"""Schemas for user-created maps (gallery, create, edit, view)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class MapLayer(BaseModel):
    """Single layer in a map definition. Style is stored with the map (not linked to collection or public styles)."""

    collection_id: str = Field(
        ...,
        description="GeoFast collection id, or placeholder _stac for STAC Titiler raster layers",
    )
    color: str | None = Field(None, description="Fill/line color hex (legacy); use style_spec when present")
    order: int = Field(0, description="Display order (0 = bottom)")
    style_spec: dict | None = Field(
        None,
        description="Map-layer style override: fillColor, lineColor, fillOpacity, lineOpacity, lineWidth, linePattern, fillEnabled, lineEnabled, pointEnabled, pointColor, pointSize, pointOpacity, pointIcon; rasterOpacity for raster_tiles; and optionally fillOpacityZoom, lineWidthZoom, lineOpacityZoom, pointSizeZoom, pointOpacityZoom (zoom breakpoints). Stored with the map, not with the collection.",
    )
    popup: bool = Field(False, description="Show popup on click for this layer")
    popup_id_property: str | None = Field(None, description="Property name to show as the identifier in popups (e.g. name, code). When unset, feature id is used.")
    tiles_url: str | None = Field(
        None,
        description="When set: vector tile URL (dynamic PBF) or absolute raster tile URL (PNG) when raster_tiles is true",
    )
    layer_id: str | None = Field(None, description="Optional unique id for this layer when using tiles_url (e.g. items-{coll}, item-{id})")
    raster_tiles: bool = Field(False, description="When true, tiles_url is a MapLibre raster source (e.g. Titiler PNG tiles)")
    stac_catalog_id: str | None = Field(None, description="STAC catalog id (when raster_tiles)")
    stac_collection_id: str | None = Field(None, description="STAC collection id on that catalog")
    stac_item_id: str | None = Field(None, description="STAC item id")
    stac_viewer_path: str | None = Field(
        None,
        description="URL path to STAC HTML viewer without query (e.g. /stac/catalogs/.../items/...)",
    )
    stac_viewer_query: str | None = Field(
        None,
        description="Query string for viewer (no leading ?), restoring render params e.g. f=html&asset=...",
    )


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
    visibility: str | None = None  # private | logged | public
    viewer_can_edit: bool | None = None  # when True, everyone who can view can edit


class MapRead(BaseModel):
    """Map response (JSON)."""

    id: UUID
    name: str
    description: str | None
    thumbnail: str | None
    definition: dict
    created_at: str
    updated_at: str
