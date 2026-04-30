from fastapi import HTTPException

from app.models.collection import COLLECTION_TYPE_RASTER, COLLECTION_TYPE_VECTOR
from app.models.resource_share import raster_style_resource_id
from app.schemas.collection import CollectionCreate
from app.services.collection_type_guard import ensure_raster_collection, ensure_vector_collection
from app.services.collection_tiles_revision import compute_collection_tiles_revision


class _C:
    def __init__(self, collection_type: str):
        self.collection_type = collection_type


def test_collection_create_defaults_to_vector_type():
    c = CollectionCreate(id="abc")
    assert c.collection_type == COLLECTION_TYPE_VECTOR


def test_collection_type_guards():
    ensure_vector_collection(_C(COLLECTION_TYPE_VECTOR))
    ensure_raster_collection(_C(COLLECTION_TYPE_RASTER))
    try:
        ensure_vector_collection(_C(COLLECTION_TYPE_RASTER))
        assert False, "expected HTTPException"
    except HTTPException:
        pass
    try:
        ensure_raster_collection(_C(COLLECTION_TYPE_VECTOR))
        assert False, "expected HTTPException"
    except HTTPException:
        pass


def test_raster_style_resource_id_prefix():
    assert raster_style_resource_id("c1", "s1") == "raster:c1:s1"


def test_tiles_revision_none_for_missing_file():
    assert compute_collection_tiles_revision("c1", "/tmp/does-not-exist.tif") is None
