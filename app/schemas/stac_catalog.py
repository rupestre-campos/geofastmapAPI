from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StacCatalogCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    stac_api_root_url: str = Field(..., description="STAC API root (POST {root}/search for Item Search).")
    enabled: bool = True
    notes: str | None = None
    default_collections: list[str] | dict[str, Any] | None = Field(
        default=None,
        description="Optional allowlist: list of collection ids forwarded when body omits collections.",
    )
    id: str | None = Field(default=None, description="Optional id; server generates UUID if omitted.")


class StacCatalogUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    stac_api_root_url: str | None = None
    enabled: bool | None = None
    notes: str | None = None
    default_collections: list[str] | dict[str, Any] | None = None


class StacCatalogRead(BaseModel):
    id: str
    title: str
    stac_api_root_url: str
    enabled: bool
    notes: str | None = None
    default_collections: list | dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
