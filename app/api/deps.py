"""Common API dependencies: auth (current user, admin)."""

import base64
import binascii
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud import user as user_crud
from app.db.session import get_db
from app.models.user import User


async def get_current_user_optional(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """
    Resolve current user from session (HTML) or HTTP Basic (API).
    Returns None if unauthenticated.
    """
    import logging

    from app.db.session import is_db_disconnect_error

    log = logging.getLogger(__name__)
    username: str | None = None
    password: str | None = None

    async def _lookup(name: str) -> User | None:
        try:
            return await user_crud.get_user_by_username(db, name)
        except Exception as exc:
            # Don't take down every page when the pool has dead sockets; treat as logged-out
            # and let the request continue (or 503 from get_db on harder failures).
            if is_db_disconnect_error(exc):
                log.warning("auth user lookup skipped (DB disconnect): %s", exc)
                return None
            raise

    # 1) Session (when SessionMiddleware is mounted)
    session = request.scope.get("session")
    if session is not None:
        username = session.get("username")
        if username:
            return await _lookup(username)
        # no username in session; fall through to Basic

    # 2) HTTP Basic
    if authorization and authorization.strip().lower().startswith("basic "):
        try:
            token = authorization.strip()[6:].strip()
            decoded = base64.b64decode(token).decode("utf-8")
            if ":" in decoded:
                username, _, password = decoded.partition(":")
            else:
                username = decoded
                password = ""
        except (binascii.Error, UnicodeDecodeError):
            pass
        else:
            if username:
                user = await _lookup(username)
                if user and password is not None:
                    from app.core.auth import verify_password
                    if verify_password(password, user.password_hash):
                        return user
    return None


async def get_current_user(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User | None:
    """Alias for optional current user (for type hints)."""
    return current_user


async def get_current_user_required(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Require authenticated user; raise 401 if not logged in."""
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return current_user


async def require_admin(
    current_user: Annotated[User, Depends(get_current_user_required)],
) -> User:
    """Require admin user; raise 403 if not admin."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
