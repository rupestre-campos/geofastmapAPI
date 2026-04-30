from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ogc import Link


def default_raster_style_spec() -> dict:
    return {
        "asset": None,
        "assets": None,
        "bidx": None,
        "rescale": None,
        "colormap_name": None,
        "expression": None,
    }


class RasterStyleCreate(BaseModel):
    id: str
    title: str | None = None
    style_spec: dict = Field(default_factory=default_raster_style_spec)
    set_default: bool = False


class RasterStylePatch(BaseModel):
    title: str | None = None
    style_spec: dict | None = None
    set_default: bool | None = None
    visibility: str | None = None


class RasterStyleRead(BaseModel):
    id: str
    title: str | None
    collection_id: str
    is_default: bool
    style_spec: dict
    visibility: str | None = None
    created_at: datetime
    updated_at: datetime
    links: list[Link] | None = None


class RasterStyleList(BaseModel):
    styles: list[RasterStyleRead]
    links: list[Link] = Field(default_factory=list)
