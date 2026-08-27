from app.core.config import get_settings


def test_bulk_worker_max_concurrent_default(monkeypatch):
    monkeypatch.delenv("BULK_WORKER_MAX_CONCURRENT", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().bulk_worker_max_concurrent == 3
    finally:
        get_settings.cache_clear()


def test_bulk_worker_max_concurrent_from_env(monkeypatch):
    monkeypatch.setenv("BULK_WORKER_MAX_CONCURRENT", "6")
    get_settings.cache_clear()
    try:
        assert get_settings().bulk_worker_max_concurrent == 6
    finally:
        get_settings.cache_clear()
