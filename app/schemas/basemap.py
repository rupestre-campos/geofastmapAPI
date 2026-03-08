"""Basemap API schemas."""

from pydantic import BaseModel, Field


class BasemapBase(BaseModel):
    id: str = Field(..., description="Basemap identifier (slug)")
    name: str = Field(..., description="Display name in basemap selector")
    copyright: str | None = Field(default=None, description="Attribution / copyright text")
    min_zoom: int = Field(default=0, ge=0, le=24, description="Minimum zoom level")
    max_zoom: int = Field(default=22, ge=0, le=24, description="Maximum zoom level")
    tiles: list[str] = Field(..., description="Tile URL templates with {z},{x},{y}")
    labels: str | None = Field(default=None, description="Optional overlay tile URL (e.g. hybrid labels)")
    sort_order: int = Field(default=0, description="Order in list and selector")


class BasemapCreate(BasemapBase):
    pass


class BasemapUpdate(BaseModel):
    name: str | None = None
    copyright: str | None = None
    min_zoom: int | None = None
    max_zoom: int | None = None
    tiles: list[str] | None = None
    labels: str | None = None
    sort_order: int | None = None


class BasemapRead(BasemapBase):
    pass


class BasemapList(BaseModel):
    basemaps: list[BasemapRead]
