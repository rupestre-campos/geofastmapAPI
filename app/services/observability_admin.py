"""In-app observability: request logging, retention, and server-load helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[dict[str, Any]] | None = None
_writer_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_last_cleanup_at = 0.0
_runtime_cache: dict[str, Any] | None = None
_runtime_cache_at = 0.0
_runtime_cache_ttl_seconds = 10.0

_OBS_KEYS = {
    "logging_enabled": "observability.logging_enabled",
    "log_debug_mode": "observability.log_debug_mode",
    "log_debug_max_body_bytes": "observability.log_debug_max_body_bytes",
    "log_retention_days": "observability.log_retention_days",
    "metrics_retention_days": "observability.metrics_retention_days",
    "cleanup_interval_seconds": "observability.cleanup_interval_seconds",
    "servers_json": "observability.servers_json",
}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    host = getattr(getattr(request, "client", None), "host", None)
    return host or "unknown"


def _safe_text(v: Any, max_len: int = 4000) -> str:
    if v is None:
        return ""
    s = str(v)
    if len(s) > max_len:
        return s[:max_len] + "...(truncated)"
    return s


def _route_template_from_scope(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


def _to_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        x = v.strip().lower()
        if x in {"1", "true", "yes", "on"}:
            return True
        if x in {"0", "false", "no", "off"}:
            return False
    return default


def _to_int(v: Any, default: int, min_value: int = 0) -> int:
    try:
        out = int(v)
    except Exception:
        return max(min_value, default)
    return max(min_value, out)


async def get_observability_runtime_settings(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return effective observability settings, preferring DB runtime overrides."""
    global _runtime_cache, _runtime_cache_at
    now = time.time()
    if not force_refresh and _runtime_cache is not None and (now - _runtime_cache_at) < _runtime_cache_ttl_seconds:
        return dict(_runtime_cache)

    settings = get_settings()
    out: dict[str, Any] = {
        "logging_enabled": bool(settings.observability_logging_enabled),
        "log_debug_mode": bool(settings.observability_log_debug_mode),
        "log_debug_max_body_bytes": int(settings.observability_log_debug_max_body_bytes),
        "log_retention_days": int(settings.observability_log_retention_days),
        "metrics_retention_days": int(settings.observability_metrics_retention_days),
        "cleanup_interval_seconds": int(settings.observability_cleanup_interval_seconds),
        "servers_json": str(settings.observability_servers_json or "[]"),
    }

    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                text(
                    """
                    SELECT key, value
                    FROM runtime_settings
                    WHERE key = ANY(:keys)
                    """
                ),
                {"keys": list(_OBS_KEYS.values())},
            )
            kv = {row[0]: row[1] for row in r.fetchall()}
        if _OBS_KEYS["logging_enabled"] in kv:
            out["logging_enabled"] = _to_bool(kv[_OBS_KEYS["logging_enabled"]], out["logging_enabled"])
        if _OBS_KEYS["log_debug_mode"] in kv:
            out["log_debug_mode"] = _to_bool(kv[_OBS_KEYS["log_debug_mode"]], out["log_debug_mode"])
        if _OBS_KEYS["log_debug_max_body_bytes"] in kv:
            out["log_debug_max_body_bytes"] = _to_int(
                kv[_OBS_KEYS["log_debug_max_body_bytes"]],
                out["log_debug_max_body_bytes"],
                min_value=128,
            )
        if _OBS_KEYS["log_retention_days"] in kv:
            out["log_retention_days"] = _to_int(kv[_OBS_KEYS["log_retention_days"]], out["log_retention_days"], min_value=1)
        if _OBS_KEYS["metrics_retention_days"] in kv:
            out["metrics_retention_days"] = _to_int(
                kv[_OBS_KEYS["metrics_retention_days"]],
                out["metrics_retention_days"],
                min_value=1,
            )
        if _OBS_KEYS["cleanup_interval_seconds"] in kv:
            out["cleanup_interval_seconds"] = _to_int(
                kv[_OBS_KEYS["cleanup_interval_seconds"]],
                out["cleanup_interval_seconds"],
                min_value=60,
            )
        if _OBS_KEYS["servers_json"] in kv:
            out["servers_json"] = str(kv[_OBS_KEYS["servers_json"]] or "[]")
    except Exception:
        logger.exception("failed loading runtime observability settings; using defaults")

    _runtime_cache = dict(out)
    _runtime_cache_at = now
    return out


