"""Zonal statistics via Titiler POST /cog|/stac/statistics with a zone GeoJSON feature."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.permissions import can_see_collection
from app.crud import collections as collections_crud
from app.crud import features as features_crud
from app.models.user import User
from app.services.coverages import CogPathOutsideStorageError, resolve_stored_cog_path
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.titiler_gate import titiler_upstream_gate_run
from app.services.titiler_http import get_titiler_http_client
from app.services.titiler_retry import titiler_execute_with_retry
from app.utils.geo import geometry_to_geojson

# Query keys consumed by our wrapper (not forwarded to Titiler).
ZONAL_STATS_DROP_KEYS = frozenset(
    {
        "zone_collection_id",
        "zone_feature_id",
        "raster_collection_id",
        "raster_feature_id",
        "catalog_id",
        "stac_collection_id",
        "stac_item_id",
        "f",
    }
)

_POLYGON_TYPES = frozenset({"Polygon", "MultiPolygon"})


def _cog_path_from_feature(feature: Any) -> str | None:
    props = feature.properties or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if isinstance(raster, dict):
        p = raster.get("cog_path")
        return p if isinstance(p, str) and p else None
    return None


def _float_or_none(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def unique_values_from_band_stats(band: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive [{value, count}, ...] from Titiler/rio-tiler categorical or histogram shapes."""
    if not isinstance(band, dict):
        return []

    cats = band.get("categories")
    out: list[dict[str, Any]] = []
    if isinstance(cats, list):
        for entry in cats:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                out.append({"value": entry[0], "count": _int_or_none(entry[1]) or 0})
            elif isinstance(entry, dict) and "value" in entry:
                out.append(
                    {
                        "value": entry.get("value"),
                        "count": _int_or_none(entry.get("count")) or 0,
                    }
                )
        if out:
            return out

    hist = band.get("histogram")
    if isinstance(hist, list) and len(hist) >= 2:
        counts, values = hist[0], hist[1]
        if isinstance(counts, list) and isinstance(values, list) and len(counts) == len(values):
            # Categorical histograms use category values as bin edges/labels.
            for val, cnt in zip(values, counts):
                out.append({"value": val, "count": _int_or_none(cnt) or 0})
            return out

    return []


def normalize_band_stats(band: dict[str, Any], *, categorical: bool) -> dict[str, Any]:
    """Stable per-band summary for API clients."""
    if not isinstance(band, dict):
        return {}
    normalized: dict[str, Any] = {
        "min": _float_or_none(band.get("min")),
        "max": _float_or_none(band.get("max")),
        "mean": _float_or_none(band.get("mean")),
        "count": _int_or_none(band.get("count") if band.get("count") is not None else band.get("valid_pixels")),
        "std": _float_or_none(band.get("std") if band.get("std") is not None else band.get("stddev")),
        "sum": _float_or_none(band.get("sum")),
        "median": _float_or_none(band.get("median")),
        "valid_percent": _float_or_none(band.get("valid_percent")),
        "masked_pixels": _int_or_none(band.get("masked_pixels")),
        "valid_pixels": _int_or_none(band.get("valid_pixels")),
        "histogram": band.get("histogram"),
    }
    # Common percentile keys from Titiler GET/POST statistics.
    for key, val in band.items():
        if isinstance(key, str) and key.startswith("percentile_"):
            normalized[key] = _float_or_none(val)
    perc = band.get("percentiles")
    if isinstance(perc, dict):
        for pk, pv in perc.items():
            normalized[f"percentile_{pk}"] = _float_or_none(pv)

    if categorical:
        unique = unique_values_from_band_stats(band)
        if unique:
            normalized["unique_values"] = unique
        if band.get("unique") is not None:
            normalized["unique"] = _int_or_none(band.get("unique"))
        if band.get("majority") is not None:
            normalized["majority"] = band.get("majority")
        if band.get("minority") is not None:
            normalized["minority"] = band.get("minority")
    return normalized


