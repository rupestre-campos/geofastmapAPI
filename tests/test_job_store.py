"""Tests for app.services.job_store (in-memory backend)."""
import os
import pytest
from app.core.config import get_settings
from app.services.job_store import create_job, get_job, update_job


@pytest.fixture(autouse=True)
def use_memory_job_store(monkeypatch):
    monkeypatch.setenv("BULK_QUEUE_TYPE", "memory")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_create_job():
    job = create_job("coll-1")
    assert job.job_id
    assert job.collection_id == "coll-1"
    assert job.status == "pending"
    assert job.items_created == 0
    assert "created_at" in job.to_dict()


def test_get_job():
    job = create_job("coll-2")
    found = get_job(job.job_id)
    assert found is not None
    assert found.job_id == job.job_id


def test_get_job_unknown_returns_none():
    assert get_job("00000000-0000-0000-0000-000000000000") is None


def test_update_job():
    job = create_job("coll-3")
    update_job(job.job_id, status="running", items_created=5)
    found = get_job(job.job_id)
    assert found.status == "running"
    assert found.items_created == 5


def test_update_job_unknown_returns_none():
    assert update_job("00000000-0000-0000-0000-000000000000", status="running") is None
