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


def is_db_disconnect_error(exc: BaseException) -> bool:
    """True when Postgres/asyncpg closed the socket (stale pool, restart, max_connections)."""
    try:
        from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

        if isinstance(exc, (OperationalError, InterfaceError)):
            return True
        if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
            return True
    except Exception:
        pass
    name = type(exc).__name__
    if name in ("ConnectionDoesNotExistError", "InterfaceError", "ConnectionResetError"):
        return True
    # Unwrap SQLAlchemy → asyncpg
    cause = getattr(exc, "__cause__", None) or getattr(exc, "orig", None)
    if cause is not None and cause is not exc:
        if is_db_disconnect_error(cause):
            return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "connection does not exist",
            "connection was closed",
            "connection is closed",
            "server closed the connection",
            "terminating connection",
            "too many connections",
            "connection reset",
            "broken pipe",
            "could not connect",
        )
    )


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

    connect_timeout = float(getattr(s, "database_connect_timeout_seconds", 10.0) or 10.0)
    command_timeout = float(getattr(s, "database_command_timeout_seconds", 0.0) or 0.0)
    connect_args: dict = {"timeout": connect_timeout}
    if command_timeout > 0:
        connect_args["command_timeout"] = command_timeout

    kwargs: dict = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
    }
    if pgbouncer:
        # Let PgBouncer own pooling; avoid stale prepared statements on checked-in conns.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {**_pgbouncer_asyncpg_connect_args(), **connect_args}
    else:
        recycle = int(getattr(s, "database_pool_recycle_seconds", 300) or 300)
        kwargs.update(
            pool_size=pool_size if pool_size is not None else s.database_pool_size,
            max_overflow=max_overflow if max_overflow is not None else s.database_pool_max_overflow,
            pool_timeout=s.database_pool_timeout,
            # Recycle before typical idle/NAT/LB cuts; pre_ping catches the rest.
            pool_recycle=max(60, recycle),
            pool_reset_on_return="rollback",
            connect_args=connect_args,
        )

    return create_async_engine(url, **kwargs)


settings = get_settings()

engine = create_app_async_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a DB session. Pool / disconnect failures become HTTP 503 (fail fast)."""
    import logging

    from fastapi import HTTPException, status
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    log = logging.getLogger(__name__)

    def _busy(detail: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
            headers={"Retry-After": "2"},
        )

    try:
        async with AsyncSessionLocal() as session:
            try:
                yield session
            except Exception as exc:
                try:
                    await session.rollback()
                except Exception:
                    pass
                if is_db_disconnect_error(exc):
                    log.warning("DB disconnect during request: %s", exc)
                    raise _busy("Database connection lost; retry shortly.") from exc
                raise
    except HTTPException:
        raise
    except SATimeoutError as exc:
        raise _busy("Database pool saturated; retry shortly.") from exc
    except TimeoutError as exc:
        # asyncpg / asyncio wait on pool
        raise _busy("Database pool saturated; retry shortly.") from exc
    except Exception as exc:
        if is_db_disconnect_error(exc):
            log.warning("DB disconnect on checkout/connect: %s", exc)
            # Drop dead pooled sockets so the next request opens fresh ones.
            try:
                await engine.dispose()
            except Exception:
                pass
            raise _busy("Database connection lost; retry shortly.") from exc
        raise
