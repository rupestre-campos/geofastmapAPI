import pytest


@pytest.mark.asyncio
async def test_create_get_and_delete_feature(client):
    # First create a collection
    collection_payload = {
        "id": "roads",
        "title": "Roads",
        "description": "Road network",
        "extent": {
            "bbox": [[-10.0, -10.0, 10.0, 10.0]],
            "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        },
    }
    resp = await client.post("/collections", json=collection_payload)
    assert resp.status_code == 201, resp.text

    feature_payload = {
        "collection_id": "roads",
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[0.0, 0.0], [1.0, 1.0]],
        },
        "properties": {"name": "Main St"},
    }

    # Create feature
    resp = await client.post("/collections/roads/items", json=feature_payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    feature_id = created["id"]
    assert created["collection_id"] == "roads"
    assert created["geometry"]["type"] == "LineString"

    # List features
    resp = await client.get("/collections/roads/items")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 1
    assert data["features"][0]["id"] == feature_id

    # Get single feature
    resp = await client.get(f"/collections/roads/items/{feature_id}")
    assert resp.status_code == 200, resp.text
    feature = resp.json()
    assert feature["id"] == feature_id
    assert feature["properties"]["name"] == "Main St"

    # Delete feature
    resp = await client.delete(f"/collections/roads/items/{feature_id}")
    assert resp.status_code == 204, resp.text

    # Ensure feature is gone
    resp = await client.get(f"/collections/roads/items/{feature_id}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_put_replace_and_patch_feature(client):
    """OGC Part 4: PUT (replace) and PATCH (partial update) feature."""
    resp = await client.post(
        "/collections",
        json={
            "id": "parcels",
            "title": "Parcels",
            "extent": {"bbox": [[0, 0, 10, 10]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
        },
    )
    assert resp.status_code == 201

    # Create feature
    resp = await client.post(
        "/collections/parcels/items",
        json={
            "collection_id": "parcels",
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            "properties": {"name": "A", "area": 100},
        },
    )
    assert resp.status_code == 201
    feature_id = resp.json()["id"]

    # PUT replace (full)
    replace_payload = {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "Point", "coordinates": [3.0, 4.0]},
        "properties": {"name": "B", "area": 200},
    }
    resp = await client.put(f"/collections/parcels/items/{feature_id}", json=replace_payload)
    assert resp.status_code == 204

    resp = await client.get(f"/collections/parcels/items/{feature_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["geometry"]["coordinates"] == [3.0, 4.0]
    assert data["properties"]["name"] == "B"
    assert data["properties"]["area"] == 200

    # PATCH partial (merge properties, update geometry)
    patch_payload = {"properties": {"area": 300, "extra": "x"}, "geometry": {"type": "Point", "coordinates": [5.0, 6.0]}}
    resp = await client.patch(f"/collections/parcels/items/{feature_id}", json=patch_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["geometry"]["coordinates"] == [5.0, 6.0]
    assert data["properties"]["name"] == "B"
    assert data["properties"]["area"] == 300
    assert data["properties"]["extra"] == "x"

    # PUT with id mismatch
    resp = await client.put(f"/collections/parcels/items/{feature_id}", json={**replace_payload, "id": "other"})
    assert resp.status_code == 400

