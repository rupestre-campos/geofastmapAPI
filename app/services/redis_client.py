"""Shared Redis client factory (socket timeouts tuned for BRPOP consumers)."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings


def make_redis_client(
    *,
    for_brpop: bool = False,
    brpop_timeout: float = 5.0,
    **extra: Any,
):
    """
    Build a redis-py client from REDIS_URL.

    For BRPOP consumers, socket_timeout must exceed the BRPOP wait or redis-py
    raises "Timeout reading from socket" on healthy idle polls.
    """
    import redis

    settings = get_settings()
    kwargs: dict[str, Any] = {"decode_responses": True, **extra}
    connect_timeout = float(
        getattr(settings, "redis_socket_connect_timeout_seconds", 10.0) or 10.0
    )
    kwargs["socket_connect_timeout"] = max(1.0, connect_timeout)

    if for_brpop:
        configured = float(getattr(settings, "redis_brpop_socket_timeout_seconds", 0) or 0)
        kwargs["socket_timeout"] = (
            configured if configured > 0 else max(30.0, float(brpop_timeout) + 15.0)
        )
    else:
        configured = getattr(settings, "redis_socket_timeout_seconds", None)
        if configured is not None:
            sock = float(configured)
            if sock > 0:
                kwargs["socket_timeout"] = sock

    return redis.from_url(settings.redis_url, **kwargs)
