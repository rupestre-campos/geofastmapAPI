"""Tests for composite collection HTML pages."""

import pytest

from app.api.deps import get_current_user_optional
from app.models.user import User


def _test_user() -> User:
    return User(id=1, username="tester", password_hash="x", is_admin=False)


@pytest.fixture
def authed_client(client, app):
    async def override_user():
        return _test_user()

    app.dependency_overrides[get_current_user_optional] = override_user
    yield client
    app.dependency_overrides.pop(get_current_user_optional, None)


@pytest.mark.asyncio
async def test_composites_list_requires_login(client):
    resp = await client.get("/composites?f=html", follow_redirects=False)
    assert resp.status_code in (401, 302), resp.text


@pytest.mark.asyncio
async def test_composites_list_html(authed_client):
    resp = await authed_client.get("/composites?f=html")
    assert resp.status_code == 200, resp.text
    assert "Composite collections" in resp.text
    assert "/composites/new?f=html" in resp.text


@pytest.mark.asyncio
async def test_composites_new_html(authed_client):
    resp = await authed_client.get("/composites/new?f=html")
    assert resp.status_code == 200, resp.text
    assert "New composite collection" in resp.text


@pytest.mark.asyncio
async def test_composites_edit_html(authed_client):
    await authed_client.post(
        "/collections",
        json={"id": "comp1", "title": "Comp", "collection_type": "composite"},
    )
    resp = await authed_client.get("/composites/comp1/edit?f=html")
    assert resp.status_code == 200, resp.text
    assert "Member collections" in resp.text
    assert "Preview map" in resp.text


@pytest.mark.asyncio
async def test_collection_edit_redirects_composite(authed_client):
    await authed_client.post(
        "/collections",
        json={"id": "comp2", "collection_type": "composite"},
    )
    resp = await authed_client.get(
        "/collections/comp2/edit?f=html",
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    assert "/composites/comp2/edit?f=html" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_legacy_composite_edit_redirects(authed_client):
    await authed_client.post(
        "/collections",
        json={"id": "comp3", "collection_type": "composite"},
    )
    resp = await authed_client.get(
        "/collections/comp3/composite/edit?f=html",
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    assert "/composites/comp3/edit?f=html" in resp.headers.get("location", "")
