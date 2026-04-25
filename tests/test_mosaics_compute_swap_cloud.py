"""Lazy swap (body.selected) must use the same STAC cloud filter as full-plan (cloud_for_search)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.routes.mosaics import MosaicPlanBody, compute_mosaic_plan


class _AsyncCM:
    def __init__(self, db: MagicMock) -> None:
        self._db = db

    async def __aenter__(self) -> MagicMock:
        return self._db

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_selected_swap_collect_passes_none_cloud_when_same_pass_strips() -> None:
    captured: dict[str, object] = {}

    async def capture_collect(*_args: object, **kwargs: object) -> tuple[list, list]:
        captured["cloud_cover_max"] = kwargs.get("cloud_cover_max")
        return [], []

    mock_db = MagicMock()
    user = MagicMock()
    user.id = 42

    body = MosaicPlanBody(
        catalog_id="stac-local",
        stac_collection_id="sentinel-2-l2a",
        bbox=[0.0, 0.0, 1.0, 1.0],
        date_start="2024-06-01",
        date_end="2024-06-30",
        seasons=[],
        cloud_cover_max=30.0,
        sort_mode="lowest_cloud",
        use_same_pass_date_strips=True,
        selected=[
            {
                "key": "S2_MSIL2A_20240615T000000_N0000_R000_T23KQT_20240615",
                "stac_item_id": "S2_MSIL2A_20240615T000000_N0000_R000_T23KQT_20240615",
                "footprint": {
                    "type": "Polygon",
                    "coordinates": [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9], [0.1, 0.1]]],
                },
            }
        ],
        include_footprint_display=False,
    )

    mock_catalog = MagicMock()
    mock_catalog.enabled = True

    async def get_cat(_db: MagicMock, _cid: str) -> MagicMock:
        return mock_catalog

    with (
        patch("app.api.routes.mosaics.AsyncSessionLocal", return_value=_AsyncCM(mock_db)),
        patch("app.api.routes.mosaics.stac_catalogs_crud.get_catalog", side_effect=get_cat),
        patch("app.api.routes.mosaics.collect_stac_features", side_effect=capture_collect),
        patch(
            "app.api.routes.mosaics.attach_footprint_displays_to_plan_result",
            new_callable=AsyncMock,
        ),
    ):
        await compute_mosaic_plan(body, user, allow_distributed=False)

    assert captured.get("cloud_cover_max") is None


@pytest.mark.asyncio
async def test_selected_swap_collect_passes_cloud_when_not_same_pass() -> None:
    captured: dict[str, object] = {}

    async def capture_collect(*_args: object, **kwargs: object) -> tuple[list, list]:
        captured["cloud_cover_max"] = kwargs.get("cloud_cover_max")
        return [], []

    mock_db = MagicMock()
    user = MagicMock()
    user.id = 42

    body = MosaicPlanBody(
        catalog_id="stac-local",
        stac_collection_id="sentinel-2-l2a",
        bbox=[0.0, 0.0, 1.0, 1.0],
        date_start="2024-06-01",
        date_end="2024-06-30",
        cloud_cover_max=30.0,
        sort_mode="lowest_cloud",
        use_same_pass_date_strips=False,
        selected=[
            {
                "key": "k",
                "footprint": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            }
        ],
        include_footprint_display=False,
    )

    mock_catalog = MagicMock()
    mock_catalog.enabled = True

    async def get_cat(_db: MagicMock, _cid: str) -> MagicMock:
        return mock_catalog

    with (
        patch("app.api.routes.mosaics.AsyncSessionLocal", return_value=_AsyncCM(mock_db)),
        patch("app.api.routes.mosaics.stac_catalogs_crud.get_catalog", side_effect=get_cat),
        patch("app.api.routes.mosaics.collect_stac_features", side_effect=capture_collect),
        patch(
            "app.api.routes.mosaics.attach_footprint_displays_to_plan_result",
            new_callable=AsyncMock,
        ),
    ):
        await compute_mosaic_plan(body, user, allow_distributed=False)

    assert captured.get("cloud_cover_max") == 30.0