async def set_observability_runtime_settings(updates: dict[str, Any]) -> None:
    if not updates:
        return
    allowed = {k: v for k, v in updates.items() if k in _OBS_KEYS}
    if not allowed:
        return
    now = datetime.now(timezone.utc)
    rows = []
    for k, v in allowed.items():
        rows.append({"key": _OBS_KEYS[k], "value": str(v), "updated_at": now})
    async with AsyncSessionLocal() as db:
        for row in rows:
            await db.execute(
                text(
                    """
                    INSERT INTO runtime_settings (key, value, updated_at)
                    VALUES (:key, :value, :updated_at)
                    ON CONFLICT (key) DO UPDATE SET
                      value = EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                row,
            )
        await db.commit()
    await get_observability_runtime_settings(force_refresh=True)


def _should_capture_body(settings: dict[str, Any], request: Request, content_length: int) -> bool:
    if not settings.get("log_debug_mode", False):
        return False
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if content_length <= 0:
        return False
    if content_length > int(settings.get("log_debug_max_body_bytes", 4096)):
        return False
    ct = (request.headers.get("content-type") or "").lower()
    return ("application/json" in ct) or ("application/x-www-form-urlencoded" in ct) or ("text/plain" in ct)


class ObservabilityRequestLogMiddleware(BaseHTTPMiddleware):
    """Capture request/response telemetry into an async queue."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        runtime = await get_observability_runtime_settings()
        if not runtime.get("logging_enabled", True):
            return await call_next(request)

        start = time.perf_counter()
        body_text: str | None = None
        try:
            content_length = int(request.headers.get("content-length") or "0")
        except Exception:
            content_length = 0
        if _should_capture_body(runtime, request, content_length):
            try:
                raw = await request.body()
                body_text = _safe_text(raw.decode("utf-8", errors="replace"), int(runtime.get("log_debug_max_body_bytes", 4096)))
            except Exception:
                body_text = "(failed to read request body)"

        status_code = 500
        try:
            response = await call_next(request)
            status_code = int(response.status_code)
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = max(0, int((time.perf_counter() - start) * 1000))
            session = request.scope.get("session") or {}
            username = session.get("username")
            user_id = session.get("user_id")
            event = {
                "created_at": datetime.now(timezone.utc),
                "method": request.method.upper(),
                "path": request.url.path,
                "route_template": _route_template_from_scope(request),
                "full_url": str(request.url),
                "query_string": request.url.query,
                "client_ip": _client_ip(request),
                "status_code": status_code,
                "latency_ms": latency_ms,
                "user_id": user_id if isinstance(user_id, int) else None,
                "username": username if isinstance(username, str) else None,
                "is_error": status_code >= 500,
                "request_body": body_text,
            }
            if _queue is not None:
                try:
                    _queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("observability queue full; dropping request log event")


async def _insert_batch(batch: list[dict[str, Any]]) -> None:
    if not batch:
        return
    q = text(
        """
        INSERT INTO request_events (
            created_at, method, path, route_template, full_url, query_string, client_ip,
            status_code, latency_ms, user_id, username, is_error, request_body
        ) VALUES (
            :created_at, :method, :path, :route_template, :full_url, :query_string, :client_ip,
            :status_code, :latency_ms, :user_id, :username, :is_error, :request_body
        )
        """
    )
    async with AsyncSessionLocal() as db:
        await db.execute(q, batch)
        await db.commit()


async def _refresh_minute_metrics(window_minutes: int = 120) -> None:
    """Recompute recent minute aggregates from raw events."""
    since = datetime.now(timezone.utc) - timedelta(minutes=max(1, window_minutes))
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM request_metrics_minute WHERE bucket_minute >= :since"),
            {"since": since},
        )
        await db.execute(
            text(
                """
                INSERT INTO request_metrics_minute (
                    bucket_minute, route_template, request_count, mean_ms, p50_ms, p90_ms,
                    status_2xx, status_3xx, status_4xx, status_5xx
                )
                SELECT
                    date_trunc('minute', created_at) AS bucket_minute,
                    route_template,
                    COUNT(*)::int AS request_count,
                    AVG(latency_ms)::int AS mean_ms,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::int AS p50_ms,
                    percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms)::int AS p90_ms,
                    SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END)::int AS status_2xx,
                    SUM(CASE WHEN status_code BETWEEN 300 AND 399 THEN 1 ELSE 0 END)::int AS status_3xx,
                    SUM(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END)::int AS status_4xx,
                    SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END)::int AS status_5xx
                FROM request_events
                WHERE created_at >= :since
                GROUP BY 1, 2
                """
            ),
            {"since": since},
        )
        await db.commit()


async def _cleanup_old_data(settings: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    cutoff_events = now - timedelta(days=max(1, int(settings.get("log_retention_days", 7))))
    cutoff_metrics = now - timedelta(days=max(1, int(settings.get("metrics_retention_days", 30))))
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM request_events WHERE created_at < :cutoff"), {"cutoff": cutoff_events})
        await db.execute(
            text("DELETE FROM request_metrics_minute WHERE bucket_minute < :cutoff"),
            {"cutoff": cutoff_metrics},
        )
        await db.commit()


async def _writer_loop() -> None:
    global _last_cleanup_at
    while _stop_event is not None and not _stop_event.is_set():
        batch: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(_queue.get(), timeout=1.0)  # type: ignore[union-attr]
            batch.append(first)
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.exception("observability writer failed fetching queue item")
            await asyncio.sleep(1.0)
            continue

        if _queue is not None:
            while len(batch) < 200:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
                except Exception:
                    break

        if batch:
            try:
                await _insert_batch(batch)
            except Exception:
                logger.exception("observability batch insert failed")
            try:
                await _refresh_minute_metrics(window_minutes=180)
            except Exception:
                logger.exception("observability minute aggregation refresh failed")

        now_s = time.time()
        runtime = await get_observability_runtime_settings()
        cleanup_interval = max(60, int(runtime.get("cleanup_interval_seconds", 3600)))
        if now_s - _last_cleanup_at >= cleanup_interval:
            try:
                await _cleanup_old_data(runtime)
                _last_cleanup_at = now_s
            except Exception:
                logger.exception("observability cleanup failed")


def init_observability_logging() -> None:
    global _queue, _writer_task, _stop_event
    if _writer_task is not None and not _writer_task.done():
        return
    _queue = asyncio.Queue(maxsize=10000)
    _stop_event = asyncio.Event()
    _writer_task = asyncio.create_task(_writer_loop())


async def shutdown_observability_logging() -> None:
    global _writer_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _writer_task is not None:
        try:
            await asyncio.wait_for(_writer_task, timeout=5.0)
        except Exception:
            pass
    _writer_task = None
    _stop_event = None


async def purge_observability_history(*, older_than_days: int | None = None) -> int:
    async with AsyncSessionLocal() as db:
        if older_than_days is None:
            r = await db.execute(text("DELETE FROM request_events"))
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(older_than_days)))
            r = await db.execute(text("DELETE FROM request_events WHERE created_at < :cutoff"), {"cutoff": cutoff})
        await db.commit()
        return int(r.rowcount or 0)


