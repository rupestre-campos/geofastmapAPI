import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.collection import COLLECTION_TYPE_RASTER, VISIBILITY_PUBLIC, Collection
from app.models.feature import Feature
from app.utils.geo import geojson_to_wkt_element
from tests.fake_db import FakeCollectionTilesCrud


@pytest.mark.asyncio
async def test_raster_collection_items_list_ok(client, store):
    """Raster collections can list items (GeoJSON + HTML) without vector-only guard."""
    now = datetime.now(timezone.utc)
    cid = "raster_items_test_c"
    store.collections[cid] = Collection(
        id=cid,
        title="Raster C",
        description=None,
        extent={"bbox": [[0.0, 0.0, 1.0, 1.0]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
        stac_source=None,
        raster_settings=None,
        feature_count=1,
        owner_id=None,
        visibility=VISIBILITY_PUBLIC,
        viewer_can_edit=False,
        collection_type=COLLECTION_TYPE_RASTER,
        created_at=now,
        updated_at=now,
    )
    fid = "raster-feat-1"
    store.features[(cid, fid)] = Feature(
        id=fid,
        collection_id=cid,
        part_index=0,
        geometry=geojson_to_wkt_element(
            {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]}
        ),
        properties={"raster": {"meta": {"bounds": [0.0, 0.0, 1.0, 1.0]}}},
        created_at=now,
        updated_at=now,
    )

    fake_tiles = FakeCollectionTilesCrud()
    with patch("app.api.routes.items.styles_crud") as mock_styles, patch(
        "app.api.routes.items.tiles_crud", fake_tiles
    ):
        mock_styles.get_default_style = AsyncMock(return_value=None)
        resp = await client.get(f"/collections/{cid}/items", headers={"Accept": "application/geo+json"})
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "vector collections" not in body.lower()
        data = resp.json()
        assert data.get("type") == "FeatureCollection"
        assert len(data.get("features", [])) == 1

        resp_html = await client.get(f"/collections/{cid}/items", params={"f": "html"})
        assert resp_html.status_code == 200, resp_html.text
        assert "vector collections" not in resp_html.text.lower()
        assert "results-geojson" in resp_html.text or "isRaster" in resp_html.text


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
async def test_items_data_download_geojsonl(client):
    resp = await client.post(
        "/collections",
        json={
            "id": "export",
            "title": "Export",
            "extent": {"bbox": [[0, 0, 10, 10]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
        },
    )
    assert resp.status_code == 201
    for name, kind in [("Alpha", "road"), ("Beta", "river")]:
        resp = await client.post(
            "/collections/export/items",
            json={
                "collection_id": "export",
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                "properties": {"name": name, "kind": kind},
            },
        )
        assert resp.status_code == 201

    resp = await client.get("/collections/export/items/data", params={"name": "Alpha", "properties": "name"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert 'filename="export.geojsonl"' in resp.headers.get("content-disposition", "")

    lines = [line for line in resp.text.splitlines() if line.strip()]
    assert len(lines) == 1
    feature = json.loads(lines[0])
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Point"
    assert feature["properties"]["name"] == "Alpha"
    assert feature["properties"]["id"] == feature["id"]
    assert "kind" not in feature["properties"]


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


@pytest.mark.asyncio
async def test_items_pagination_bbox_datetime_sort(client):
    """OGC query params: limit, offset, bbox, datetime, sortby, sortdesc."""
    resp = await client.post(
        "/collections",
        json={"id": "paginated", "title": "P", "extent": {"bbox": [[0, 0, 10, 10]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
    )
    assert resp.status_code == 201
    # Add two features
    for i in range(2):
        resp = await client.post(
            "/collections/paginated/items",
            json={
                "collection_id": "paginated",
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [1.0 + i, 2.0]},
                "properties": {"name": f"F{i}", "order": 10 - i},
            },
        )
        assert resp.status_code == 201
    # Pagination: limit=1, offset=0
    resp = await client.get("/collections/paginated/items?limit=1&offset=0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] == 2
    assert data["numberReturned"] == 1
    assert len(data["features"]) == 1
    assert "next" in [l["rel"] for l in data["links"]]
    # sortby property
    resp = await client.get("/collections/paginated/items?sortby=order&sortdesc=true")
    assert resp.status_code == 200
    data2 = resp.json()
    assert data2["numberMatched"] == 2
    # bbox filter (point 1,2 and 2,2 are inside 0,0,3,3)
    resp = await client.get("/collections/paginated/items?bbox=0,0,3,3")
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] >= 1
    assert data["numberReturned"] >= 1


@pytest.mark.asyncio
async def test_items_attribute_filter_and_selection(client):
    """OGC attribute filtering (name=value, * partial) and attribute selection (properties=)."""
    resp = await client.post(
        "/collections",
        json={"id": "attrs", "title": "A", "extent": {"bbox": [[0, 0, 10, 10]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
    )
    assert resp.status_code == 201
    for name, order in [("Alpha", 1), ("Beta", 2), ("Alpine", 3)]:
        resp = await client.post(
            "/collections/attrs/items",
            json={
                "collection_id": "attrs",
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {"name": name, "order": order},
            },
        )
        assert resp.status_code == 201
    # Attribute filter: name=Alpha (exact)
    resp = await client.get("/collections/attrs/items?name=Alpha")
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] == 1
    assert data["features"][0]["properties"]["name"] == "Alpha"
    # Partial: *pha (ends with pha) -> Alpha, Alpine
    resp = await client.get("/collections/attrs/items?name=*pha")
    assert resp.status_code == 200
    data2 = resp.json()
    assert data2["numberMatched"] >= 1
    assert all("pha" in (f["properties"].get("name") or "") for f in data2["features"])
    # Attribute selection: only return "order"
    resp = await client.get("/collections/attrs/items?properties=order")
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] == 3
    for feat in data["features"]:
        assert "order" in feat["properties"]
        assert "name" not in feat["properties"]


@pytest.mark.asyncio
async def test_items_structured_filter_and_fulltext(client):
    """Structured filter=key:op:value and full-text q= across all properties."""
    resp = await client.post(
        "/collections",
        json={"id": "flt", "title": "F", "extent": {"bbox": [[0, 0, 1, 1]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
    )
    assert resp.status_code == 201
    for car_code, count in [("GO-5206206-69BE7A7C3DD542DCAF95F50D32BD9154", 10), ("GO-OTHER", 5), ("GO-5206206-69BE7A7C3DD542DCAF95F50D32BD9154", 20)]:
        resp = await client.post(
            "/collections/flt/items",
            json={
                "collection_id": "flt",
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {"car_code": car_code, "count": count},
            },
        )
        assert resp.status_code == 201
    # Structured filter: car_code eq value
    resp = await client.get(
        "/collections/flt/items",
        params={"filter": "car_code:eq:GO-5206206-69BE7A7C3DD542DCAF95F50D32BD9154"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] == 2
    assert all(f["properties"]["car_code"] == "GO-5206206-69BE7A7C3DD542DCAF95F50D32BD9154" for f in data["features"])
    # Combined: filter count gte 15
    resp = await client.get(
        "/collections/flt/items",
        params=[
            ("filter", "car_code:eq:GO-5206206-69BE7A7C3DD542DCAF95F50D32BD9154"),
            ("filter", "count:gte:15"),
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] == 1
    assert data["features"][0]["properties"]["count"] == 20
    # Full-text search: q=OTHER (matches one feature's car_code)
    resp = await client.get("/collections/flt/items", params={"q": "OTHER"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["numberMatched"] >= 1
    assert any("OTHER" in str(f["properties"].get("car_code", "")) for f in data["features"])


@pytest.mark.asyncio
async def test_items_invalid_bbox_ignored(client):
    """Invalid bbox (non-numeric or not 4 parts) is ignored; no filter applied."""
    resp = await client.post(
        "/collections",
        json={"id": "bx", "title": "B", "extent": {"bbox": [[0, 0, 1, 1]], "crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
    )
    assert resp.status_code == 201
    resp = await client.get("/collections/bx/items?bbox=1,2,3,not-a-number")
    assert resp.status_code == 200
    # bbox filter not applied (invalid), so all features returned if any
    data = resp.json()
    assert "features" in data


@pytest.mark.asyncio
async def test_bulk_import_202_and_job_status(client):
    """Bulk import returns 202 and job status is visible; sync import is mocked."""
    resp = await client.post(
        "/collections",
        json={
            "id": "bulk_coll",
            "title": "Bulk",
            "description": "For bulk test",
        },
    )
    assert resp.status_code == 201

    with patch("app.services.bulk_worker.run_bulk_import_sync", return_value=(2, 0, None)):
        resp = await client.post(
            "/collections/bulk_coll/items/bulk",
            files={"file": ("data.geojson", b'{"type":"FeatureCollection","features":[]}', "application/geo+json")},
            data={"mode": "append"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "job_id" in body
        assert "status_url" in body
        assert body["status_url"].endswith(f"/jobs/{body['job_id']}")

        # Background task runs after response; keep patch active until it completes
        await asyncio.sleep(0.2)
        resp = await client.get(f"/jobs/{body['job_id']}")
        assert resp.status_code == 200, resp.text
        job = resp.json()
        assert job["status"] == "completed"
        assert job["items_created"] == 2
        assert job["collection_id"] == "bulk_coll"


@pytest.mark.asyncio
async def test_job_status_404(client):
    """Unknown job id returns 404."""
    resp = await client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404

