import pytest


@pytest.mark.asyncio
async def test_create_collection_duplicate_id(client):
    payload = {
        "id": "dupe",
        "title": "Dupe",
        "description": "First instance",
        "extent": {
            "bbox": [[0.0, 0.0, 1.0, 1.0]],
            "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        },
    }

    resp = await client.post("/collections", json=payload)
    assert resp.status_code == 201, resp.text

    # Second create with same id should fail with 400
    resp = await client.post("/collections", json=payload)
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert data["detail"] == "Collection with this id already exists"


@pytest.mark.asyncio
async def test_get_and_delete_nonexistent_collection(client):
    # Get unknown collection
    resp = await client.get("/collections/unknown")
    assert resp.status_code == 404, resp.text

    # Delete unknown collection
    resp = await client.delete("/collections/unknown")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_items_errors_for_missing_collection_and_feature(client):
    # List items for non-existent collection
    resp = await client.get("/collections/missing/items")
    assert resp.status_code == 404, resp.text

    # Get non-existent feature in non-existent collection
    resp = await client.get("/collections/missing/items/nonexistent")
    assert resp.status_code == 404, resp.text

    # Delete non-existent feature in non-existent collection
    resp = await client.delete("/collections/missing/items/nonexistent")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_create_item_mismatched_collection_id_and_missing_collection(client):
    # First create a collection
    collection_payload = {
        "id": "lakes",
        "title": "Lakes",
        "description": "Water bodies",
        "extent": {
            "bbox": [[-5.0, -5.0, 5.0, 5.0]],
            "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        },
    }
    resp = await client.post("/collections", json=collection_payload)
    assert resp.status_code == 201, resp.text

    # Mismatched collection_id in body vs path
    feature_payload = {
        "collection_id": "other",
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [0.0, 0.0],
        },
        "properties": {"name": "Lake"},
    }
    resp = await client.post("/collections/lakes/items", json=feature_payload)
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert data["detail"] == "collection_id in path and body must match"

    # Attempt to create item for non-existent collection
    feature_payload["collection_id"] = "missing"
    resp = await client.post("/collections/missing/items", json=feature_payload)
    assert resp.status_code == 404, resp.text
    data = resp.json()
    assert data["detail"] == "Collection not found"

