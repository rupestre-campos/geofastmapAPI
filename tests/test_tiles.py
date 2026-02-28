"""Tests for OGC API Tiles routes (TileJSON, build, cancel, status, dynamic/static tiles)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_tiles_tilejson_returns_200(client):
    """GET /collections/{id}/tiles returns TileJSON with dynamic tile URL."""
    # Create collection first
    await client.post(
        "/collections",
        json={"id": "maps", "title": "Maps", "description": "Test"},
    )
    resp = await client.get("/collections/maps/tiles")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("tilejson") == "2.2.0"
    assert "tiles" in data
    assert len(data["tiles"]) >= 1
    # Dynamic tile URL pattern
    assert any("dynamic" in t for t in data["tiles"])


@pytest.mark.asyncio
async def test_tiles_tilejson_404_for_missing_collection(client):
    resp = await client.get("/collections/nonexistent/tiles")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_tiles_build_503_when_memory_queue(client):
    """POST .../tiles/build returns 503 when Redis is not configured (memory queue)."""
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    resp = await client.post("/collections/c1/tiles/build")
    assert resp.status_code == 503, resp.text
    assert "Redis" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_tiles_cancel_503_when_memory_queue(client):
    """POST .../tiles/build/cancel returns 503 when Redis is not configured."""
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    resp = await client.post("/collections/c1/tiles/build/cancel")
    assert resp.status_code == 503, resp.text
    assert "Redis" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_tiles_dynamic_404_for_missing_collection(client):
    resp = await client.get("/collections/nonexistent/tiles/dynamic/0/0/0.pbf")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_tiles_dynamic_invalid_z_400(client):
    """z out of [0, 22] returns 400. (z=-1 matches as collection_id, so we only test z=99.)"""
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    resp = await client.get("/collections/c1/tiles/dynamic/99/0/0.pbf")
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_tiles_dynamic_invalid_x_y_400(client):
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    # At z=2, x and y must be in [0, 3]
    resp = await client.get("/collections/c1/tiles/dynamic/2/10/0.pbf")
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_tiles_dynamic_200_empty_tile(client, app):
    """With mocked DB returning no MVT, dynamic tile returns 200 with empty body."""
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    # Route calls db.execute twice: property keys (fetchall), then MVT (first).
    result_keys = MagicMock()
    result_keys.fetchall.return_value = []
    mock_row = MagicMock()
    mock_row.mvt = None
    result_mvt = MagicMock()
    result_mvt.first.return_value = mock_row
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=[result_keys, result_mvt])

    async def override_get_db():
        yield mock_conn

    from app.db.session import get_db

    app.dependency_overrides[get_db] = override_get_db
    try:
        resp = await client.get("/collections/c1/tiles/dynamic/0/0/0.pbf")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("content-type") == "application/x-protobuf"
        assert resp.content == b""
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_tiles_static_404_when_no_static_tiles(client):
    """GET .../tiles/static/{z}/{x}/{y}.pbf returns 404 when no static tiles (MBTiles) built."""
    await client.post("/collections", json={"id": "c1", "title": "C1", "description": ""})
    resp = await client.get("/collections/c1/tiles/static/0/0/0.pbf")
    assert resp.status_code == 404, resp.text


