"""Watchdog behavior with finalize queue."""

from app.services import bulk_watchdog as bw


def test_fail_stale_running_disabled_when_finalize_queue_default(monkeypatch):
    monkeypatch.setattr(
        bw,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "bulk_watchdog_fail_stale_running": None,
                "bulk_finalize_queue_enabled": True,
            },
        )(),
    )
    assert bw._should_fail_stale_running() is False


def test_fail_stale_running_enabled_when_finalize_off(monkeypatch):
    monkeypatch.setattr(
        bw,
        "get_settings",
        lambda: type(
            "S",
            (),
            {
                "bulk_watchdog_fail_stale_running": None,
                "bulk_finalize_queue_enabled": False,
            },
        )(),
    )
    assert bw._should_fail_stale_running() is True
