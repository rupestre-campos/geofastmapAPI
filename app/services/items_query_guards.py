"""Timeouts and helpers for collection items reads during bulk import."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings


def is_items_query_timeout_error(exc: BaseException) -> bool:
    """True when Postgres aborted the query (lock_timeout / statement_timeout)."""
    msg = str(exc).lower()
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        msg = f"{msg} {orig}".lower()
    return any(
        frag in msg
        for frag in (
            "lock timeout",
            "locknotavailable",
            "canceling statement",
            "query_canceled",
            "statement timeout",
        )
    )


async def apply_items_query_timeouts(db: AsyncSession, *, during_bulk: bool) -> None:
    """Short lock wait during bulk import so HTML/API fail fast instead of proxy timeout."""
    settings = get_settings()
    lock_ms = max(100, int(float(getattr(settings, "items_list_lock_timeout_seconds", 3.0) or 3.0) * 1000))
    if during_bulk:
        stmt_s = float(getattr(settings, "items_list_during_bulk_statement_timeout_seconds", 8.0) or 8.0)
    else:
        stmt_s = float(getattr(settings, "items_list_statement_timeout_seconds", 30.0) or 30.0)
    stmt_ms = max(500, int(stmt_s * 1000))
    await db.execute(text(f"SET LOCAL lock_timeout = {lock_ms}"))
    await db.execute(text(f"SET LOCAL statement_timeout = {stmt_ms}"))
