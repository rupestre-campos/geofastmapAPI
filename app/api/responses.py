"""Custom response types for OGC API - Features."""

from fastapi.responses import JSONResponse


class GeoJSONResponse(JSONResponse):
    """JSON response with Content-Type: application/geo+json for OGC/GeoJSON clients (e.g. QGIS)."""

    media_type = "application/geo+json"
