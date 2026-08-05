"""Periodic disk self-heal for shared bulk-upload and tiles volumes.

Workers on multiple hosts share NFS/bind mounts. Interrupted uploads and tippecanoe
builds leave known junk patterns; this module removes only files that Redis no longer
references, then waits a long grace window (default 24h) so outages and slow jobs do
not lose in-flight data.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.services.bulk_queue import BULK_IMPORT_REG_PREFIX, QUEUE_KEY, BulkJobPayload
from app.services.bulk_upload_sessions import UPLOAD_SESSION_PREFIX
from app.services.tile_build_queue import TILE_BUILD_PENDING_PREFIX

# tippecanoe output: {collection_id}.mbtiles.{32hex}.tmp[+ -journal]
_TILE_TMP_RE = re.compile(
    r"^(?P<cid>.+)\.mbtiles\.[0-9a-f]{32}\.tmp(?P<journal>-journal)?$",
    re.IGNORECASE,
)


@dataclass
class SelfHealStats:
    bulk_files_deleted: list[str] = field(default_factory=list)
    upload_dirs_deleted: list[str] = field(default_factory=list)
    tile_tmp_deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return (
            len(self.bulk_files_deleted)
            + len(self.upload_dirs_deleted)
            + len(self.tile_tmp_deleted)
        )


def _redis_client():
    import redis

    settings = get_settings()
    return redis.from_url(settings.redis_url, decode_responses=True)


def _scan_keys(r, match: str, *, count: int = 200) -> list[str]:
    out: list[str] = []
    cursor: int | str = 0
    while True:
        cursor, batch = r.scan(cursor=cursor, match=match, count=count)
        out.extend(batch or [])
        if cursor == 0 or cursor == "0":
            break
    return out


def protected_bulk_storage_keys(r) -> set[str]:
    """Storage keys still needed: queued jobs + registered in-flight imports."""
    keys: set[str] = set()
    try:
        payloads = r.lrange(QUEUE_KEY, 0, -1) or []
    except Exception:
        payloads = []
    for s in payloads:
        try:
            keys.add(BulkJobPayload.from_json(s).storage_key)
        except Exception:
            continue
    for meta_key in _scan_keys(r, f"{BULK_IMPORT_REG_PREFIX}*"):
        try:
            val = r.get(meta_key)
        except Exception:
            continue
        if val:
            keys.add(val)
    return keys


def active_upload_session_ids(r) -> set[str]:
    ids: set[str] = set()
    prefix = UPLOAD_SESSION_PREFIX
    for key in _scan_keys(r, f"{prefix}*"):
        if key.startswith(prefix):
            ids.add(key[len(prefix) :])
    return ids


def collections_with_pending_tile_build(r) -> set[str]:
    ids: set[str] = set()
    prefix = TILE_BUILD_PENDING_PREFIX
    for key in _scan_keys(r, f"{prefix}*"):
        if key.startswith(prefix):
            ids.add(key[len(prefix) :])
    return ids


def _path_age_seconds(path: str) -> float | None:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _dir_age_seconds(path: str) -> float | None:
    """Age from newest file under path; falls back to directory mtime if empty."""
    newest_file: float | None = None
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    m = os.path.getmtime(os.path.join(root, name))
                except OSError:
                    continue
                if newest_file is None or m > newest_file:
                    newest_file = m
    except OSError:
        return None
    if newest_file is not None:
        return time.time() - newest_file
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def cleanup_orphan_bulk_files(
    *,
    r=None,
    grace_seconds: float | None = None,
    dry_run: bool = False,
) -> SelfHealStats:
    """Delete top-level bulk-upload files not referenced by queue or bulk_import_meta."""
    stats = SelfHealStats()
    settings = get_settings()
    if settings.bulk_queue_type != "redis" or settings.bulk_storage_type != "filesystem":
        return stats
    if grace_seconds is None:
        grace_seconds = float(
            getattr(settings, "storage_self_heal_bulk_grace_seconds", 86400.0) or 86400.0
        )
    base = (settings.bulk_storage_path or "").rstrip("/")
    if not base or not os.path.isdir(base):
        return stats
    try:
        client = r if r is not None else _redis_client()
        protected = protected_bulk_storage_keys(client)
    except Exception as e:
        stats.errors.append(f"redis_bulk_keys:{e}")
        return stats

    try:
        names = os.listdir(base)
    except OSError as e:
        stats.errors.append(f"listdir_bulk:{e}")
        return stats

    for name in names:
        if name.startswith(".") or name == "_uploads" or ".." in name or "/" in name:
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        if name in protected:
            continue
        age = _path_age_seconds(path)
        if age is None or age < grace_seconds:
            continue
        if dry_run:
            stats.bulk_files_deleted.append(name)
            continue
        try:
            os.unlink(path)
            stats.bulk_files_deleted.append(name)
        except OSError as e:
            stats.errors.append(f"delete_bulk:{name}:{e}")
    return stats


def cleanup_orphan_upload_parts(
    *,
    r=None,
    grace_seconds: float | None = None,
    dry_run: bool = False,
) -> SelfHealStats:
    """Delete `_uploads/{uuid}/` dirs with no Redis upload session (after grace)."""
    stats = SelfHealStats()
    settings = get_settings()
    if settings.bulk_queue_type != "redis" or settings.bulk_storage_type != "filesystem":
        return stats
    if grace_seconds is None:
        grace_seconds = float(
            getattr(settings, "storage_self_heal_bulk_grace_seconds", 86400.0) or 86400.0
        )
    base = (settings.bulk_storage_path or "").rstrip("/")
    uploads_root = os.path.join(base, "_uploads")
    if not base or not os.path.isdir(uploads_root):
        return stats
    try:
        client = r if r is not None else _redis_client()
        live = active_upload_session_ids(client)
    except Exception as e:
        stats.errors.append(f"redis_upload_sessions:{e}")
        return stats

    try:
        entries = os.listdir(uploads_root)
    except OSError as e:
        stats.errors.append(f"listdir_uploads:{e}")
        return stats

    for upload_id in entries:
        if ".." in upload_id or "/" in upload_id or upload_id.startswith("."):
            continue
        if upload_id in live:
            continue
        path = os.path.join(uploads_root, upload_id)
        if not os.path.isdir(path):
            continue
        age = _dir_age_seconds(path)
        if age is None or age < grace_seconds:
            continue
        if dry_run:
            stats.upload_dirs_deleted.append(upload_id)
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.isdir(path):
                stats.upload_dirs_deleted.append(upload_id)
            else:
                stats.errors.append(f"delete_uploads:{upload_id}:still_exists")
        except OSError as e:
            stats.errors.append(f"delete_uploads:{upload_id}:{e}")
    return stats


def cleanup_stale_tile_tmp_files(
    *,
    r=None,
    grace_seconds: float | None = None,
    dry_run: bool = False,
) -> SelfHealStats:
    """Delete tippecanoe `*.mbtiles.*.tmp` (+ journal) with no pending lease (after grace)."""
    stats = SelfHealStats()
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return stats
    if grace_seconds is None:
        grace_seconds = float(
            getattr(settings, "storage_self_heal_tile_tmp_grace_seconds", 86400.0) or 86400.0
        )
    tiles_dir = (settings.tiles_storage_path or "").rstrip("/")
    if not tiles_dir or not os.path.isdir(tiles_dir):
        return stats
    try:
        client = r if r is not None else _redis_client()
        pending = collections_with_pending_tile_build(client)
    except Exception as e:
        stats.errors.append(f"redis_tile_pending:{e}")
        return stats

    try:
        names = os.listdir(tiles_dir)
    except OSError as e:
        stats.errors.append(f"listdir_tiles:{e}")
        return stats

    for name in names:
        m = _TILE_TMP_RE.match(name)
        if not m:
            continue
        collection_id = m.group("cid")
        if collection_id in pending:
            continue
        path = os.path.join(tiles_dir, name)
        if not os.path.isfile(path):
            continue
        age = _path_age_seconds(path)
        if age is None or age < grace_seconds:
            continue
        if dry_run:
            stats.tile_tmp_deleted.append(name)
            continue
        try:
            os.unlink(path)
            stats.tile_tmp_deleted.append(name)
        except OSError as e:
            stats.errors.append(f"delete_tile_tmp:{name}:{e}")
    return stats


def _merge_stats(into: SelfHealStats, other: SelfHealStats) -> SelfHealStats:
    into.bulk_files_deleted.extend(other.bulk_files_deleted)
    into.upload_dirs_deleted.extend(other.upload_dirs_deleted)
    into.tile_tmp_deleted.extend(other.tile_tmp_deleted)
    into.errors.extend(other.errors)
    return into


def run_storage_self_heal(
    *,
    bulk: bool = True,
    tiles: bool = True,
    r=None,
    dry_run: bool = False,
) -> SelfHealStats:
    """Run configured cleanup passes. Safe to call from any worker with the shared volume."""
    settings = get_settings()
    stats = SelfHealStats()
    if not bool(getattr(settings, "storage_self_heal_enabled", True)):
        return stats
    client = r
    if client is None and settings.bulk_queue_type == "redis":
        try:
            client = _redis_client()
        except Exception as e:
            stats.errors.append(f"redis_connect:{e}")
            return stats
    if bulk:
        _merge_stats(stats, cleanup_orphan_bulk_files(r=client, dry_run=dry_run))
        _merge_stats(stats, cleanup_orphan_upload_parts(r=client, dry_run=dry_run))
    if tiles:
        _merge_stats(stats, cleanup_stale_tile_tmp_files(r=client, dry_run=dry_run))
    return stats


def log_self_heal_stats(label: str, stats: SelfHealStats) -> None:
    if stats.deleted_count == 0 and not stats.errors:
        return
    parts = []
    if stats.bulk_files_deleted:
        parts.append(f"bulk_files={len(stats.bulk_files_deleted)}")
    if stats.upload_dirs_deleted:
        parts.append(f"upload_dirs={len(stats.upload_dirs_deleted)}")
    if stats.tile_tmp_deleted:
        parts.append(f"tile_tmp={len(stats.tile_tmp_deleted)}")
    if stats.errors:
        parts.append(f"errors={len(stats.errors)}")
    print(f"[{label}] storage self-heal: {', '.join(parts) or 'ok'}", flush=True)
    for name in stats.bulk_files_deleted[:20]:
        print(f"[{label}] deleted orphan bulk file: {name}", flush=True)
    for uid in stats.upload_dirs_deleted[:20]:
        print(f"[{label}] deleted orphan upload parts: {uid}", flush=True)
    for name in stats.tile_tmp_deleted[:20]:
        print(f"[{label}] deleted stale tippecanoe tmp: {name}", flush=True)
    for err in stats.errors[:10]:
        print(f"[{label}] self-heal error: {err}", flush=True)
