"""Tests for Redis-aware disk self-heal (orphan bulk files, upload parts, tippecanoe tmp)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.services import storage_self_heal as ssh
from app.services.bulk_queue import BULK_IMPORT_REG_PREFIX, QUEUE_KEY, BulkJobPayload
from app.services.bulk_upload_sessions import UPLOAD_SESSION_PREFIX
from app.services.tile_build_queue import TILE_BUILD_PENDING_PREFIX


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)
        return 1

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        if end == -1:
            end = len(items) - 1
        return items[start : end + 1]

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def scan(self, cursor=0, match=None, count=200):
        keys = list(self.kv.keys())
        if match and match.endswith("*"):
            prefix = match[:-1]
            keys = [k for k in keys if k.startswith(prefix)]
        return 0, keys


def _settings(tmp_bulk: Path, tmp_tiles: Path, **extra):
    base = {
        "bulk_queue_type": "redis",
        "bulk_storage_type": "filesystem",
        "bulk_storage_path": str(tmp_bulk),
        "tiles_storage_path": str(tmp_tiles),
        "redis_url": "redis://x",
        "storage_self_heal_enabled": True,
        "storage_self_heal_bulk_grace_seconds": 60.0,
        "storage_self_heal_tile_tmp_grace_seconds": 60.0,
    }
    base.update(extra)
    return type("S", (), base)()


def _touch_old(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_keeps_queued_and_registered_bulk_files(monkeypatch, tmp_path: Path):
    bulk = tmp_path / "bulk"
    tiles = tmp_path / "tiles"
    bulk.mkdir()
    tiles.mkdir()
    r = _FakeRedis()
    monkeypatch.setattr(ssh, "get_settings", lambda: _settings(bulk, tiles))

    keep_q = "queued.geojsonl"
    keep_reg = "running.geojsonl"
    junk = "orphan.geojsonl"
    _touch_old(bulk / keep_q, 10_000)
    _touch_old(bulk / keep_reg, 10_000)
    _touch_old(bulk / junk, 10_000)

    payload = BulkJobPayload(
        job_id="j1",
        collection_id="c1",
        storage_key=keep_q,
        mode="append",
        batch_size=100,
    )
    r.lpush(QUEUE_KEY, payload.to_json())
    r.set(f"{BULK_IMPORT_REG_PREFIX}j2", keep_reg)

    stats = ssh.cleanup_orphan_bulk_files(r=r, grace_seconds=60)
    assert junk in stats.bulk_files_deleted
    assert keep_q not in stats.bulk_files_deleted
    assert keep_reg not in stats.bulk_files_deleted
    assert (bulk / keep_q).exists()
    assert (bulk / keep_reg).exists()
    assert not (bulk / junk).exists()


def test_skips_recent_orphan_bulk_file(monkeypatch, tmp_path: Path):
    bulk = tmp_path / "bulk"
    tiles = tmp_path / "tiles"
    bulk.mkdir()
    tiles.mkdir()
    r = _FakeRedis()
    monkeypatch.setattr(ssh, "get_settings", lambda: _settings(bulk, tiles))

    recent = "fresh.geojsonl"
    _touch_old(bulk / recent, 10)  # younger than grace
    stats = ssh.cleanup_orphan_bulk_files(r=r, grace_seconds=60)
    assert stats.bulk_files_deleted == []
    assert (bulk / recent).exists()


def test_deletes_orphan_upload_dirs_keeps_live_session(monkeypatch, tmp_path: Path):
    bulk = tmp_path / "bulk"
    tiles = tmp_path / "tiles"
    bulk.mkdir()
    tiles.mkdir()
    r = _FakeRedis()
    monkeypatch.setattr(ssh, "get_settings", lambda: _settings(bulk, tiles))

    live = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    dead = "11111111-2222-3333-4444-555555555555"
    live_part = bulk / "_uploads" / live / "part-00000001.bin"
    dead_part = bulk / "_uploads" / dead / "part-00000001.bin"
    _touch_old(live_part, 10_000)
    _touch_old(dead_part, 10_000)
    r.set(f"{UPLOAD_SESSION_PREFIX}{live}", "{}")

    stats = ssh.cleanup_orphan_upload_parts(r=r, grace_seconds=60)
    assert dead in stats.upload_dirs_deleted
    assert live not in stats.upload_dirs_deleted
    assert live_part.exists()
    assert not dead_part.exists()


def test_tile_tmp_keeps_pending_deletes_orphan(monkeypatch, tmp_path: Path):
    bulk = tmp_path / "bulk"
    tiles = tmp_path / "tiles"
    bulk.mkdir()
    tiles.mkdir()
    r = _FakeRedis()
    monkeypatch.setattr(ssh, "get_settings", lambda: _settings(bulk, tiles))

    keep = "car-area.mbtiles.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp"
    junk = "old-layer.mbtiles.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.tmp"
    journal = "old-layer.mbtiles.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.tmp-journal"
    live_mbtiles = "car-area.mbtiles"
    _touch_old(tiles / keep, 10_000)
    _touch_old(tiles / junk, 10_000)
    _touch_old(tiles / journal, 10_000)
    _touch_old(tiles / live_mbtiles, 10_000)
    r.set(f"{TILE_BUILD_PENDING_PREFIX}car-area", "job-1")

    stats = ssh.cleanup_stale_tile_tmp_files(r=r, grace_seconds=60)
    assert junk in stats.tile_tmp_deleted
    assert journal in stats.tile_tmp_deleted
    assert keep not in stats.tile_tmp_deleted
    assert (tiles / keep).exists()
    assert (tiles / live_mbtiles).exists()
    assert not (tiles / junk).exists()
    assert not (tiles / journal).exists()


def test_run_storage_self_heal_respects_enabled_flag(monkeypatch, tmp_path: Path):
    bulk = tmp_path / "bulk"
    tiles = tmp_path / "tiles"
    bulk.mkdir()
    tiles.mkdir()
    r = _FakeRedis()
    monkeypatch.setattr(
        ssh,
        "get_settings",
        lambda: _settings(bulk, tiles, storage_self_heal_enabled=False),
    )
    _touch_old(bulk / "orphan.geojsonl", 10_000)
    stats = ssh.run_storage_self_heal(bulk=True, tiles=True, r=r)
    assert stats.deleted_count == 0
    assert (bulk / "orphan.geojsonl").exists()
