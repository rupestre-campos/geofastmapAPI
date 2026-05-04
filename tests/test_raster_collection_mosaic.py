"""Tests for raster collection MosaicJSON href resolution (Titiler)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.raster_collection_mosaic import (
    internal_cog_http_url,
    resolve_mosaic_asset_href,
)


def test_internal_cog_http_url_builds_coverages_path():
    settings = SimpleNamespace(
        titiler_internal_secret="sec ret",
        raster_internal_fetch_base_url="http://api:8000",
    )
    url = internal_cog_http_url(settings, "coll-1", "feat-9")
    assert url is not None
    assert url.startswith("http://api:8000/internal/collections/coll-1/coverages/feat-9/cog?token=")
    assert "sec%20ret" in url or "sec+ret" in url


def test_internal_cog_http_url_requires_secret_and_base():
    assert internal_cog_http_url(SimpleNamespace(titiler_internal_secret="", raster_internal_fetch_base_url="http://x"), "c", "f") is None
    assert internal_cog_http_url(SimpleNamespace(titiler_internal_secret="a", raster_internal_fetch_base_url=""), "c", "f") is None


def test_resolve_mosaic_asset_href_uses_http_when_configured(tmp_path: Path):
    det = tmp_path / "c" / "f" / "x.tif"
    det.parent.mkdir(parents=True, exist_ok=True)
    det.write_bytes(b"fake")
    settings = SimpleNamespace(
        titiler_internal_secret="t",
        raster_internal_fetch_base_url="http://api:8000",
    )
    href = resolve_mosaic_asset_href(
        settings,
        "coll-1",
        "feat-9",
        deterministic_path=det,
        db_cog_path=None,
    )
    assert href is not None
    assert "/internal/collections/coll-1/coverages/feat-9/cog" in href
    assert "token=" in href


def test_resolve_mosaic_asset_href_filesystem_when_http_not_configured(tmp_path: Path):
    det = tmp_path / "a.tif"
    det.write_bytes(b"fake")
    settings = SimpleNamespace(
        titiler_internal_secret="",
        raster_internal_fetch_base_url="",
    )
    href = resolve_mosaic_asset_href(
        settings,
        "coll-1",
        "feat-9",
        deterministic_path=det,
        db_cog_path=None,
    )
    assert href == str(det)


def test_resolve_mosaic_asset_href_none_when_missing_file(tmp_path: Path):
    det = tmp_path / "missing.tif"
    settings = SimpleNamespace(
        titiler_internal_secret="t",
        raster_internal_fetch_base_url="http://api:8000",
    )
    assert (
        resolve_mosaic_asset_href(
            settings,
            "c",
            "f",
            deterministic_path=det,
            db_cog_path=None,
        )
        is None
    )
