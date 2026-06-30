"""Tests for composite collection JSON API."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException, status

from app.api.deps import get_current_user_optional
from app.models.collection import COLLECTION_TYPE_COMPOSITE, COLLECTION_TYPE_VECTOR
from app.models.user import User


def _test_user() -> User:
    return User(id=1, username="tester", password_hash="x", is_admin=False)


async def _store_validate_composite_members(db, composite_id, members, *, collections_crud):
    if not members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Composite collection requires at least one member",
        )
    seen: set[str] = set()
    for m in members:
        cid = m["collection_id"]
        if cid == composite_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Composite cannot include itself as a member")
        if cid in seen:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Duplicate member: {cid}")
        seen.add(cid)
        row = await collections_crud.get_collection(db, cid)
        if not row:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Member collection not found: {cid}")
        if getattr(row, "collection_type", "") == COLLECTION_TYPE_COMPOSITE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Nested composite members are not supported: {cid}",
            )
        if getattr(row, "collection_type", COLLECTION_TYPE_VECTOR) != COLLECTION_TYPE_VECTOR:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Member must be a vector collection: {cid}",
            )


@pytest.fixture
def authed_client(client, app):
    async def override_user():
        return _test_user()

    app.dependency_overrides[get_current_user_optional] = override_user
    yield client
    app.dependency_overrides.pop(get_current_user_optional, None)


@pytest.fixture
def validate_via_store(app, store):
    from app.crud import collections as collections_crud

    fake = __import__("tests.fake_db", fromlist=["FakeCollectionsCrud"]).FakeCollectionsCrud(store)

    async def _validate(db, composite_id, members):
        await _store_validate_composite_members(
            db, composite_id, members, collections_crud=fake
        )

    with (
        patch("app.api.routes.collections.validate_composite_members", new=_validate),
        patch("app.api.routes.composites.validate_composite_members", new=_validate),
    ):
        yield


@pytest.mark.asyncio
async def test_post_composites_creates_with_members(authed_client, validate_via_store):
    for cid in ("layer_a", "layer_b"):
        resp = await authed_client.post(
            "/collections",
            json={"id": cid, "title": cid, "collection_type": "vector"},
        )
        assert resp.status_code == 201, resp.text

    resp = await authed_client.post(
        "/composites",
        json={
            "id": "mosaic_1",
            "title": "Mosaic",
            "composite_members": [
                {"collection_id": "layer_a"},
                {"collection_id": "layer_b"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] == "mosaic_1"
    assert data["collection_type"] == COLLECTION_TYPE_COMPOSITE
    assert len(data["composite_members"]) == 2
    assert data["composite_members"][0]["collection_id"] == "layer_a"
    assert data["member_status"] is not None
    assert len(data["member_status"]) == 2


@pytest.mark.asyncio
async def test_post_collections_composite_alias(authed_client, validate_via_store):
    await authed_client.post("/collections", json={"id": "m1", "title": "M1"})
    resp = await authed_client.post(
        "/collections",
        json={
            "id": "mosaic_2",
            "collection_type": "composite",
            "composite_members": [{"collection_id": "m1"}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["collection_type"] == COLLECTION_TYPE_COMPOSITE


@pytest.mark.asyncio
async def test_post_composites_rejects_missing_member(authed_client, validate_via_store):
    resp = await authed_client.post(
        "/composites",
        json={
            "id": "bad_mosaic",
            "composite_members": [{"collection_id": "no_such_layer"}],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_composite_members(authed_client, validate_via_store):
    await authed_client.post("/collections", json={"id": "a", "title": "A"})
    await authed_client.post("/collections", json={"id": "b", "title": "B"})
    await authed_client.post("/composites", json={"id": "mosaic_3", "title": "M3"})

    resp = await authed_client.patch(
        "/collections/mosaic_3",
        json={"composite_members": [{"collection_id": "a"}, {"collection_id": "b"}]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["composite_members"]) == 2
    assert data["member_status"] is not None


@pytest.mark.asyncio
async def test_get_composites_json_list(authed_client, validate_via_store):
    await authed_client.post("/collections", json={"id": "v1", "title": "V1"})
    await authed_client.post(
        "/composites",
        json={"id": "c1", "composite_members": [{"collection_id": "v1"}]},
    )
    await authed_client.post("/collections", json={"id": "plain", "title": "Plain"})

    resp = await authed_client.get("/composites")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ids = [c["id"] for c in data["collections"]]
    assert ids == ["c1"]
    assert data["collections"][0]["member_status"] is not None


@pytest.mark.asyncio
async def test_post_composites_requires_auth(client):
    resp = await client.post("/composites", json={"id": "x"})
    assert resp.status_code == 401, resp.text
