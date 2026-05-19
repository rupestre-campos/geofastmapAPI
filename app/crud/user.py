"""CRUD for users."""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_password
from app.models.user import User


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    if not username or not username.strip():
        return None
    result = await db.execute(
        select(User).where(User.username == username.strip())
    )
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession) -> Sequence[User]:
    result = await db.execute(select(User).order_by(User.username))
    return result.scalars().all()


async def get_usernames_by_ids(db: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    """Return mapping user_id -> username for given ids (skips missing)."""
    if not user_ids:
        return {}
    result = await db.execute(
        select(User.id, User.username).where(User.id.in_(user_ids))
    )
    return {row.id: row.username for row in result.all()}


async def get_nicknames_by_ids(db: AsyncSession, user_ids: list[int]) -> dict[int, str | None]:
    """Return mapping user_id -> nickname (value may be None)."""
    if not user_ids:
        return {}
    result = await db.execute(
        select(User.id, User.nickname).where(User.id.in_(user_ids))
    )
    return {row.id: row.nickname for row in result.all()}


async def get_nicknames_by_usernames(db: AsyncSession, usernames: list[str]) -> dict[str, str | None]:
    """Return mapping username -> nickname for given usernames."""
    names = [u.strip() for u in usernames if u and str(u).strip()]
    if not names:
        return {}
    result = await db.execute(
        select(User.username, User.nickname).where(User.username.in_(names))
    )
    return {row.username: row.nickname for row in result.all()}


async def get_user_by_nickname(
    db: AsyncSession,
    nickname: str,
    *,
    exclude_user_id: int | None = None,
) -> User | None:
    nick = (nickname or "").strip()
    if not nick:
        return None
    q = select(User).where(func.lower(User.nickname) == nick.lower())
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def set_nickname(
    db: AsyncSession,
    user_id: int,
    nickname: str | None,
) -> User | None:
    user = await get_user_by_id(db, user_id)
    if user is None:
        return None
    user.nickname = nickname
    await db.commit()
    await db.refresh(user)
    return user


async def create_user(
    db: AsyncSession,
    username: str,
    password: str,
    *,
    is_admin: bool = False,
    must_change_password: bool = True,
) -> User:
    user = User(
        username=username.strip(),
        password_hash=hash_password(password),
        is_admin=is_admin,
        must_change_password=must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def set_password(
    db: AsyncSession,
    user_id: int,
    new_password: str,
    *,
    must_change_password: bool = False,
) -> User | None:
    user = await get_user_by_id(db, user_id)
    if user is None:
        return None
    user.password_hash = hash_password(new_password)
    user.must_change_password = must_change_password
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(
    db: AsyncSession,
    user_id: int,
    *,
    is_admin: bool | None = None,
    must_change_password: bool | None = None,
) -> User | None:
    user = await get_user_by_id(db, user_id)
    if user is None:
        return None
    if is_admin is not None:
        user.is_admin = is_admin
    if must_change_password is not None:
        user.must_change_password = must_change_password
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if user is None:
        return False
    await db.delete(user)
    await db.commit()
    return True
