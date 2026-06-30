"""
Test fixtures. Database is mocked via in-memory Store; no real Postgres required.
"""
import os
import tempfile
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.session import get_db
from app.main import create_app

from tests.fake_db import FakeCollectionTilesCrud, FakeCollectionsCrud, FakeFeaturesCrud, Store


@pytest_asyncio.fixture(autouse=True)
def bulk_queue_memory_and_storage():
    """Use in-memory queue and temp storage so tests don't need Redis or shared disk."""
    tmp = tempfile.mkdtemp()
    prev_queue = os.environ.get("BULK_QUEUE_TYPE")
    prev_storage = os.environ.get("BULK_STORAGE_PATH")
    os.environ["BULK_QUEUE_TYPE"] = "memory"
    os.environ["BULK_STORAGE_PATH"] = tmp
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    if prev_queue is not None:
        os.environ["BULK_QUEUE_TYPE"] = prev_queue
    else:
        os.environ.pop("BULK_QUEUE_TYPE", None)
    if prev_storage is not None:
        os.environ["BULK_STORAGE_PATH"] = prev_storage
    else:
        os.environ.pop("BULK_STORAGE_PATH", None)


@pytest_asyncio.fixture
def store() -> Store:
    """Fresh in-memory store per test. No real DB."""
    return Store()


@pytest_asyncio.fixture
def app(store: Store):
    """App with get_db overridden to a mock and CRUD patched to use the in-memory store."""
    app = create_app()
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_db.__aexit__ = AsyncMock(return_value=None)

    async def override_get_db() -> AsyncGenerator[MagicMock, None]:
        yield MagicMock()

    app.dependency_overrides[get_db] = override_get_db

    fake_coll = FakeCollectionsCrud(store)
    fake_feat = FakeFeaturesCrud(store)

    async def _fake_member_tile_status(db, members):
        return [
            {
                "collection_id": m["collection_id"],
                "title": m["collection_id"],
                "feature_count": 0,
                "has_static_tiles": False,
                "tiles_revision": None,
                "minzoom": None,
                "maxzoom": None,
                "built_at": None,
            }
            for m in members
        ]

    fake_tiles = FakeCollectionTilesCrud()
    with (
        patch("app.api.routes.collections.collections_crud", fake_coll),
        patch("app.api.routes.composites.collections_crud", fake_coll),
        patch("app.api.routes.items.collections_crud", fake_coll),
        patch("app.api.routes.items.features_crud", fake_feat),
        patch("app.api.routes.tiles.collections_crud", fake_coll),
        patch("app.api.routes.tiles.tiles_crud", fake_tiles),
        patch("app.api.routes.composites.member_tile_status", new=_fake_member_tile_status),
        patch("app.api.routes.collections.member_tile_status", new=_fake_member_tile_status),
    ):
        yield app


@pytest_asyncio.fixture
async def client(app):
    """HTTP client for the test app (with mocked DB)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
