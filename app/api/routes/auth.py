"""Auth: login, logout, change password, admin user management.

Includes basic brute-force protection for login attempts (per IP).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, get_current_user_required, require_admin
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.core.permissions import can_edit_collection
from app.crud import collection_tiles as tiles_crud
from app.crud import collections as collections_crud
from app.crud import maps as maps_crud
from app.crud import styles as styles_crud
from app.crud import resource_share as resource_share_crud
from app.crud import user as user_crud
from app.db.session import get_db
from app.models.collection import Collection
from app.models.map import Map
from app.models.resource_share import RESOURCE_TYPE_COLLECTION, RESOURCE_TYPE_MAP, ROLE_EDITOR
from app.models.user import User
from app.schemas.user import PasswordChange, UserCreate, UserUpdate, UserRead

router = APIRouter()

_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_LOGIN_ATTEMPTS_MEM: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Prefer X-Forwarded-For when behind a proxy (first IP).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    host = getattr(getattr(request, "client", None), "host", None)
    return host or "unknown"


def _attempts_key(ip: str) -> str:
    return f"geofastmap:auth:login_fail:{ip}"


def _prune_attempts(ts: list[float], now: float, window_s: int) -> list[float]:
    cutoff = now - window_s
    return [t for t in ts if t >= cutoff]


def _is_blocked_and_register_failure(request: Request, *, register_failure: bool) -> tuple[bool, int]:
    """
    Returns (blocked, retry_after_seconds).
    Uses Redis if configured, else in-memory.
    """
    settings = get_settings()
    ip = _client_ip(request)
    now = time.time()
    window_s = 60
    max_failures = 8
    block_s = 300  # 5 minutes

    if settings.bulk_queue_type == "redis" and settings.redis_url:
        try:
            import redis

            r = redis.from_url(settings.redis_url, decode_responses=True)
            key = _attempts_key(ip)
            # If blocked marker exists, block.
            blocked_key = key + ":blocked"
            ttl = r.ttl(blocked_key)
            if ttl and ttl > 0:
                return True, int(ttl)
            # Keep a rolling window list of timestamps.
            # Use a Redis list: LPUSH now, LTRIM, then remove old by scanning a small list.
            # We'll store as float seconds strings.
            if register_failure:
                r.lpush(key, str(now))
                r.ltrim(key, 0, max_failures * 2)
                r.expire(key, window_s + block_s)
            raw = r.lrange(key, 0, max_failures * 2)
            ts = []
            for s in raw:
                try:
                    ts.append(float(s))
                except Exception:
                    pass
            ts = [t for t in ts if t >= now - window_s]
            if len(ts) >= max_failures:
                r.set(blocked_key, "1", ex=block_s)
                return True, block_s
            return False, 0
        except Exception:
            # Fall back to in-memory
            pass

    with _LOGIN_ATTEMPTS_LOCK:
        ts = _LOGIN_ATTEMPTS_MEM.get(ip, [])
        ts = _prune_attempts(ts, now, window_s)
        if register_failure:
            ts.append(now)
        _LOGIN_ATTEMPTS_MEM[ip] = ts
        if len(ts) >= max_failures:
            # Record block as a timestamp in the future (store as negative marker).
            _LOGIN_ATTEMPTS_MEM[ip] = [now + block_s]
            return True, block_s
        # If we stored a single future timestamp marker, block until it passes.
        if len(ts) == 1 and ts[0] > now and (now + window_s) < ts[0]:
            return True, int(ts[0] - now)
    return False, 0


def _clear_failures(request: Request) -> None:
    settings = get_settings()
    ip = _client_ip(request)
    if settings.bulk_queue_type == "redis" and settings.redis_url:
        try:
            import redis

            r = redis.from_url(settings.redis_url, decode_responses=True)
            key = _attempts_key(ip)
            r.delete(key)
            r.delete(key + ":blocked")
            return
        except Exception:
            pass
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS_MEM.pop(ip, None)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _normalize_next_url(request: Request, next_url: str | None) -> str:
    """
    Safe redirect target after login / change-password.
    Accepts absolute same-origin URLs or a path+query beginning with / (not //).
    """
    base = _base_url(request)
    u = (next_url or "").strip()
    if not u:
        return f"{base}/collections?f=html"
    if u.startswith("/") and not u.startswith("//"):
        if u.startswith("/auth/login"):
            return f"{base}/collections?f=html"
        return base + u
    if u.startswith(base):
        if u.startswith(base + "/auth/login"):
            return f"{base}/collections?f=html"
        return u
    return f"{base}/collections?f=html"


def _format_bytes(n: int | None) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.2f} {units[i]}"


async def _features_partition_name_map(db: AsyncSession) -> dict[str, str]:
    """
    Map collection_id -> partition relname (child table) for features.
    Uses pg_get_expr(relpartbound) to identify LIST partition bounds.
    """
    r = await db.execute(
        text(
            """
            SELECT c.relname AS relname, pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'features'
            """
        )
    )
    rows = r.fetchall()
    out: dict[str, str] = {}
    for row in rows:
        b = row.bound or ""
        if "FOR VALUES IN" not in b:
            continue
        try:
            inside = b.split("IN", 1)[1].strip()
            if inside.startswith("(") and inside.endswith(")"):
                inside = inside[1:-1].strip()
            if inside.startswith("'") and inside.endswith("'"):
                cid = inside[1:-1].replace("''", "'")
                out[cid] = row.relname
        except Exception:
            continue
    return out


async def _pg_table_size_bytes(db: AsyncSession, table_name: str) -> int:
    """Return pg_total_relation_size for the given table (by name in public schema)."""
    r = await db.execute(
        text("SELECT pg_total_relation_size(to_regclass(:tbl)) AS s"),
        {"tbl": f"public.{table_name}"},
    )
    row = r.first()
    return int(row.s or 0) if row else 0


def _file_size_bytes(path_str: str | None) -> int:
    if not path_str:
        return 0
    try:
        p = Path(path_str)
        if not p.exists() or not p.is_file():
            return 0
        return int(p.stat().st_size)
    except Exception:
        return 0


async def _delete_collection_tiles(db: AsyncSession, collection_id: str) -> None:
    rec = await tiles_crud.get_collection_tiles(db, collection_id)
    if rec and rec.pmtiles_path:
        try:
            p = Path(rec.pmtiles_path)
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass
    await tiles_crud.clear_static_tiles(db, collection_id)


@router.get("/config", summary="User config page")
async def user_config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    base = _base_url(request)

    # Cache admin usernames so we can exclude them from editor lists.
    admins_r = await db.execute(text("SELECT username FROM users WHERE is_admin = true"))
    admin_usernames = {row[0] for row in admins_r.fetchall() if row and row[0]}

    r = await db.execute(
        select(Collection)
        .where(Collection.owner_id == current_user.id)
        .order_by(Collection.id.asc())
    )
    owned = r.scalars().all()
    owned_ids = [c.id for c in owned]

    part_map = await _features_partition_name_map(db)

    total_styles_in_owned = 0
    if owned_ids:
        r2 = await db.execute(
            text("SELECT COUNT(*) AS n FROM styles WHERE collection_id = ANY(:cids)"),
            {"cids": owned_ids},
        )
        row2 = r2.first()
        total_styles_in_owned = int(row2.n or 0) if row2 else 0

    collections_rows = []
    total_features = 0
    total_db_size = 0
    total_tiles_size = 0

    for c in owned:
        total_features += int(getattr(c, "feature_count", 0) or 0)

        part = part_map.get(c.id)
        db_size = await _pg_table_size_bytes(db, part) if part else 0
        total_db_size += db_size

        tiles_rec = await tiles_crud.get_collection_tiles(db, c.id)
        tiles_size = _file_size_bytes(getattr(tiles_rec, "pmtiles_path", None) if tiles_rec else None)
        total_tiles_size += tiles_size

        shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_COLLECTION, c.id)
        editors = sorted(
            [
                u
                for (u, role) in shares
                if role == ROLE_EDITOR and u and u not in admin_usernames
            ]
        )

        collections_rows.append(
            {
                "id": c.id,
                "title": c.title,
                "feature_count": int(getattr(c, "feature_count", 0) or 0),
                "db_size_h": _format_bytes(db_size),
                "tiles_size_h": _format_bytes(tiles_size),
                "visibility": getattr(c, "visibility", "private") or "private",
                "viewer_can_edit": bool(getattr(c, "viewer_can_edit", False)),
                "editors": editors,
            }
        )

    # Owned maps
    maps_r = await db.execute(
        select(Map).where(Map.owner_id == current_user.id).order_by(Map.updated_at.desc())
    )
    owned_maps = maps_r.scalars().all()
    maps_rows = []
    for m in owned_maps:
        shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_MAP, str(m.id))
        editors = sorted(
            [
                u
                for (u, role) in shares
                if role == ROLE_EDITOR and u and u not in admin_usernames
            ]
        )
        maps_rows.append(
            {
                "id": str(m.id),
                "name": m.name,
                "description": m.description,
                "visibility": getattr(m, "visibility", "private") or "private",
                "viewer_can_edit": bool(getattr(m, "viewer_can_edit", False)),
                "editors": editors,
            }
        )

    summary = {
        "total_collections": len(owned),
        "total_maps": len(owned_maps),
        "total_features": total_features,
        "total_styles_in_owned_collections": total_styles_in_owned,
        "total_db_size_h": _format_bytes(total_db_size),
        "total_tiles_size_h": _format_bytes(total_tiles_size),
    }

    return html_response(
        "user_config.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        summary=summary,
        collections=collections_rows,
        maps=maps_rows,
    )


@router.post("/config/collections/{collection_id}/editors/remove", summary="Remove editor from owned collection")
async def user_config_remove_collection_editor(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)
    # Never remove admin shares through this UI.
    u = await user_crud.get_user_by_username(db, username)
    if u and u.is_admin:
        return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, coll, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_COLLECTION, collection_id, username)
    return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/config/maps/{map_id}/editors/remove", summary="Remove editor from owned map")
async def user_config_remove_map_editor(
    request: Request,
    map_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)
    u = await user_crud.get_user_by_username(db, username)
    if u and u.is_admin:
        return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)
    # Reuse map permission check by verifying ownership/edit permission via CRUD + existing can_edit_map logic.
    try:
        import uuid as _uuid

        mid = _uuid.UUID(map_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid map id")
    row = await maps_crud.get_map(db, mid)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    # Owner can edit; admins can edit too. For this config page we effectively scope to owned maps.
    if not (current_user.is_admin or row.owner_id == current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_MAP, map_id, username)
    return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/config/reset-tiles", summary="Delete tiles for all owned collections")
async def user_config_reset_tiles(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    r = await db.execute(select(Collection.id).where(Collection.owner_id == current_user.id))
    owned_ids = [row[0] for row in r.all()]
    for cid in owned_ids:
        await _delete_collection_tiles(db, cid)
    return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/config/collections/{collection_id}/delete-tiles", summary="Delete tiles for one owned collection")
async def user_config_delete_tiles(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await _delete_collection_tiles(db, collection_id)
    return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/config/collections/{collection_id}/delete", summary="Delete one owned collection")
async def user_config_delete_collection(
    request: Request,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_edit_collection(db, collection, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
    await collections_crud.delete_collection(db, collection_id)
    return RedirectResponse(url=f"{_base_url(request)}/auth/config?f=html", status_code=status.HTTP_302_FOUND)


@router.get("/login", summary="Login form")
async def login_form(request: Request):
    """Show login form (HTML)."""
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    base = _base_url(request)
    next_url = _normalize_next_url(request, request.query_params.get("next"))
    return html_response("login.html", base=base, next_url=next_url, error=None, username=None, is_admin=False)


@router.post("/login", summary="Login (form)")
async def login_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Process login: set session and redirect. Form fields: username, password, next."""
    blocked, retry_after = _is_blocked_and_register_failure(request, register_failure=False)
    if blocked:
        # Keep response generic; do not reveal anything about username validity.
        return html_response(
            "login.html",
            base=_base_url(request),
            next_url=_normalize_next_url(request, request.query_params.get("next")),
            error=f"Too many login attempts. Try again in {retry_after}s.",
            username=None,
            is_admin=False,
        )
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    base = _base_url(request)
    next_url = _normalize_next_url(request, (form.get("next") or request.query_params.get("next") or "").strip() or None)
    if not username:
        return html_response("login.html", base=base, next_url=next_url, error="Username required", username=None, is_admin=False)
    user = await user_crud.get_user_by_username(db, username)
    if not user:
        _is_blocked_and_register_failure(request, register_failure=True)
        return html_response("login.html", base=base, next_url=next_url, error="Invalid username or password", username=None, is_admin=False)
    from app.core.auth import verify_password
    if not verify_password(password, user.password_hash):
        _is_blocked_and_register_failure(request, register_failure=True)
        return html_response("login.html", base=base, next_url=next_url, error="Invalid username or password", username=None, is_admin=False)
    _clear_failures(request)
    session = request.scope.get("session")
    if session is not None:
        session["username"] = user.username
        if user.must_change_password:
            return RedirectResponse(
                url=f"{base}/auth/change-password?next={quote(next_url, safe='')}",
                status_code=status.HTTP_302_FOUND,
            )
        return RedirectResponse(url=next_url, status_code=status.HTTP_302_FOUND)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Session not configured (set auth_secret_key)")


@router.get("/logout", summary="Logout")
async def logout(request: Request):
    """Clear session and redirect to landing."""
    session = request.scope.get("session")
    if session is not None:
        session.clear()
    base = _base_url(request)
    return RedirectResponse(url=f"{base}/", status_code=status.HTTP_302_FOUND)


@router.get("/change-password", summary="Change password form")
async def change_password_form(
    request: Request,
    current_user: User = Depends(get_current_user_required),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    base = _base_url(request)
    next_url = _normalize_next_url(request, request.query_params.get("next"))
    return html_response(
        "change_password.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        next_url=next_url,
        error=None,
        must_change_password=current_user.must_change_password,
    )


@router.post("/change-password", summary="Change password")
async def change_password_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Change own password. Form: current_password (if not first login), new_password, new_password_confirm, next."""
    form = await request.form()
    current_password = form.get("current_password") or ""
    new_password = form.get("new_password") or ""
    new_password_confirm = form.get("new_password_confirm") or ""
    base = _base_url(request)
    next_url = _normalize_next_url(request, (form.get("next") or request.query_params.get("next") or "").strip() or None)
    ctx = dict(
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        next_url=next_url,
        must_change_password=current_user.must_change_password,
    )
    if not current_user.must_change_password:
        from app.core.auth import verify_password
        if not verify_password(current_password, current_user.password_hash):
            return html_response("change_password.html", error="Current password is incorrect", **ctx)
    if not new_password or len(new_password) < 1:
        return html_response("change_password.html", error="New password is required", **ctx)
    if new_password != new_password_confirm:
        return html_response("change_password.html", error="New password and confirmation do not match", **ctx)
    await user_crud.set_password(db, current_user.id, new_password, must_change_password=False)
    session = request.scope.get("session")
    if session is not None:
        session["username"] = current_user.username
    return RedirectResponse(url=next_url, status_code=status.HTTP_302_FOUND)


# ----- Admin: user management -----


@router.get("/users", summary="List users (admin)")
async def list_users_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    user_list = await user_crud.list_users(db)
    base = _base_url(request)

    admins_r = await db.execute(text("SELECT username FROM users WHERE is_admin = true"))
    admin_usernames = {row[0] for row in admins_r.fetchall() if row and row[0]}

    async def usage_for_owner(owner_id: int) -> dict:
        r = await db.execute(
            select(Collection).where(Collection.owner_id == owner_id).order_by(Collection.id.asc())
        )
        owned = r.scalars().all()
        owned_ids = [c.id for c in owned]
        part_map = await _features_partition_name_map(db)

        total_styles_in_owned = 0
        if owned_ids:
            r2 = await db.execute(
                text("SELECT COUNT(*) AS n FROM styles WHERE collection_id = ANY(:cids)"),
                {"cids": owned_ids},
            )
            row2 = r2.first()
            total_styles_in_owned = int(row2.n or 0) if row2 else 0

        collections_rows = []
        total_features = 0
        total_db_size = 0
        total_tiles_size = 0
        for c in owned:
            total_features += int(getattr(c, "feature_count", 0) or 0)
            part = part_map.get(c.id)
            db_size = await _pg_table_size_bytes(db, part) if part else 0
            total_db_size += db_size
            tiles_rec = await tiles_crud.get_collection_tiles(db, c.id)
            tiles_size = _file_size_bytes(getattr(tiles_rec, "pmtiles_path", None) if tiles_rec else None)
            total_tiles_size += tiles_size
            shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_COLLECTION, c.id)
            editors = sorted([u for (u, role) in shares if role == ROLE_EDITOR and u and u not in admin_usernames])
            collections_rows.append(
                {
                    "id": c.id,
                    "title": c.title,
                    "feature_count": int(getattr(c, "feature_count", 0) or 0),
                    "db_size_h": _format_bytes(db_size),
                    "tiles_size_h": _format_bytes(tiles_size),
                    "visibility": getattr(c, "visibility", "private") or "private",
                    "viewer_can_edit": bool(getattr(c, "viewer_can_edit", False)),
                    "editors": editors,
                }
            )

        maps_r = await db.execute(
            select(Map).where(Map.owner_id == owner_id).order_by(Map.updated_at.desc())
        )
        owned_maps = maps_r.scalars().all()
        maps_rows = []
        for m in owned_maps:
            shares = await resource_share_crud.list_shares(db, RESOURCE_TYPE_MAP, str(m.id))
            editors = sorted([u for (u, role) in shares if role == ROLE_EDITOR and u and u not in admin_usernames])
            maps_rows.append(
                {
                    "id": str(m.id),
                    "name": m.name,
                    "description": m.description,
                    "visibility": getattr(m, "visibility", "private") or "private",
                    "viewer_can_edit": bool(getattr(m, "viewer_can_edit", False)),
                    "editors": editors,
                }
            )

        return {
            "summary": {
                "total_collections": len(owned),
                "total_maps": len(owned_maps),
                "total_features": total_features,
                "total_styles_in_owned_collections": total_styles_in_owned,
                "total_db_size_h": _format_bytes(total_db_size),
                "total_tiles_size_h": _format_bytes(total_tiles_size),
            },
            "collections": collections_rows,
            "maps": maps_rows,
        }

    users_usage = []
    for u in user_list:
        snap = await usage_for_owner(u.id)
        users_usage.append(
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "must_change_password": u.must_change_password,
                "summary": snap["summary"],
                "collections": snap["collections"],
                "maps": snap["maps"],
            }
        )
    return html_response(
        "admin_users.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        users=users_usage,
    )


@router.post("/users/{user_id}/reset-tiles", summary="Reset tiles for a user's collections (admin)")
async def admin_user_reset_tiles(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    r = await db.execute(select(Collection.id).where(Collection.owner_id == user_id))
    owned_ids = [row[0] for row in r.all()]
    for cid in owned_ids:
        await _delete_collection_tiles(db, cid)
    return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/collections/{collection_id}/delete-tiles", summary="Delete tiles for user's collection (admin)")
async def admin_user_delete_tiles(
    request: Request,
    user_id: int,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll or coll.owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    await _delete_collection_tiles(db, collection_id)
    return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/collections/{collection_id}/delete", summary="Delete user's collection (admin)")
async def admin_user_delete_collection(
    request: Request,
    user_id: int,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll or coll.owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    await collections_crud.delete_collection(db, collection_id)
    return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/collections/{collection_id}/editors/remove", summary="Remove editor from user's collection (admin)")
async def admin_user_remove_collection_editor(
    request: Request,
    user_id: int,
    collection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)
    u = await user_crud.get_user_by_username(db, username)
    if u and u.is_admin:
        return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)
    coll = await collections_crud.get_collection(db, collection_id)
    if not coll or coll.owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_COLLECTION, collection_id, username)
    return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/maps/{map_id}/editors/remove", summary="Remove editor from user's map (admin)")
async def admin_user_remove_map_editor(
    request: Request,
    user_id: int,
    map_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    form = await request.form()
    username = (form.get("username") or "").strip()
    if not username:
        return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)
    u = await user_crud.get_user_by_username(db, username)
    if u and u.is_admin:
        return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)
    try:
        import uuid as _uuid

        mid = _uuid.UUID(map_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid map id")
    row = await maps_crud.get_map(db, mid)
    if not row or row.owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    await resource_share_crud.remove_share(db, RESOURCE_TYPE_MAP, map_id, username)
    return RedirectResponse(url=f"{_base_url(request)}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.get("/users/new", summary="Create user form (admin)")
async def new_user_form(
    request: Request,
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    base = _base_url(request)
    return html_response("admin_user_edit.html", base=base, user=None, error=None, username=current_user.username, is_admin=True)


@router.post("/users", summary="Create user (admin)")
async def create_user_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    is_admin = form.get("is_admin") == "on"
    base = _base_url(request)
    if not username:
        return html_response("admin_user_edit.html", base=base, username=current_user.username, is_admin=current_user.is_admin, user=None, error="Username required")
    existing = await user_crud.get_user_by_username(db, username)
    if existing:
        return html_response("admin_user_edit.html", base=base, username=current_user.username, is_admin=current_user.is_admin, user={"username": username, "is_admin": is_admin}, error="Username already exists")
    if not password:
        return html_response("admin_user_edit.html", base=base, username=current_user.username, is_admin=current_user.is_admin, user={"username": username, "is_admin": is_admin}, error="Password required")
    await user_crud.create_user(db, username, password, is_admin=is_admin, must_change_password=True)
    return RedirectResponse(url=f"{base}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.get("/users/{user_id}/edit", summary="Edit user form (admin)")
async def edit_user_form(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    user = await user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    base = _base_url(request)
    return html_response(
        "admin_user_edit.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        user={"id": user.id, "username": user.username, "is_admin": user.is_admin, "must_change_password": user.must_change_password},
        error=None,
    )


@router.post("/users/{user_id}", summary="Update user (admin)")
async def update_user_post(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update is_admin, must_change_password; optional new password."""
    form = await request.form()
    is_admin = form.get("is_admin") == "on"
    must_change_password = form.get("must_change_password") == "on"
    new_password = (form.get("new_password") or "").strip()
    base = _base_url(request)
    user = await user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    # Only admin can delete another admin; prevent demoting last admin
    if user.is_admin and not is_admin:
        from sqlalchemy import select, func
        from app.models.user import User as UserModel
        r = await db.execute(select(func.count()).select_from(UserModel).where(UserModel.is_admin.is_(True)))
        admin_count = r.scalar() or 0
        if admin_count <= 1:
            return html_response(
                "admin_user_edit.html",
                base=base,
                username=current_user.username,
                is_admin=current_user.is_admin,
                user={"id": user.id, "username": user.username, "is_admin": user.is_admin, "must_change_password": user.must_change_password},
                error="Cannot remove the last admin",
            )
    await user_crud.update_user(db, user_id, is_admin=is_admin, must_change_password=must_change_password)
    if new_password:
        await user_crud.set_password(db, user_id, new_password, must_change_password=must_change_password)
    return RedirectResponse(url=f"{base}/auth/users?f=html", status_code=status.HTTP_302_FOUND)


@router.post("/users/{user_id}/delete", summary="Delete user (admin)")
async def delete_user_post(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete user. Only admin can delete another admin."""
    user = await user_crud.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_admin and current_user.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an admin can delete another admin")
    if current_user.id == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own user")
    await user_crud.delete_user(db, user_id)
    base = _base_url(request)
    return RedirectResponse(url=f"{base}/auth/users?f=html", status_code=status.HTTP_302_FOUND)
