from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def _asyncpg_connect_args() -> dict:
    s = get_settings()
    if s.database_use_pgbouncer and "asyncpg" in (s.database_url or ""):
        return {"statement_cache_size": 0}
    return {}


def create_app_async_engine(
    database_url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Create an async engine with shared pool / asyncpg settings (used by app and short-lived workers)."""
    s = get_settings()
    url = database_url or s.database_url
    kwargs: dict = {
        "echo": False,
        "future": True,
        "pool_size": pool_size if pool_size is not None else s.database_pool_size,
        "max_overflow": max_overflow if max_overflow is not None else s.database_pool_max_overflow,
        "pool_timeout": s.database_pool_timeout,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    }
    ca = _asyncpg_connect_args()
    if ca and "asyncpg" in url:
        kwargs["connect_args"] = ca
    return create_async_engine(url, **kwargs)


settings = get_settings()

engine = create_app_async_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
