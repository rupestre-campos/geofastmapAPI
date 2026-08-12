"""Admin storage usage: per-collection / mosaic disk+DB sizes with Redis snapshot cache."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings

SNAPSHOT_KEY = "geofastmap:storage_usage:snapshot"
STATUS_KEY = "geofastmap:storage_usage:status"
LOCK_KEY = "geofastmap:storage_usage:lock"
LOCK_TTL_SECONDS = 30 * 60

_compute_lock = threading.Lock()


def format_bytes(n: int | None) -> str:
    if not n or n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.2f} {units[i]}"


def _redis():
    import redis

    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _dir_size_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _file_size_bytes(path: Path | str | None) -> int:
    if not path:
        return 0
    try:
        p = Path(path)
        if p.is_file():
            return int(p.stat().st_size)
    except OSError:
        return 0
    return 0


def collection_mbtiles_path(
    tiles_root: Path,
    collection_id: str,
    pmtiles_path: str | None,
) -> Path | None:
    """Find the MBTiles file for a collection.

    DB ``pmtiles_path`` may be a stale host path from another machine. Always
    fall back to ``{tiles_root}/{collection_id}.mbtiles`` and the basename of
    the stored path under the configured tiles root.
    """
    candidates: list[Path] = []
    if pmtiles_path:
        stored = Path(str(pmtiles_path))
        candidates.append(stored)
        if stored.name:
            candidates.append(tiles_root / stored.name)
    if collection_id:
        candidates.append(tiles_root / f"{collection_id}.mbtiles")
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _features_partition_sizes(conn) -> dict[str, tuple[str, int]]:
    """collection_id -> (partition_relname, pg_total_relation_size)."""
    rows = conn.execute(
        text(
            """
            SELECT c.relname AS relname,
                   pg_get_expr(c.relpartbound, c.oid) AS bound,
                   pg_total_relation_size(c.oid) AS sz
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'features'
            """
        )
    ).fetchall()
    out: dict[str, tuple[str, int]] = {}
    for row in rows:
        b = row.bound or ""
        if "FOR VALUES IN" not in b:
            continue
        try:
            inside = b.split("IN", 1)[1].strip()
            if inside.startswith("(") and inside.endswith(")"):
                inside = inside[1:-1].strip()
            if inside.startswith("'") and inside.endswith("'"):
                cid = inside[1:-1].replace("''", "'")
                out[cid] = (row.relname, int(row.sz or 0))
        except Exception:
            continue
    return out


def _row(
    *,
    kind: str,
    id: str,
    title: str | None = None,
    owner: str | None = None,
    collection_type: str | None = None,
    feature_count: int = 0,
    db_bytes: int = 0,
    tiles_bytes: int = 0,
    raster_bytes: int = 0,
    other_bytes: int = 0,
    can_delete_all: bool = False,
    can_delete_tiles: bool = False,
) -> dict[str, Any]:
    total = int(db_bytes) + int(tiles_bytes) + int(raster_bytes) + int(other_bytes)
    return {
        "kind": kind,
        "id": id,
        "title": title or "",
        "owner": owner or "",
        "collection_type": collection_type or "",
        "feature_count": int(feature_count or 0),
        "db_bytes": int(db_bytes),
        "tiles_bytes": int(tiles_bytes),
        "raster_bytes": int(raster_bytes),
        "other_bytes": int(other_bytes),
        "total_bytes": total,
        "db_h": format_bytes(db_bytes),
        "tiles_h": format_bytes(tiles_bytes),
        "raster_h": format_bytes(raster_bytes),
        "other_h": format_bytes(other_bytes),
        "total_h": format_bytes(total),
        "can_delete_all": bool(can_delete_all),
        "can_delete_tiles": bool(can_delete_tiles),
    }


def compute_storage_usage_sync(engine: Engine | None = None) -> dict[str, Any]:
    """Scan collections, mosaics, and known storage roots. Returns snapshot dict."""
    settings = get_settings()
    own_engine = False
    if engine is None:
        engine = create_engine(settings.database_sync_url, pool_pre_ping=True, future=True)
        own_engine = True

    tiles_root = Path(settings.tiles_storage_path)
    raster_root = Path(settings.raster_storage_path)
    bulk_root = Path(settings.bulk_storage_path)

    rows: list[dict[str, Any]] = []
    known_tile_files: set[str] = set()
    known_raster_dirs: set[str] = set()
    known_mosaic_files: set[str] = set()

    try:
        with engine.connect() as conn:
            part_sizes = _features_partition_sizes(conn)
            coll_rows = conn.execute(
                text(
                    """
                    SELECT c.id, c.title, c.collection_type, c.feature_count,
                           c.owner_id, u.username AS owner_username,
                           ct.pmtiles_path
                    FROM collections c
                    LEFT JOIN users u ON u.id = c.owner_id
                    LEFT JOIN collection_tiles ct ON ct.collection_id = c.id
                    ORDER BY c.id
                    """
                )
            ).fetchall()
            mosaic_rows = conn.execute(
                text(
                    """
                    SELECT rv.id, rv.title, rv.json_relative_path, rv.owner_id,
                           u.username AS owner_username
                    FROM raster_views rv
                    LEFT JOIN users u ON u.id = rv.owner_id
                    ORDER BY rv.id
                    """
                )
            ).fetchall()

        for c in coll_rows:
            cid = str(c.id)
            part = part_sizes.get(cid)
            db_bytes = int(part[1]) if part else 0
            stored_path = str(c.pmtiles_path) if c.pmtiles_path else None
            tiles_path = collection_mbtiles_path(tiles_root, cid, stored_path)
            tiles_bytes = _file_size_bytes(tiles_path)
            if tiles_path is not None:
                known_tile_files.add(tiles_path.name)

            raster_bytes = 0
            raster_dir = raster_root / cid
            if raster_dir.is_dir():
                raster_bytes = _dir_size_bytes(raster_dir)
                known_raster_dirs.add(cid)

            ctype = str(c.collection_type or "vector")
            rows.append(
                _row(
                    kind="collection",
                    id=cid,
                    title=c.title,
                    owner=c.owner_username,
                    collection_type=ctype,
                    feature_count=int(c.feature_count or 0),
                    db_bytes=db_bytes,
                    tiles_bytes=tiles_bytes,
                    raster_bytes=raster_bytes,
                    can_delete_all=True,
                    can_delete_tiles=tiles_bytes > 0 or ctype in ("vector", "composite"),
                )
            )

        for m in mosaic_rows:
            mid = str(m.id)
            rel = str(m.json_relative_path or "")
            mosaic_path = raster_root / rel if rel else None
            other = _file_size_bytes(mosaic_path)
            if mosaic_path and mosaic_path.is_file():
                known_mosaic_files.add(str(mosaic_path.resolve()))
            rows.append(
                _row(
                    kind="mosaic",
                    id=mid,
                    title=m.title,
                    owner=m.owner_username,
                    collection_type="mosaic",
                    other_bytes=other,
                    can_delete_all=True,
                    can_delete_tiles=False,
                )
            )

        # Orphan MBTiles on disk (not linked to a collection row path we already counted)
        if tiles_root.is_dir():
            try:
                for name in os.listdir(tiles_root):
                    if not name.endswith(".mbtiles"):
                        continue
                    if name in known_tile_files:
                        continue
                    path = tiles_root / name
                    if not path.is_file():
                        continue
                    # Skip tippecanoe temps (handled by self-heal)
                    if ".mbtiles." in name and name.endswith(".tmp"):
                        continue
                    rows.append(
                        _row(
                            kind="orphan_tiles",
                            id=name,
                            title="Orphan MBTiles (no collection record)",
                            tiles_bytes=_file_size_bytes(path),
                            can_delete_all=True,
                            can_delete_tiles=True,
                        )
                    )
            except OSError:
                pass

        # Orphan raster collection dirs
        if raster_root.is_dir():
            try:
                for name in os.listdir(raster_root):
                    if name in ("views",) or name.startswith("."):
                        continue
                    if name in known_raster_dirs:
                        continue
                    path = raster_root / name
                    if not path.is_dir():
                        continue
                    rows.append(
                        _row(
                            kind="orphan_rasters",
                            id=name,
                            title="Orphan raster directory",
                            raster_bytes=_dir_size_bytes(path),
                            can_delete_all=True,
                            can_delete_tiles=False,
                        )
                    )
            except OSError:
                pass

            views_dir = raster_root / "views"
            if views_dir.is_dir():
                try:
                    for name in os.listdir(views_dir):
                        path = views_dir / name
                        if not path.is_file():
                            continue
                        try:
                            resolved = str(path.resolve())
                        except OSError:
                            resolved = str(path)
                        if resolved in known_mosaic_files:
                            continue
                        rows.append(
                            _row(
                                kind="orphan_mosaic",
                                id=f"views/{name}",
                                title="Orphan mosaic JSON",
                                other_bytes=_file_size_bytes(path),
                                can_delete_all=True,
                                can_delete_tiles=False,
                            )
                        )
                except OSError:
                    pass

        bulk_bytes = _dir_size_bytes(bulk_root) if bulk_root.is_dir() else 0
        if bulk_bytes > 0:
            rows.append(
                _row(
                    kind="bulk_uploads",
                    id="_bulk_uploads",
                    title="Bulk upload staging (shared)",
                    other_bytes=bulk_bytes,
                    can_delete_all=False,
                    can_delete_tiles=False,
                )
            )

        rows.sort(key=lambda r: (-int(r["total_bytes"]), str(r["id"])))

        totals = {
            "db_bytes": sum(int(r["db_bytes"]) for r in rows),
            "tiles_bytes": sum(int(r["tiles_bytes"]) for r in rows),
            "raster_bytes": sum(int(r["raster_bytes"]) for r in rows),
            "other_bytes": sum(int(r["other_bytes"]) for r in rows),
        }
        totals["total_bytes"] = (
            totals["db_bytes"]
            + totals["tiles_bytes"]
            + totals["raster_bytes"]
            + totals["other_bytes"]
        )
        for k in list(totals.keys()):
            totals[k.replace("_bytes", "_h") if k.endswith("_bytes") else k] = (
                format_bytes(totals[k]) if k.endswith("_bytes") else totals[k]
            )
        totals["total_h"] = format_bytes(totals["total_bytes"])

        return {
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "row_count": len(rows),
            "rows": rows,
            "totals": totals,
            "tiles_root": str(tiles_root),
            "tiles_root_available": tiles_root.is_dir(),
        }
    finally:
        if own_engine:
            engine.dispose()


def get_status() -> dict[str, Any]:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return {"state": "idle", "message": ""}
    try:
        r = _redis()
        raw = r.get(STATUS_KEY)
        if not raw:
            return {"state": "idle", "message": ""}
        return json.loads(raw)
    except Exception:
        return {"state": "idle", "message": ""}


def set_status(state: str, message: str = "") -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return
    try:
        payload = json.dumps(
            {
                "state": state,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        )
        _redis().set(STATUS_KEY, payload, ex=LOCK_TTL_SECONDS + 60)
    except Exception:
        pass


def get_snapshot() -> dict[str, Any] | None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return None
    try:
        raw = _redis().get(SNAPSHOT_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def save_snapshot(snapshot: dict[str, Any]) -> None:
    settings = get_settings()
    ttl = int(getattr(settings, "storage_usage_snapshot_ttl_seconds", 86400 * 8) or 86400 * 8)
    if settings.bulk_queue_type != "redis":
        return
    try:
        _redis().set(SNAPSHOT_KEY, json.dumps(snapshot, separators=(",", ":")), ex=ttl)
    except Exception:
        pass


def _try_acquire_lock() -> bool:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return _compute_lock.acquire(blocking=False)
    try:
        return bool(_redis().set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_SECONDS))
    except Exception:
        return False


def _release_lock() -> None:
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        try:
            _compute_lock.release()
        except RuntimeError:
            pass
        return
    try:
        _redis().delete(LOCK_KEY)
    except Exception:
        pass


def recompute_and_store(*, force: bool = False) -> dict[str, Any]:
    """Recompute usage and persist snapshot. Returns snapshot. Raises if lock held unless force wait."""
    if not _try_acquire_lock():
        existing = get_snapshot()
        status = get_status()
        if existing is not None:
            return {**existing, "status": status, "recompute_skipped": True}
        # No snapshot yet — wait briefly for in-flight compute
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(1)
            if _try_acquire_lock():
                break
            snap = get_snapshot()
            if snap is not None:
                return {**snap, "status": get_status(), "recompute_skipped": True}
        else:
            raise RuntimeError("Storage usage recompute already in progress")

    set_status("computing", "Scanning database and disk…")
    try:
        snapshot = compute_storage_usage_sync()
        save_snapshot(snapshot)
        set_status("idle", "Ready")
        return {**snapshot, "recompute_skipped": False}
    except Exception as e:
        set_status("error", str(e))
        raise
    finally:
        _release_lock()


def tiles_storage_visible() -> bool:
    """True when this process can list the shared MBTiles directory."""
    root = (get_settings().tiles_storage_path or "").rstrip("/")
    return bool(root) and os.path.isdir(root)


def start_recompute_background() -> bool:
    """Start recompute in a daemon thread. Returns False if already computing."""
    if not _try_acquire_lock():
        return False
    # We hold the lock; release so recompute_and_store can acquire — use internal path instead

    def _run() -> None:
        set_status("computing", "Scanning database and disk…")
        try:
            snapshot = compute_storage_usage_sync()
            save_snapshot(snapshot)
            set_status("idle", "Ready")
        except Exception as e:
            set_status("error", str(e))
        finally:
            _release_lock()

    # Lock already held by us; thread will release it
    threading.Thread(target=_run, name="storage-usage-recompute", daemon=True).start()
    return True


def maybe_daily_storage_usage_recompute() -> bool:
    """
    If daily recompute is enabled and the snapshot is from before today's UTC midnight,
    start a background recompute. Returns True if a recompute was started.
    """
    settings = get_settings()
    if not bool(getattr(settings, "storage_usage_daily_recompute", True)):
        return False
    # Bulk workers often do not mount tiles; a scan there would zero every tiles_bytes.
    if not tiles_storage_visible():
        return False
    now = datetime.now(timezone.utc)
    hour = int(getattr(settings, "storage_usage_daily_hour_utc", 0) or 0)
    # Only run the daily job at/after the configured hour
    if now.hour < hour:
        return False
    midnight = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < midnight:
        return False

    snap = get_snapshot()
    if snap and snap.get("computed_at"):
        try:
            computed = datetime.fromisoformat(str(snap["computed_at"]).replace("Z", "+00:00"))
            if computed.tzinfo is None:
                computed = computed.replace(tzinfo=timezone.utc)
            if computed >= midnight:
                return False
        except Exception:
            pass

    status = get_status()
    if status.get("state") == "computing":
        return False
    return start_recompute_background()


def _refresh_snapshot_totals(snap: dict) -> None:
    rows = snap.get("rows") or []
    totals = {
        "db_bytes": sum(int(r["db_bytes"]) for r in rows),
        "tiles_bytes": sum(int(r["tiles_bytes"]) for r in rows),
        "raster_bytes": sum(int(r["raster_bytes"]) for r in rows),
        "other_bytes": sum(int(r["other_bytes"]) for r in rows),
    }
    totals["total_bytes"] = (
        totals["db_bytes"] + totals["tiles_bytes"] + totals["raster_bytes"] + totals["other_bytes"]
    )
    for key in ("db", "tiles", "raster", "other", "total"):
        totals[f"{key}_h"] = format_bytes(totals[f"{key}_bytes"])
    snap["totals"] = totals


def remove_row_from_snapshot(kind: str, item_id: str) -> None:
    snap = get_snapshot()
    if not snap:
        return
    rows = [r for r in (snap.get("rows") or []) if not (r.get("kind") == kind and r.get("id") == item_id)]
    snap["rows"] = rows
    snap["row_count"] = len(rows)
    _refresh_snapshot_totals(snap)
    save_snapshot(snap)


def patch_row_tiles_cleared(collection_id: str) -> None:
    snap = get_snapshot()
    if not snap:
        return
    changed = False
    for r in snap.get("rows") or []:
        if r.get("kind") == "collection" and r.get("id") == collection_id:
            r["tiles_bytes"] = 0
            r["tiles_h"] = format_bytes(0)
            r["total_bytes"] = (
                int(r.get("db_bytes") or 0)
                + int(r.get("raster_bytes") or 0)
                + int(r.get("other_bytes") or 0)
            )
            r["total_h"] = format_bytes(r["total_bytes"])
            r["can_delete_tiles"] = False
            changed = True
            break
    if changed:
        snap["rows"].sort(key=lambda x: (-int(x["total_bytes"]), str(x["id"])))
        _refresh_snapshot_totals(snap)
        save_snapshot(snap)


def patch_row_tiles_and_rasters_cleared(collection_id: str) -> None:
    """After disk phase of collection delete — DB row may still exist briefly."""
    snap = get_snapshot()
    if not snap:
        return
    changed = False
    for r in snap.get("rows") or []:
        if r.get("kind") == "collection" and r.get("id") == collection_id:
            r["tiles_bytes"] = 0
            r["tiles_h"] = format_bytes(0)
            r["raster_bytes"] = 0
            r["raster_h"] = format_bytes(0)
            r["total_bytes"] = int(r.get("db_bytes") or 0) + int(r.get("other_bytes") or 0)
            r["total_h"] = format_bytes(r["total_bytes"])
            r["can_delete_tiles"] = False
            changed = True
            break
    if changed:
        snap["rows"].sort(key=lambda x: (-int(x["total_bytes"]), str(x["id"])))
        _refresh_snapshot_totals(snap)
        save_snapshot(snap)


def patch_mosaic_file_cleared(view_id: str) -> None:
    snap = get_snapshot()
    if not snap:
        return
    changed = False
    for r in snap.get("rows") or []:
        if r.get("kind") == "mosaic" and r.get("id") == view_id:
            r["other_bytes"] = 0
            r["other_h"] = format_bytes(0)
            r["total_bytes"] = 0
            r["total_h"] = format_bytes(0)
            changed = True
            break
    if changed:
        snap["rows"].sort(key=lambda x: (-int(x["total_bytes"]), str(x["id"])))
        _refresh_snapshot_totals(snap)
        save_snapshot(snap)


def delete_orphan_tiles_file(name: str) -> bool:
    """Unlink an orphan MBTiles file. True if gone (deleted or already missing)."""
    settings = get_settings()
    if ".." in name or "/" in name or not name.endswith(".mbtiles"):
        return False
    path = Path(settings.tiles_storage_path) / name
    try:
        if path.is_file():
            path.unlink()
        return not path.exists()
    except OSError:
        return False


def delete_orphan_raster_dir(name: str) -> bool:
    settings = get_settings()
    if ".." in name or "/" in name or name in ("views",):
        return False
    path = Path(settings.raster_storage_path) / name
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return not path.exists()
    except OSError:
        return False
    return False


def delete_orphan_mosaic_file(rel: str) -> bool:
    settings = get_settings()
    if ".." in rel or not rel.startswith("views/"):
        return False
    path = Path(settings.raster_storage_path) / rel
    try:
        if path.is_file():
            path.unlink()
        return not path.exists()
    except OSError:
        return False
    return False


def unlink_collection_tile_files(collection_id: str, pmtiles_path: str | None = None) -> bool:
    """Remove MBTiles for a collection from the configured tiles root (and stored path if local)."""
    settings = get_settings()
    tiles_root = Path(settings.tiles_storage_path)
    found = collection_mbtiles_path(tiles_root, collection_id, pmtiles_path)
    removed = False
    for path in {p for p in (found, tiles_root / f"{collection_id}.mbtiles") if p is not None}:
        try:
            if path.is_file():
                path.unlink()
                removed = True
        except OSError:
            pass
    return removed or not (tiles_root / f"{collection_id}.mbtiles").exists()


def delete_collection_raster_dir(collection_id: str) -> None:
    settings = get_settings()
    if ".." in collection_id or "/" in collection_id:
        return
    path = Path(settings.raster_storage_path) / collection_id
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def delete_mosaic_files(json_relative_path: str | None) -> None:
    if not json_relative_path or ".." in json_relative_path:
        return
    settings = get_settings()
    path = Path(settings.raster_storage_path) / json_relative_path
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
