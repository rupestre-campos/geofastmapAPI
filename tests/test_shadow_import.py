"""Tests for shadow replace import and destructive bulk activity signaling."""
from unittest.mock import MagicMock, patch

import pytest

from app.services import bulk_collection_activity as bca
from app.services import bulk_import as bi
from app.services.shadow_import import (
    active_shadow_exclude_job_ids,
    shadow_distinct_on_order,
    shadow_import_enabled,
    shadow_read_where_sql,
)


def test_shadow_import_enabled_default_false(monkeypatch):
    monkeypatch.setattr(
        "app.services.shadow_import.get_settings",
        lambda: type("S", (), {"bulk_replace_shadow_import": False})(),
    )
    assert shadow_import_enabled() is False


def test_shadow_import_enabled_when_config_true(monkeypatch):
    monkeypatch.setattr(
        "app.services.shadow_import.get_settings",
        lambda: type("S", (), {"bulk_replace_shadow_import": True})(),
    )
    assert shadow_import_enabled() is True


def test_shadow_read_where_sql():
    clause, param = shadow_read_where_sql()
    assert "bulk_import_job_id IS NULL" in clause
    assert param == "shadow_exclude_jobs"


def test_shadow_distinct_on_order_prefers_stable_rows():
    order = shadow_distinct_on_order(False)
    assert "bulk_import_job_id IS NULL) DESC" in order


def test_active_shadow_exclude_job_ids_when_disabled(monkeypatch):
    monkeypatch.setattr("app.services.shadow_import.shadow_import_enabled", lambda: False)
    assert active_shadow_exclude_job_ids("c1") == []


def test_active_shadow_exclude_job_ids_when_enabled(monkeypatch):
    monkeypatch.setattr("app.services.shadow_import.shadow_import_enabled", lambda: True)
    monkeypatch.setattr(
        "app.services.shadow_import.get_active_bulk_job_ids",
        lambda cid: ["job-1"] if cid == "c1" else [],
    )
    assert active_shadow_exclude_job_ids("c1") == ["job-1"]


def test_replace_prestage_skipped_when_shadow(monkeypatch):
    monkeypatch.setattr(bi, "shadow_replace_import_enabled", lambda: True)
    called = {"truncate": False}

    def fake_truncate(*_a, **_k):
        called["truncate"] = True

    monkeypatch.setattr(bi, "_truncate_collection_features_sync", fake_truncate)
    monkeypatch.setattr(bi, "delete_features_by_filters_sync", lambda *_a, **_k: None)
    bi.replace_collection_prestage_sync("c1")
    assert called["truncate"] is False


def test_get_active_bulk_job_ids_includes_mutex_holder(monkeypatch):
    monkeypatch.setattr(bca, "get_collection_bulk_mutex_holder", lambda _cid: "parent-job")
    with patch("app.services.job_store.list_jobs_for_collection", lambda *_a, **_k: []):
        ids = bca.get_active_bulk_job_ids("c1")
    assert ids == ["parent-job"]


def test_finalize_shadow_calls_delete_and_untag(monkeypatch):
    calls = []

    monkeypatch.setattr(bi, "incr_collection_destructive_bulk_activity", lambda cid: calls.append(("incr", cid)))
    monkeypatch.setattr(bi, "decr_collection_destructive_bulk_activity", lambda cid: calls.append(("decr", cid)))
    monkeypatch.setattr(bi, "ensure_features_partition_sync", lambda *_a, **_k: "features_c1")
    monkeypatch.setattr(
        bi,
        "_delete_shadow_stale_rows_sync",
        lambda *_a, **_k: calls.append("delete") or 3,
    )
    monkeypatch.setattr(bi, "_clear_bulk_import_job_tags_sync", lambda *_a, **_k: calls.append("untag"))
    monkeypatch.setattr(bi, "_update_feature_count_sync", lambda *_a, **_k: calls.append("count"))
    monkeypatch.setattr(bi, "_run_db_retry", lambda _label, fn, **_k: fn())
    monkeypatch.setattr(
        bi,
        "get_settings",
        lambda: type("S", (), {"database_sync_url": "postgresql://x", "bulk_extent_update_mode": "deferred"})(),
    )
    monkeypatch.setattr(bi, "create_engine", lambda *_a, **_k: MagicMock(dispose=lambda: None))

    bi.finalize_shadow_collection_import_sync("c1", bulk_import_job_id="job-1")
    assert ("incr", "c1") in calls
    assert "delete" in calls
    assert "untag" in calls
    assert "count" in calls
    assert ("decr", "c1") in calls


def test_finalize_collection_import_sync_uses_shadow_when_enabled(monkeypatch):
    monkeypatch.setattr(bi, "shadow_replace_import_enabled", lambda: True)
    called = {}

    def fake_shadow(*_a, **kwargs):
        called.update(kwargs)

    monkeypatch.setattr(bi, "finalize_shadow_collection_import_sync", fake_shadow)
    bi.finalize_collection_import_sync("c1", bulk_import_job_id="j1", replace_filters=None)
    assert called.get("bulk_import_job_id") == "j1"
