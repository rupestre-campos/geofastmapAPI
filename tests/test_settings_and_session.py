from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings, get_settings
from app.db import session as db_session


def test_database_url_direct_from_alembic_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db_direct")
    settings = Settings()
    assert "db_direct" in (settings.database_url_direct or "")


def test_settings_can_read_env(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:pass@host:9999/testdb",
    )
    settings = Settings()
    assert "host:9999" in settings.database_url


def test_database_sync_url_replaces_asyncpg(monkeypatch):
    """database_sync_url converts asyncpg to psycopg2 for sync engine."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    settings = Settings()
    assert settings.database_sync_url == "postgresql+psycopg2://u:p@localhost/db"


def test_database_sync_url_plain_postgresql(monkeypatch):
    """database_sync_url leaves postgresql:// as-is (psycopg2 default)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    settings = Settings()
    assert settings.database_sync_url == "postgresql://u:p@localhost/db"


def test_pgbouncer_engine_uses_null_pool_and_query_param(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@pool:6432/db")
    monkeypatch.setenv("DATABASE_USE_PGBOUNCER", "true")
    get_settings.cache_clear()
    eng = db_session.create_app_async_engine()
    assert eng.pool.__class__.__name__ == "NullPool"
    assert eng.url.query.get("prepared_statement_cache_size") == "0"
    ca = db_session._pgbouncer_asyncpg_connect_args()
    assert ca["statement_cache_size"] == 0
    assert ca["prepared_statement_cache_size"] == 0
    assert "prepared_statement_name_func" in ca


def test_direct_postgres_engine_uses_queue_pool(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("DATABASE_USE_PGBOUNCER", "false")
    get_settings.cache_clear()
    eng = db_session.create_app_async_engine()
    assert "NullPool" not in eng.pool.__class__.__name__


def test_database_sync_url_fallback(monkeypatch):
    """database_sync_url returns url as-is when no asyncpg and not postgresql://."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+other://u@p/db")
    settings = Settings()
    assert settings.database_sync_url == "postgresql+other://u@p/db"


@pytest.mark.asyncio
async def test_get_db_yields_session():
    """get_db is an async generator that yields a session. No real DB required."""
    mock_session = MagicMock()
    mock_session_context = AsyncMock()
    mock_session_context.__aenter__.return_value = mock_session
    mock_session_context.__aexit__.return_value = None
    mock_session_factory = MagicMock(return_value=mock_session_context)

    with patch.object(db_session, "AsyncSessionLocal", mock_session_factory):
        gen = db_session.get_db()
        sessions = []
        async for session in gen:
            sessions.append(session)
            break
        assert len(sessions) == 1
        assert sessions[0] is mock_session
        mock_session_factory.assert_called_once()

