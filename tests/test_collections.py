import pytest


@pytest.mark.asyncio
async def test_create_get_and_delete_collection(client):
    payload = {
        "id": "buildings",
        "title": "Buildings",
        "description": "Building footprints",
        "extent": {
            "bbox": [[-180.0, -90.0, 180.0, 90.0]],
            "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        },
    }

    # Create collection (helper endpoint)
    resp = await client.post("/collections", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] == payload["id"]
    assert data["title"] == payload["title"]

    # List collections
    resp = await client.get("/collections")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "collections" in data
    assert len(data["collections"]) == 1
    assert data["collections"][0]["id"] == payload["id"]

    # Get collection by id
    resp = await client.get(f"/collections/{payload['id']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == payload["id"]
    assert data["extent"]["bbox"][0] == payload["extent"]["bbox"][0]

    # Delete collection
    resp = await client.delete(f"/collections/{payload['id']}")
    assert resp.status_code == 204, resp.text

    # Subsequent get should 404
    resp = await client.get(f"/collections/{payload['id']}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_put_replace_and_patch_collection(client):
    """PUT (replace) and PATCH (partial update) collection."""
    resp = await client.post(
        "/collections",
        json={
            "id": "zones",
            "title": "Zones",
            "description": "Original",
            "extent": {"bbox": [[0, 0, 1, 1]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
        },
    )
    assert resp.status_code == 201

    # PUT replace
    resp = await client.put(
        "/collections/zones",
        json={
            "title": "Zones Updated",
            "description": "Replaced",
            "extent": {"bbox": [[-1, -1, 2, 2]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Zones Updated"
    assert data["description"] == "Replaced"
    assert data["extent"]["bbox"][0] == [-1, -1, 2, 2]

    # PATCH partial (only description)
    resp = await client.patch("/collections/zones", json={"description": "Patched only"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Zones Updated"
    assert data["description"] == "Patched only"


@pytest.mark.asyncio
async def test_collection_bbox_dynamic_from_features(client):
    """Collection extent (bbox) is computed from features, not stored value."""
    # Create collection without extent
    resp = await client.post(
        "/collections",
        json={"id": "dynamic", "title": "Dynamic Bbox", "description": "No extent set"},
    )
    assert resp.status_code == 201
    assert resp.json().get("extent") is None

    # Add a feature with known bounds
    resp = await client.post(
        "/collections/dynamic/items",
        json={
            "collection_id": "dynamic",
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [10.5, 20.25]},
            "properties": {},
        },
    )
    assert resp.status_code == 201

    # GET collection: extent must be computed from feature (point → bbox is [10.5, 20.25, 10.5, 20.25] or similar)
    resp = await client.get("/collections/dynamic")
    assert resp.status_code == 200
    data = resp.json()
    assert data["extent"] is not None
    assert data["extent"]["bbox"] == [[10.5, 20.25, 10.5, 20.25]]

    # List collections: same collection must show dynamic extent
    resp = await client.get("/collections")
    assert resp.status_code == 200
    coll = next(c for c in resp.json()["collections"] if c["id"] == "dynamic")
    assert coll["extent"]["bbox"] == [[10.5, 20.25, 10.5, 20.25]]

