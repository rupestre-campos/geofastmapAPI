"""Labels and metadata for map gallery cards."""

from __future__ import annotations

from typing import Any

from app.models.collection import VISIBILITY_LOGGED, VISIBILITY_PUBLIC


def owner_display_name(nickname: str | None, username: str | None) -> str | None:
    """Nickname when set, otherwise login username."""
    nick = (nickname or "").strip()
    if nick:
        return nick
    user = (username or "").strip()
    return user or None


def format_map_created_at(created_at) -> str | None:
    if created_at is None:
        return None
    try:
        return created_at.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(created_at)


def map_access_label(
    *,
    visibility: str,
    share_count: int,
    is_owner: bool,
    shared_with_me: bool,
) -> str:
    vis = (visibility or "private").strip().lower()
    if vis == VISIBILITY_PUBLIC:
        return "Public"
    if vis == VISIBILITY_LOGGED:
        return "Logged in"
    if shared_with_me and not is_owner:
        return "Shared with you"
    if share_count > 0:
        return f"Shared ({share_count})"
    return "Private"


def map_access_badge_class(access_label: str) -> str:
    if access_label == "Public":
        return "map-badge-public"
    if access_label == "Logged in":
        return "map-badge-logged"
    if access_label.startswith("Shared"):
        return "map-badge-shared"
    return "map-badge-private"


def build_map_gallery_item(
    m,
    *,
    can_edit: bool,
    owner_nickname: str | None,
    owner_username: str | None,
    share_count: int,
    shared_with_me: bool,
    share_display_names: list[str],
    current_user_id: int | None,
) -> dict[str, Any]:
    is_owner = bool(
        current_user_id is not None
        and getattr(m, "owner_id", None) is not None
        and m.owner_id == current_user_id
    )
    vis = getattr(m, "visibility", "private") or "private"
    access = map_access_label(
        visibility=vis,
        share_count=share_count,
        is_owner=is_owner,
        shared_with_me=shared_with_me,
    )
    return {
        "can_edit": can_edit,
        "owner_nickname": owner_nickname,
        "owner_display_name": owner_display_name(owner_nickname, owner_username),
        "access_label": access,
        "access_badge_class": map_access_badge_class(access),
        "share_count": share_count,
        "shared_with_me": shared_with_me,
        "share_display_names": share_display_names,
        "created_at_display": format_map_created_at(m.created_at),
        "is_owner": is_owner,
    }
