import os
from collections.abc import AsyncGenerator

import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.session import get_db
from app.main import create_app

# PostGIS/JSONB require PostgreSQL. Use same host as docker-compose (port 5434).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/geofast",
)


@pytest_asyncio.fixture()
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        if "postgresql" in TEST_DATABASE_URL:
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))
            # Drop and recreate so schema matches current model (Geometry + JSONB).
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    async_session = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    async with async_session() as session:
        # Clean tables so each test sees an empty DB (no leftover data from other tests).
        if "postgresql" in TEST_DATABASE_URL:
            await session.execute(sa.text("TRUNCATE features, collections RESTART IDENTITY CASCADE"))
            await session.commit()
        yield session


@pytest_asyncio.fixture()
async def app(db_session: AsyncSession):
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest_asyncio.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

