"""DEM terrain tile smoothing settings (stock Titiler resampling)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_RESAMPLING_ALLOWED = frozenset({"nearest", "bilinear", "cubic", "lanczos", "average", "mode"})


def _normalize_resampling(value: Any, default: str) -> str:
    s = (str(value).strip().lower() if value is not None else "") or default
    return s if s in _RESAMPLING_ALLOWED else default


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def dem_terrain_smooth_settings(collection, *, collection_is_dem: bool | None = None) -> dict[str, Any]:
    """
    Parsed ``raster_settings.dem_terrain_smooth`` with defaults.

    When the collection is DEM and no block exists, smoothing is enabled by default.
    """
    from app.services.raster_titiler_forward import collection_dem_settings

    if collection_is_dem is None:
        collection_is_dem, _ = collection_dem_settings(collection)

    rs = getattr(collection, "raster_settings", None)
    raw = rs.get("dem_terrain_smooth") if isinstance(rs, dict) else None
    raw_dict = raw if isinstance(raw, dict) else {}

    defaults_enabled = bool(collection_is_dem)
    enabled = raw_dict.get("enabled", defaults_enabled)
    if not isinstance(enabled, bool):
        enabled = defaults_enabled

    resampling = _normalize_resampling(raw_dict.get("resampling"), "bilinear")
    reproject = _normalize_resampling(raw_dict.get("reproject"), resampling)

    padding_raw = raw_dict.get("padding")
    padding: int | None
    if padding_raw is None:
        padding = 1 if enabled else None
    else:
        padding = _clamp_int(padding_raw, 0, 0, 8)

    return {
        "enabled": enabled,
        "min_zoom": _clamp_int(raw_dict.get("min_zoom"), 14, 0, 22),
        "resampling": resampling,
        "reproject": reproject,
        "padding": padding,
        "maxzoom": _clamp_int(raw_dict.get("maxzoom"), 14, 0, 22),
    }


def dem_terrain_smooth_demv(smooth: dict[str, Any]) -> str:
    """Stable short cache-bust token from smooth settings (for ``demv`` query param)."""
    payload = {
        "enabled": bool(smooth.get("enabled")),
        "min_zoom": int(smooth.get("min_zoom", 14)),
        "resampling": str(smooth.get("resampling", "bilinear")),
        "reproject": str(smooth.get("reproject", "bilinear")),
        "padding": smooth.get("padding"),
        "maxzoom": int(smooth.get("maxzoom", 14)),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]
    return digest


def append_dem_terrain_smooth_titiler_params(
    params: list[tuple[str, str]],
    *,
    z: int | None,
    kind: str,
    dem_request: bool,
    smooth: dict[str, Any],
) -> None:
    """Append Titiler resampling params for terrain tiles when zoom >= min_zoom."""
    if kind != "tiles" or not dem_request or not smooth.get("enabled"):
        return
    if z is None:
        return
    min_zoom = int(smooth.get("min_zoom", 14))
    if z < min_zoom:
        return

    def _set(key: str, value: str) -> None:
        params[:] = [(k, v) for k, v in params if k != key]
        params.append((key, value))

    _set("resampling", str(smooth.get("resampling", "bilinear")))
    _set("reproject", str(smooth.get("reproject", "bilinear")))
    padding = smooth.get("padding")
    if padding is not None:
        try:
            p = int(padding)
        except (TypeError, ValueError):
            p = None
        if p is not None and p > 0:
            _set("padding", str(p))
        else:
            params[:] = [(k, v) for k, v in params if k != "padding"]