def normalize_titiler_statistics_payload(
    data: Any,
    *,
    categorical: bool,
    raster_meta: dict[str, Any] | None = None,
    zone_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Normalize Titiler POST /statistics GeoJSON (Feature or FeatureCollection) into a stable Feature.
    """
    feature: dict[str, Any] | None = None
    if isinstance(data, dict):
        t = data.get("type")
        if t == "Feature":
            feature = data
        elif t == "FeatureCollection":
            feats = data.get("features") or []
            if feats and isinstance(feats[0], dict):
                feature = feats[0]
        elif "statistics" in data:
            feature = {
                "type": "Feature",
                "geometry": None,
                "properties": {"statistics": data.get("statistics")},
            }

    if feature is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected Titiler statistics response shape",
        )

    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    raw_stats = props.get("statistics") if isinstance(props, dict) else None
    normalized_stats: dict[str, Any] = {}
    if isinstance(raw_stats, dict):
        for band_key, band_val in raw_stats.items():
            if isinstance(band_val, dict):
                normalized_stats[str(band_key)] = normalize_band_stats(band_val, categorical=categorical)
    elif isinstance(raw_stats, list):
        for i, band_val in enumerate(raw_stats):
            if isinstance(band_val, dict):
                normalized_stats[f"b{i + 1}"] = normalize_band_stats(band_val, categorical=categorical)

    out_props: dict[str, Any] = {
        "statistics": normalized_stats,
    }
    if raster_meta:
        out_props["raster"] = raster_meta
    if zone_meta:
        out_props["zone"] = zone_meta

    return {
        "type": "Feature",
        "geometry": feature.get("geometry"),
        "properties": out_props,
    }


async def load_zone_feature_geojson(
    db: AsyncSession,
    zone_collection_id: str,
    zone_feature_id: str,
    current_user: User | None,
    *,
    require_auth_user: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Load a Polygon/MultiPolygon zone from the DB as a GeoJSON Feature.

    Returns (geojson_feature, zone_meta).
    """
    if require_auth_user and current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    collection = await collections_crud.get_collection(db, zone_collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view the zone collection",
        )

    feature = await features_crud.get_feature(db, zone_collection_id, zone_feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone feature not found")

    geom = geometry_to_geojson(feature.geometry)
    if not geom or geom.get("type") not in _POLYGON_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Zone feature must have Polygon or MultiPolygon geometry",
        )

    geojson_feature = {
        "type": "Feature",
        "properties": {"id": zone_feature_id, "collection_id": zone_collection_id},
        "geometry": geom,
    }
    zone_meta = {
        "collection_id": zone_collection_id,
        "feature_id": zone_feature_id,
    }
    return geojson_feature, zone_meta


def resolve_local_cog_url_for_titiler(
    *,
    collection_id: str,
    feature_id: str,
    feature: Any,
) -> str:
    """Build Titiler `url` for a registered raster coverage (HTTP internal or file://).

    Prefer ``file://`` when the COG exists on shared storage. The internal HTTP COG
    endpoint does not support Range requests, which Titiler needs for
    ``/cog/statistics`` — that path yields upstream errors like
    "Range downloading not supported by this server!".
    """
    settings = get_settings()
    cog_path = _cog_path_from_feature(feature)
    if not cog_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")
    try:
        p = resolve_stored_cog_path(cog_path, settings.raster_storage_path)
    except CogPathOutsideStorageError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage") from None
    if not p.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="COG file missing on disk")

    # Shared volume with Titiler (same default as mosaic tiles): file:// works.
    return f"file://{p.resolve()}"


def titiler_base_url() -> str:
    settings = get_settings()
    base = (settings.titiler_internal_url or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Titiler not configured (set TITILER_INTERNAL_URL)",
        )
    return base


def build_titiler_stats_params(
    query_pairs: list[tuple[str, str]],
    *,
    url: str,
    drop_keys: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    drop = ZONAL_STATS_DROP_KEYS if drop_keys is None else (ZONAL_STATS_DROP_KEYS | drop_keys)
    params: list[tuple[str, str]] = [(k, v) for k, v in query_pairs if k not in drop]
    params.append(("url", url))
    # Stable CRS default for API features (EPSG:4326).
    if not any(k == "coord_crs" for k, _ in params):
        params.append(("coord_crs", "epsg:4326"))
    return params


def query_flag_true(pairs: list[tuple[str, str]], name: str) -> bool:
    for k, v in pairs:
        if k == name and str(v).strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


async def post_titiler_zonal_statistics(
    *,
    forward_path: str,
    url: str,
    geojson_feature: dict[str, Any],
    query_pairs: list[tuple[str, str]],
    drop_keys: frozenset[str] | None = None,
    request: Any | None = None,
) -> Any:
    """POST GeoJSON zone to Titiler statistics; return parsed JSON."""
    base = titiler_base_url()
    params = build_titiler_stats_params(query_pairs, url=url, drop_keys=drop_keys)
    stats_url = f"{base}{forward_path}"

    async def _do_post() -> httpx.Response:
        client = get_titiler_http_client()

        async def _request() -> httpx.Response:
            return await client.post(
                stats_url,
                params=params,
                json=geojson_feature,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

        return await titiler_upstream_gate_run(request, _request)

    try:
        resp, _attempts = await titiler_execute_with_retry(_do_post)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Titiler statistics failed: {sanitize_titiler_upstream_error_text(str(e))}",
        ) from e

    if resp.status_code >= 400:
        detail = sanitize_titiler_upstream_error_text(resp.text or f"HTTP {resp.status_code}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Titiler statistics error ({resp.status_code}): {detail}",
        )

    try:
        return resp.json()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Titiler statistics returned non-JSON body",
        ) from e


async def ensure_raster_coverage_feature(
    db: AsyncSession,
    collection_id: str,
    feature_id: str,
    current_user: User | None,
) -> Any:
    """Auth + load raster coverage feature."""
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if not await can_see_collection(db, collection, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this collection",
        )
    feature = await features_crud.get_feature(db, collection_id, feature_id)
    if not feature:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if not _cog_path_from_feature(feature):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item is not a coverage")
    return feature
