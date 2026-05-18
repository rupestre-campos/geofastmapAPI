from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ogc import Link


def default_raster_style_spec() -> dict:
    """Continuous / analytic raster style (Titiler bidx, rescale, colormap_name, expression)."""
    return {
        "style_type": "continuous",
        "asset": None,
        "assets": None,
        "bidx": None,
        "rescale": None,
        "colormap_name": None,
        "expression": None,
    }


# Classification example (style_type=classification):
# {
#   "style_type": "classification",
#   "asset": "<feature_id>",
#   "bidx": ["1"],
#   "colormap": {"0": "#000000", "1": "#228822"},
#   "colormap_type": "explicit",
#   "classes": [{"value": "0", "name": "nodata", "color": "#000000"}, ...],
#   "nodata": 0,  # optional Titiler nodata override (query param)
# }


class RasterStyleCreate(BaseModel):
    id: str
    title: str | None = None
    style_spec: dict = Field(default_factory=default_raster_style_spec)
    set_default: bool = False
    visibility: str | None = None


class RasterStyleReplace(BaseModel):
    title: str | None = None
    style_spec: dict = Field(default_factory=default_raster_style_spec)


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
