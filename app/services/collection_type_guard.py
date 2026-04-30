from __future__ import annotations

from fastapi import HTTPException, status

from app.models.collection import COLLECTION_TYPE_RASTER, COLLECTION_TYPE_VECTOR


def ensure_vector_collection(collection) -> None:
    ctype = getattr(collection, "collection_type", COLLECTION_TYPE_VECTOR)
    if ctype != COLLECTION_TYPE_VECTOR:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint is only available for vector collections.",
        )


def ensure_raster_collection(collection) -> None:
    ctype = getattr(collection, "collection_type", COLLECTION_TYPE_VECTOR)
    if ctype != COLLECTION_TYPE_RASTER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint is only available for raster collections.",
        )
