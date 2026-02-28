"""OGC API - Features Part 1: Core schemas (landing page, conformance, links)."""

from typing import Any

from pydantic import BaseModel, Field


class Link(BaseModel):
    """RFC 8288 Web Link for OGC API resources."""

    href: str = Field(..., description="URI of the target resource.")
    rel: str = Field(..., description="Link relation type.")
    type: str | None = Field(default=None, description="Media type of the target.")
    title: str | None = Field(default=None, description="Human-readable title.")


class LandingPage(BaseModel):
    """OGC API - Features landing page (root resource)."""

    title: str = Field(..., description="Title of the API.")
    description: str = Field(default="", description="Brief description of the API.")
    links: list[Link] = Field(default_factory=list, description="Navigation links.")


class Conformance(BaseModel):
    """OGC API - Features conformance declaration."""

    conformsTo: list[str] = Field(
        ...,
        description="List of URIs of conformance classes this API implements.",
    )


# Standard conformance class URIs (OGC API - Features Part 1)
CONFORMANCE_CORE = "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core"
CONFORMANCE_GEOJSON = "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson"
CONFORMANCE_OAS30 = "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30"

# OGC API - Features Part 4: Create, Replace, Update and Delete (draft 20-002)
# Part 4 is still in draft; these are the conformance URIs from the draft spec.
# Declaring them allows clients to discover that create/replace/update/delete are supported.
CONFORMANCE_P4_CREATE_REPLACE_DELETE = "http://www.opengis.net/spec/ogcapi-features-4/1.0-draft/conf/create-replace-delete"
CONFORMANCE_P4_UPDATE = "http://www.opengis.net/spec/ogcapi-features-4/1.0-draft/conf/update"

# OGC API - Tiles 1.0 (vector tiles: TileJSON, static MBTiles, and dynamic tiles)
CONFORMANCE_TILES_CORE = "http://www.opengis.net/spec/ogcapi-tiles-1/1.0/conf/core"
CONFORMANCE_TILES_GEODATA = "http://www.opengis.net/spec/ogcapi-tiles-1/1.0/conf/geodata"
