"""Unit tests for distributed mosaic subjob helpers."""

from types import SimpleNamespace

import pytest

from app.services import mosaic_plan_distributed as mpd


def test_build_subtask_payload_shape():
    c = SimpleNamespace(id="c1", stac_api_root_url="https://example.com", default_collections=["sentinel-2-l2a"])
    body = mpd.build_subtask_payload(
        catalogs=[c],
        stac_collection="sentinel-2-l2a",
        bbox=[0, 1, 2, 3],
        datetime_slice="2024-01-01T00:00:00Z/2024-01-31T23:59:59Z",
        cloud_cover_max=20.0,
        sort_mode="lowest_cloud",
        fetch_limit=200,
    )
    assert body["stac_collection"] == "sentinel-2-l2a"
    assert body["catalogs"][0]["id"] == "c1"
    assert body["bbox"] == [0.0, 1.0, 2.0, 3.0]
    assert body["datetime_slices"] == ["2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"]


@pytest.mark.asyncio
async def test_execute_subtask_payload_uses_collect(monkeypatch):
    async def fake_collect(*_args, **_kwargs):
        return ([{"id": "scene-a"}], [{"catalog_id": "c1", "detail": "warn"}])

    monkeypatch.setattr(mpd, "collect_stac_features", fake_collect)
    payload = {
        "catalogs": [{"id": "c1", "stac_api_root_url": "https://example.com", "default_collections": []}],
        "stac_collection": "sentinel-2-l2a",
        "bbox": [0, 0, 1, 1],
        "datetime_slices": ["2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"],
        "sort_mode": "lowest_cloud",
        "fetch_limit": 100,
    }
    out = await mpd.execute_subtask_payload(payload)
    assert len(out["features"]) == 1
    assert out["errors"][0]["catalog_id"] == "c1"
