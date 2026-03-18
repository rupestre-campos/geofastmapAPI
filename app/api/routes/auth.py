"""Auth: login, logout, change password, admin user management."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_optional, get_current_user_required, require_admin
from app.core.config import get_settings
from app.core.html import html_response, wants_html
from app.crud import user as user_crud
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import PasswordChange, UserCreate, UserUpdate, UserRead

router = APIRouter()


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/login", summary="Login form")
async def login_form(request: Request):
    """Show login form (HTML)."""
    if not wants_html(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use ?f=html")
    base = _base_url(request)
    next_url = request.query_params.get("next", f"{base}/collections?f=html")
    return html_response("login.html", base=base, next_url=next_url, error=None, username=None, is_admin=False)


@router.post("/login", summary="Login (form)")
async def login_post(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Process login: set session and redirect. Form fields: username, password, next."""
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = (form.get("next") or request.query_params.get("next") or "").strip()
    base = _base_url(request)
    if not next_url or not next_url.startswith(base):
        next_url = f"{base}/collections?f=html"
    if not username:
        return html_response("login.html", base=base, next_url=next_url, error="Username required", username=None, is_admin=False)
    user = await user_crud.get_user_by_username(db, username)
    if not user:
        return html_response("login.html", base=base, next_url=next_url, error="Invalid username or password", username=None, is_admin=False)
    from app.core.auth import verify_password
    if not verify_password(password, user.password_hash):
        return html_response("login.html", base=base, next_url=next_url, error="Invalid username or password", username=None, is_admin=False)
    session = request.scope.get("session")
    if session is not None:
        session["username"] = user.username
        if user.must_change_password:
            return RedirectResponse(url=f"{base}/auth/change-password?next={next_url}", status_code=status.HTTP_302_FOUND)
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
    next_url = request.query_params.get("next", f"{base}/collections?f=html")
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
    next_url = (form.get("next") or request.query_params.get("next") or "").strip()
    base = _base_url(request)
    if not next_url or not next_url.startswith(base):
        next_url = f"{base}/collections?f=html"
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
    return html_response(
        "admin_users.html",
        base=base,
        username=current_user.username,
        is_admin=current_user.is_admin,
        users=[{"id": u.id, "username": u.username, "is_admin": u.is_admin, "must_change_password": u.must_change_password} for u in user_list],
    )


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
