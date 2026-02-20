import os

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db import session as db_session
from app.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/geofast",
)


def test_settings_can_read_env(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:pass@host:9999/testdb",
    )
    settings = Settings()
    assert "host:9999" in settings.database_url


@pytest.mark.asyncio
async def test_get_db_uses_async_session(monkeypatch):
    # Model uses PostGIS Geometry + JSONB, so we must use PostgreSQL (SQLite can't create that schema).
    if "postgresql" not in TEST_DATABASE_URL:
        pytest.skip("test_get_db_uses_async_session requires PostgreSQL (TEST_DATABASE_URL)")

    test_engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with test_engine.begin() as conn:
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    TestSessionLocal = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(db_session, "AsyncSessionLocal", TestSessionLocal)

    gen = db_session.get_db()
    async for session in gen:  # type: ignore[assignment]
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
        break

    await test_engine.dispose()