def local_server_snapshot() -> dict[str, Any]:
    """Very small local snapshot; no extra dependencies."""
    load1, load5, load15 = os.getloadavg()
    mem_total = 0
    mem_avail = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    mem_total = int(ln.split()[1]) * 1024
                elif ln.startswith("MemAvailable:"):
                    mem_avail = int(ln.split()[1]) * 1024
    except Exception:
        pass
    mem_used = max(0, mem_total - mem_avail)
    mem_pct = float((mem_used / mem_total) * 100.0) if mem_total else 0.0
    return {
        "name": "local",
        "source": "local_procfs",
        "cpu_load_1m": round(load1, 2),
        "cpu_load_5m": round(load5, 2),
        "cpu_load_15m": round(load15, 2),
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        "mem_used_percent": round(mem_pct, 2),
        "healthy": True,
    }


def parse_servers_config(settings: dict[str, Any]) -> list[dict[str, str]]:
    raw = str(settings.get("servers_json") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("OBSERVABILITY_SERVERS_JSON is invalid JSON; ignoring")
        return []
    out: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            base_url = str(item.get("base_url") or "").strip().rstrip("/")
            if name and base_url:
                out.append({"name": name, "base_url": base_url})
    return out


async def fetch_server_snapshots() -> list[dict[str, Any]]:
    runtime = await get_observability_runtime_settings()
    servers = parse_servers_config(runtime)
    snapshots = [local_server_snapshot()]
    if not servers:
        return snapshots
    timeout = httpx.Timeout(4.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for srv in servers:
            name = srv["name"]
            base_url = srv["base_url"]
            try:
                # Netdata generic CPU chart endpoint.
                cpu = await client.get(
                    f"{base_url}/api/v1/data",
                    params={"chart": "system.cpu", "after": -60, "points": 1, "format": "json"},
                )
                mem = await client.get(
                    f"{base_url}/api/v1/data",
                    params={"chart": "system.ram", "after": -60, "points": 1, "format": "json"},
                )
                cpu.raise_for_status()
                mem.raise_for_status()
                cpu_val = 0.0
                mem_val = 0.0
                c = cpu.json()
                m = mem.json()
                if isinstance(c, dict) and isinstance(c.get("data"), list) and c["data"]:
                    row = c["data"][-1]
                    if isinstance(row, list) and len(row) > 1:
                        cpu_val = float(row[1] or 0.0)
                if isinstance(m, dict) and isinstance(m.get("data"), list) and m["data"]:
                    row = m["data"][-1]
                    if isinstance(row, list) and len(row) > 1:
                        mem_val = float(row[1] or 0.0)
                snapshots.append(
                    {
                        "name": name,
                        "source": "netdata",
                        "cpu_percent": round(cpu_val, 2),
                        "mem_percent": round(mem_val, 2),
                        "healthy": True,
                    }
                )
            except Exception as e:
                snapshots.append(
                    {
                        "name": name,
                        "source": "netdata",
                        "healthy": False,
                        "error": _safe_text(e, 240),
                    }
                )
    return snapshots
