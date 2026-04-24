"""Unit tests for footprint_display work specs and patch application."""

from __future__ import annotations

from app.services.mosaic_preview_footprint import (
    apply_footprint_display_patches,
    build_footprint_display_work_specs,
)


def test_build_footprint_display_work_specs_selected_and_swap() -> None:
    feat = {
        "id": "item-1",
        "bbox": [-10.0, -5.0, 10.0, 5.0],
        "assets": {"thumbnail": {"href": "https://example.com/t.png"}},
    }
    dk = "::item-1"
    result = {
        "selected": [
            {
                "key": dk,
                "stac_item_id": "item-1",
            }
        ],
        "swap_options": {
            dk: [
                {
                    "key": dk,
                    "stac_item_id": "item-1",
                }
            ],
        },
    }
    specs = build_footprint_display_work_specs(result, [feat], max_items=10)
    assert len(specs) == 2
    assert specs[0]["path"] == ["selected", 0]
    assert specs[0]["url"] == "https://example.com/t.png"
    assert specs[1]["path"] == ["swap", dk, 0]


def test_apply_footprint_display_patches_writes_geo() -> None:
    result = {
        "selected": [{"key": "a"}],
        "swap_options": {"a": [{"key": "b"}]},
    }
    geo = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    apply_footprint_display_patches(
        result,
        [
            {"path": ["selected", 0], "footprint_display": geo},
            {"path": ["swap", "a", 0], "footprint_display": geo},
        ],
    )
    assert result["selected"][0]["footprint_display"] == geo
    assert result["swap_options"]["a"][0]["footprint_display"] == geo


def test_build_respects_max_items() -> None:
    feats = []
    for i in range(5):
        feats.append(
            {
                "id": f"id{i}",
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "assets": {"thumbnail": {"href": f"https://ex/{i}.png"}},
            }
        )
    result = {
        "selected": [{"key": f"::id{i}", "stac_item_id": f"id{i}"} for i in range(5)],
        "swap_options": {},
    }
    specs = build_footprint_display_work_specs(result, feats, max_items=2)
    assert len(specs) == 2
