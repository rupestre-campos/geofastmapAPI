"""Unit tests for STAC federation merge (no HTTP)."""

import asyncio
from types import SimpleNamespace

import pytest

import app.core.config as core_config
from app.services import stac_federation as sf
from app.services.stac_federation import (
    _extract_next_link,
    _merge_item_collections,
    _post_search_with_retries,
    _retry_wait_seconds,
)


def test_merge_item_collections_two_features():
    parts = [
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "b", "properties": {}},
            ],
        },
    ]
    out = _merge_item_collections(parts, catalog_labels=["c1", "c2"])
    assert out["type"] == "FeatureCollection"
    assert len(out["features"]) == 2
    assert out["features"][0]["properties"]["geofast:sourceCatalog"] == "c1"
    assert out["features"][1]["properties"]["geofast:sourceCatalog"] == "c2"


def test_merge_dedupes_same_catalog_and_id():
    parts = [
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "properties": {}},
            ],
        },
    ]
    out = _merge_item_collections(parts, catalog_labels=["c1", "c1"])
    assert len(out["features"]) == 1


@pytest.mark.asyncio
async def test_federated_search_respects_catalog_parallelism(monkeypatch):
    settings = SimpleNamespace(
        stac_search_http_timeout_seconds=10.0,
        mosaic_stac_catalog_parallelism=2,
        mosaic_stac_total_inflight_max=4,
        stac_http_user_agent="test",
    )
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)
    monkeypatch.setattr(sf, "get_settings", lambda: settings)

    active = {"n": 0, "max": 0}

    async def fake_post(_client, catalog, _body):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.01)
        active["n"] -= 1
        return (
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "id": f"{catalog.id}-1", "properties": {}}],
            },
            None,
        )

    monkeypatch.setattr(sf, "_post_search_with_retries", fake_post)
    catalogs = [
        SimpleNamespace(id="c1", stac_api_root_url="https://x", default_collections=None),
        SimpleNamespace(id="c2", stac_api_root_url="https://x", default_collections=None),
        SimpleNamespace(id="c3", stac_api_root_url="https://x", default_collections=None),
    ]
    out, errs = await sf.federated_search(catalogs, {"collections": ["sentinel-2-l2a"]})
    assert not errs
    assert len(out["features"]) == 3
    assert active["max"] <= 2


def test_retry_wait_seconds_uses_exponential_backoff_with_cap():
    assert _retry_wait_seconds(base_backoff=2.0, attempt=0, max_backoff=300.0) == 2.0
    assert _retry_wait_seconds(base_backoff=2.0, attempt=4, max_backoff=300.0) == 32.0
    assert _retry_wait_seconds(base_backoff=2.0, attempt=8, max_backoff=300.0) == 300.0


def test_retry_wait_seconds_respects_retry_after_but_caps():
    assert _retry_wait_seconds(
        base_backoff=2.0,
        attempt=3,
        max_backoff=300.0,
        retry_after_seconds=40.0,
    ) == 40.0
    assert _retry_wait_seconds(
        base_backoff=2.0,
        attempt=3,
        max_backoff=300.0,
        retry_after_seconds=999.0,
    ) == 300.0


def test_extract_next_link_prefers_next_rel_and_normalizes_method():
    part = {
        "type": "FeatureCollection",
        "features": [],
        "links": [
            {"rel": "self", "href": "https://example.com/self"},
            {"rel": "next", "href": "https://example.com/next", "method": "post", "body": {"page": 2}},
        ],
    }
    nxt = _extract_next_link(part)
    assert nxt is not None
    assert nxt["href"] == "https://example.com/next"
    assert nxt["method"] == "POST"
    assert nxt["body"] == {"page": 2}


@pytest.mark.asyncio
async def test_post_search_with_retries_follows_next_pages(monkeypatch):
    settings = SimpleNamespace(
        stac_search_http_max_retries=0,
        stac_search_http_retry_backoff_seconds=0.1,
        stac_search_http_retry_backoff_max_seconds=1.0,
        stac_search_http_max_pages=2,
        stac_search_http_page_delay_seconds=0.0,
        stac_http_user_agent="test",
    )
    monkeypatch.setattr(core_config, "get_settings", lambda: settings)
    monkeypatch.setattr(sf, "get_settings", lambda: settings)

    calls = {"n": 0}

    async def fake_request_json_with_retries(*_args, **kwargs):
        calls["n"] += 1
        if kwargs.get("url", "").endswith("/search"):
            return (
                {
                    "type": "FeatureCollection",
                    "features": [{"type": "Feature", "id": "a", "properties": {}}],
                    "links": [{"rel": "next", "href": "https://x/next", "method": "GET"}],
                },
                None,
                200,
            )
        return (
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "id": "b", "properties": {}}],
                "links": [],
            },
            None,
            200,
        )

    monkeypatch.setattr(sf, "_request_json_with_retries", fake_request_json_with_retries)
    catalog = SimpleNamespace(id="c1", stac_api_root_url="https://x", default_collections=None)
    part, err = await _post_search_with_retries(SimpleNamespace(), catalog, {"collections": ["s2"]})  # type: ignore[arg-type]
    assert err is None
    assert part is not None
    assert [f["id"] for f in part["features"]] == ["a", "b"]
    assert calls["n"] == 2
