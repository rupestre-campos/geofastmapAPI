from app.services.redis_resilience import retry_wait_seconds, run_redis_retry


def test_retry_wait_seconds_caps_with_growth():
    w1 = retry_wait_seconds(1, base=1.0, max_seconds=5.0)
    w3 = retry_wait_seconds(3, base=1.0, max_seconds=5.0)
    w8 = retry_wait_seconds(8, base=1.0, max_seconds=5.0)
    assert 1.0 <= w1 <= 5.75
    assert w3 >= w1
    assert w8 <= 5.75


def test_run_redis_retry_succeeds_after_transient_failures(monkeypatch):
    class S:
        redis_retry_base_seconds = 0.01
        redis_retry_max_seconds = 0.02
        redis_retry_enqueue_max_attempts = 5

    from app.services import redis_resilience as rr

    monkeypatch.setattr(rr, "get_settings", lambda: S())

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary")
        return "ok"

    out = run_redis_retry("x", flaky)
    assert out == "ok"
    assert calls["n"] == 3
