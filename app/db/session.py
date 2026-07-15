from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def _use_pgbouncer_asyncpg(url: str) -> bool:
    s = get_settings()
    return bool(s.database_use_pgbouncer and "asyncpg" in (url or ""))


def _pgbouncer_asyncpg_connect_args() -> dict:
    """PgBouncer transaction pool: unique prepared stmt names + no driver/dialect stmt caches."""
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        # SQLAlchemy still calls connection.prepare(); reuse of __asyncpg_stmt_N__ breaks on PgBouncer.
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }


def _url_with_pgbouncer_query(url: str) -> str:
    """Ensure prepared_statement_cache_size=0 is in the URL (SQLAlchemy reads it in create_connect_args)."""
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["prepared_statement_cache_size"] = "0"
    return urlunparse(parsed._replace(query=urlencode(q)))


def create_app_async_engine(
    database_url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Create an async engine with shared pool / asyncpg settings (used by app and short-lived workers)."""
    s = get_settings()
    url = database_url or s.database_url
    pgbouncer = _use_pgbouncer_asyncpg(url)
    if pgbouncer:
        url = _url_with_pgbouncer_query(url)

    kwargs: dict = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
    }
    if pgbouncer:
        # Let PgBouncer own pooling; avoid stale prepared statements on checked-in conns.
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=pool_size if pool_size is not None else s.database_pool_size,
            max_overflow=max_overflow if max_overflow is not None else s.database_pool_max_overflow,
            pool_timeout=s.database_pool_timeout,
            pool_recycle=3600,
        )

    if pgbouncer and "asyncpg" in url:
        kwargs["connect_args"] = _pgbouncer_asyncpg_connect_args()
    return create_async_engine(url, **kwargs)


settings = get_settings()

engine = create_app_async_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a DB session. Pool checkout failures become HTTP 503 (fail fast under load)."""
    from fastapi import HTTPException, status
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    try:
        async with AsyncSessionLocal() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
    except SATimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool saturated; retry shortly.",
            headers={"Retry-After": "2"},
        ) from exc
    except TimeoutError as exc:
        # asyncpg / asyncio wait on pool
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool saturated; retry shortly.",
            headers={"Retry-After": "2"},
        ) from exc
