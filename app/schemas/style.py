"""OGC API - Styles: style metadata and spec for vector tile layers."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ogc import Link


def default_style_spec() -> dict:
    return {
        "fillColor": "#58a6ff",
        "lineColor": "#58a6ff",
        "fillOpacity": 0.6,
        "lineOpacity": 1.0,
        "lineWidth": 1.0,
        "linePattern": "solid",
        "fillEnabled": True,
        "lineEnabled": True,
        "pointEnabled": True,
        "pointColor": "#58a6ff",
        "pointOpacity": 1.0,
        "pointSize": 8.0,
        "pointIcon": "circle",
    }


class StyleSpec(BaseModel):
    """Vector tile paint spec (client-side)."""

    fillColor: str = Field(default="#58a6ff", description="Fill color hex")
    lineColor: str = Field(default="#58a6ff", description="Line color hex")
    fillOpacity: float = Field(default=0.6, ge=0, le=1)
    lineOpacity: float = Field(default=1.0, ge=0, le=1)
    lineWidth: float = Field(default=1.0, ge=0.5, le=20)
    linePattern: str = Field(default="solid", description="solid | dashed | dotted")
    fillEnabled: bool = Field(default=True, description="Show polygon fill")
    lineEnabled: bool = Field(default=True, description="Show line stroke")
    pointEnabled: bool = Field(default=True, description="Show points/circles")
    pointColor: str = Field(default="#58a6ff", description="Point/circle color hex")
    pointSize: float = Field(default=8.0, ge=1, le=40, description="Point radius in pixels")
    pointIcon: str = Field(default="circle", description="circle | pin | marker")


class StyleBase(BaseModel):
    id: str = Field(..., description="Style identifier (slug)")
    title: str | None = Field(default=None, description="Human-readable title")
    style_spec: dict = Field(default_factory=default_style_spec, description="Paint spec for vector layer")


class StyleCreate(StyleBase):
    set_default: bool = Field(default=False, description="Set as default style for this collection")


class StyleReplace(BaseModel):
    title: str | None = None
    style_spec: dict = Field(default_factory=default_style_spec)
    set_default: bool = False


class StylePatch(BaseModel):
    title: str | None = None
    style_spec: dict | None = None
    set_default: bool | None = None


class StyleRead(BaseModel):
    id: str
    title: str | None
    collection_id: str | None = Field(default=None, description="Empty or null = public style")
    is_default: bool
    style_spec: dict
    created_at: datetime
    updated_at: datetime
    links: list[Link] | None = None


class StyleList(BaseModel):
    styles: list[StyleRead]
    links: list[Link] = Field(default_factory=list)
