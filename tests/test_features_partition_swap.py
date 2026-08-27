"""Tests for partition swap helpers (no DB)."""

from app.db.features_partitions import (
    _partition_bound_literal,
    _safe_partition_name,
)
from app.services.bulk_staging import staging_table_name


def test_safe_partition_name_matches_car_apps_ms():
    name = _safe_partition_name("car-apps-ms")
    assert name.startswith("features_car_apps_ms_")


def test_staging_table_for_failed_job_example():
    jid = "eaaa9f9f-6d52-40e0-bbfc-32410319279e"
    assert staging_table_name(jid).startswith("bulk_staging_eaaa9f9f")


def test_partition_bound_literal_normalizes_spaces():
    lit = _partition_bound_literal("car-consolidated_area-go")
    assert "car-consolidated_area-go" in lit
    assert " " not in lit.replace("FOR", "").replace("VALUES", "").replace("IN", "")

