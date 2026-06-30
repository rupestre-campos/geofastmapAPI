"""Tests for COPY + staging bulk ingest helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.services import bulk_import_params
from app.services.bulk_copy_ingest import (
    _feature_rows_from_record,
    _is_geojson_seq_path,
)
from app.services.bulk_staging import STAGING_TABLE_PREFIX, staging_table_name


@pytest.fixture
def copy_ingest_enabled(monkeypatch):
    monkeypatch.setattr(
        bulk_import_params,
        "get_settings",
        lambda: type("S", (), {"bulk_copy_ingest_enabled": True})(),
    )


@pytest.fixture
def copy_ingest_disabled(monkeypatch):
    monkeypatch.setattr(
        bulk_import_params,
        "get_settings",
        lambda: type("S", (), {"bulk_copy_ingest_enabled": False})(),
    )


def test_staging_table_name_sanitizes_job_id():
    name = staging_table_name("abc-123-def")
    assert name.startswith(STAGING_TABLE_PREFIX)
    assert "-" not in name


def test_is_geojson_seq_extensions():
    assert _is_geojson_seq_path("/data/foo.geojsonl")
    assert _is_geojson_seq_path("/data/foo.geojsonseq")
    assert not _is_geojson_seq_path("/data/foo.geojson")


def test_feature_rows_from_point():
    now = datetime.now(timezone.utc)
    rec = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
        "properties": {"name": "a"},
    }
    rows, failed = _feature_rows_from_record(
        rec,
        collection_id="test-col",
        job_id="job-1",
        now=now,
        max_vertices=256,
    )
    assert failed == 0
    assert len(rows) == 1
    assert rows[0][0]  # id
    assert rows[0][1] == "test-col"
    assert rows[0][2] == 0  # part_index
    props = json.loads(rows[0][4])
    assert props["name"] == "a"


def test_validate_rejects_replace_filtered_when_copy_enabled(copy_ingest_enabled):
    with pytest.raises(HTTPException) as exc:
        bulk_import_params.validate_bulk_import_mode_and_filters(
            "replace_filtered",
            "state_id:eq:1",
        )
    assert "append" in str(exc.value.detail)


def test_validate_allows_append_replace_when_copy_enabled(copy_ingest_enabled):
    mode, lines = bulk_import_params.validate_bulk_import_mode_and_filters("replace", None)
    assert mode == "replace"
    assert lines == []


def test_validate_replace_filtered_when_legacy_insert(copy_ingest_disabled):
    mode, lines = bulk_import_params.validate_bulk_import_mode_and_filters(
        "replace_filtered",
        ["state_id:eq:12"],
    )
    assert mode == "replace_filtered"
    assert lines == ["state_id:eq:12"]
