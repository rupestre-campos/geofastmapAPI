"""Build MosaicJSON asset lists for raster collections consumed by Titiler.

Verification when a second COG does not appear on the map:

1. Ensure Titiler internal fetch is configured (``titiler_internal_secret``,
   ``raster_internal_fetch_base_url``).
2. Request ``GET {raster_internal_fetch_base_url}/internal/collections/{collection_id}/rasters/mosaic.json?token=...``
   using the same secret Titiler uses.
3. Flatten every URL string in the JSON ``tiles`` object (each key is a quadkey).
   Expect one distinct asset path or URL per raster item (filesystem paths by default when COGs
   exist under ``raster_storage_path``; HTTP internal COG URLs only when
   ``RASTER_MOSAIC_ASSET_HREFS_HTTP=true``).
4. If only one URL appears, check API logs for ``raster mosaic skip`` warnings
   (missing file or footprint on disk).
"""

from __future__ import annotations

import logging
import os
import time
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import features as features_crud
from app.services.mosaic_plan import build_mosaicjson_from_footprints
from app.services.coverages import cog_path_for
from app.services.raster_mosaic_version import compute_mosaic_version_id

log = logging.getLogger(__name__)


def internal_cog_http_url(settings: Any, collection_id: str, feature_id: str) -> str | None:
    """Same COG URL shape as :mod:`app.api.routes.titiler_proxy` when using HTTP fetch."""
    secret = (getattr(settings, "titiler_internal_secret", None) or "").strip()
    base = (getattr(settings, "raster_internal_fetch_base_url", None) or "").rstrip("/")
    if not secret or not base:
        return None
    return (
        f"{base}/internal/collections/{collection_id}/coverages/{feature_id}/cog"
        f"?token={quote(secret, safe='')}"
    )


def resolve_mosaic_asset_href(
    settings: Any,
    collection_id: str,
    feature_id: str,
    *,
    deterministic_path: Path,
    db_cog_path: str | None,
) -> str | None:
    """
    Return the ``href`` for one MosaicJSON asset, or None if no COG file is available
    on the API host.

    By default uses **filesystem paths** (same as single-item Titiler tiles): Titiler opens COGs
    from ``raster_storage_path`` when that volume is mounted identically in the Titiler container.

    Set ``raster_mosaic_asset_hrefs_http`` (env ``RASTER_MOSAIC_ASSET_HREFS_HTTP``) to use HTTP
    internal COG URLs instead (only when Titiler cannot share disk with the API).
    """
    viable = False
    if deterministic_path.exists():
        viable = True
    elif isinstance(db_cog_path, str) and db_cog_path and Path(db_cog_path).exists():
        viable = True
    if not viable:
        return None
    http_u = internal_cog_http_url(settings, collection_id, feature_id)
    use_http = bool(getattr(settings, "raster_mosaic_asset_hrefs_http", False) and http_u)
    if use_http:
        return http_u
    if deterministic_path.exists():
        return os.fspath(deterministic_path)
    if isinstance(db_cog_path, str) and db_cog_path and Path(db_cog_path).exists():
        return db_cog_path
    return None


async def collect_raster_collection_mosaic_pairs(
    db: AsyncSession,
    collection_id: str,
    settings: Any,
) -> list[tuple[str, Any]]:
    """
    Load all raster items for a collection and return (href, footprint geometry) pairs
    for :func:`app.services.mosaic_plan.build_mosaicjson_from_footprints`.
    """
    from shapely.geometry import box, shape
    t0 = time.perf_counter()
    rows = await features_crud.list_raster_feature_rows_for_collection(db, collection_id)
    pairs: list[tuple[str, Any]] = []
    skipped_missing_cog = 0
    skipped_missing_geom = 0
    for feature in rows:
        fid = str(feature.id)
        props = feature.properties or {}
        raster = props.get("raster") if isinstance(props, dict) else None
        cog_path = raster.get("cog_path") if isinstance(raster, dict) else None
        det = cog_path_for(settings.raster_storage_path, collection_id, fid)
        href = resolve_mosaic_asset_href(
            settings,
            collection_id,
            fid,
            deterministic_path=det,
            db_cog_path=cog_path if isinstance(cog_path, str) else None,
        )
        if not href:
            skipped_missing_cog += 1
            log.warning(
                "raster mosaic skip: no COG file on API host collection_id=%s feature_id=%s",
                collection_id,
                fid,
            )
            continue

        gj = feature.geometry_geojson if isinstance(feature.geometry_geojson, dict) else None
        geom_shp = None
        if gj:
            try:
                geom_shp = shape(gj)
            except Exception:
                geom_shp = None
        if geom_shp is None or getattr(geom_shp, "is_empty", True):
            meta = (raster or {}).get("meta") if isinstance(raster, dict) else None
            b = meta.get("bounds") if isinstance(meta, dict) else None
            if isinstance(b, (list, tuple)) and len(b) >= 4:
                try:
                    geom_shp = box(float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                except (TypeError, ValueError):
                    geom_shp = None
        if geom_shp is None or getattr(geom_shp, "is_empty", True):
            skipped_missing_geom += 1
            log.warning(
                "raster mosaic skip: no footprint geometry collection_id=%s feature_id=%s",
                collection_id,
                fid,
            )
            continue
        try:
            pairs.append((href, geom_shp))
        except Exception:
            continue
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "raster mosaic pairs built collection_id=%s total=%d pairs=%d skipped_cog=%d skipped_geom=%d elapsed_ms=%d",
        collection_id,
        len(rows),
        len(pairs),
        skipped_missing_cog,
        skipped_missing_geom,
        elapsed_ms,
    )
    return pairs


def _mosaic_cache_key(collection_id: str, mosaic_version_id: str) -> str:
    return f"geofastmap:raster_collection_mosaic:{collection_id}:{mosaic_version_id}"


def _mosaic_cache_ttl_seconds(settings: Any) -> int:
    try:
        ttl = int(getattr(settings, "titiler_mosaic_tile_cache_ttl_seconds", 0) or 0)
    except Exception:
        ttl = 0
    if ttl <= 0:
        try:
            ttl = int(getattr(settings, "titiler_tile_cache_ttl_seconds", 3600) or 3600)
        except Exception:
            ttl = 3600
    return max(120, ttl)


def _redis_client(settings: Any):
    url = getattr(settings, "redis_url", None)
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return redis.from_url(url.strip(), decode_responses=True)
    except Exception:
        return None


async def get_or_build_collection_mosaic(
    db: AsyncSession,
    collection_id: str,
    settings: Any,
    *,
    item_ids: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    ids = list(item_ids or [])
    if not ids:
        rows = await db.execute(
            text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
            {"cid": collection_id},
        )
        ids = [str(r.id) for r in rows.fetchall()]
    mv = compute_mosaic_version_id(collection_id, ids)
    key = _mosaic_cache_key(collection_id, mv)
    r = _redis_client(settings)
    if r is not None:
        try:
            raw = r.get(key)
            if raw:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    payload.setdefault("mosaic_version_id", mv)
                    payload.setdefault("collection_id", collection_id)
                    return payload, mv
        except Exception:
            pass
    pairs = await collect_raster_collection_mosaic_pairs(db, collection_id, settings)
    if not pairs:
        raise ValueError("No valid raster COG items found")
    mosaic = build_mosaicjson_from_footprints(pairs)
    mosaic["mosaic_version_id"] = mv
    mosaic["collection_id"] = collection_id
    if r is not None:
        try:
            r.setex(key, _mosaic_cache_ttl_seconds(settings), json.dumps(mosaic, separators=(",", ":")))
        except Exception:
            pass
    return mosaic, mv
