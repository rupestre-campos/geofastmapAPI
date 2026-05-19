"""Shared Titiler param building for raster collection tiles and point identify."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.crud import features as features_crud
from app.crud import raster_styles as raster_styles_crud
from app.services.coverages import CogPathOutsideStorageError, cog_path_for, resolve_stored_cog_path
from app.services.collection_type_guard import ensure_raster_collection
from app.services.raster_mosaic_version import (
    compute_mosaic_version_id,
    mosaic_mv_matches_request,
)
from app.services.raster_dem_settings import (
    append_dem_terrain_smooth_titiler_params,
    dem_terrain_smooth_demv,
    dem_terrain_smooth_settings,
)
from app.services.raster_style_spec import (
    is_classification_style,
    titiler_params_from_classification_style,
    titiler_params_from_classification_style_for_point,
)

_DEM_ENCODINGS = frozenset({"terrainrgb", "terrarium"})
_SKIP_FORWARD_KEYS = frozenset({"mode", "feature_id", "style_id", "dem_encoding", "mv", "sv", "demv", "lon", "lat"})
# Viz params belong on tile PNG responses, not on /point (we need raw band values).
_POINT_STRIP_CLIENT_KEYS = frozenset(
    {"colormap", "colormap_type", "colormap_name", "rescale", "color_formula", "algorithm"}
)
_MOSAIC_VIZ_KEYS = frozenset(
    {"bidx", "colormap_name", "colormap", "colormap_type", "expression", "color_formula", "assets", "rescale"}
)


def normalize_dem_encoding(value: str | None) -> str:
    enc = (value or "terrainrgb").strip().lower()
    return enc if enc in _DEM_ENCODINGS else "terrainrgb"


def collection_dem_settings(collection) -> tuple[bool, str]:
    rs = getattr(collection, "raster_settings", None)
    if not isinstance(rs, dict):
        return (False, "terrainrgb")
    return (bool(rs.get("is_dem", False)), normalize_dem_encoding(rs.get("dem_encoding")))


def feature_dem_settings(feature) -> tuple[bool, str]:
    props = getattr(feature, "properties", None) or {}
    raster = props.get("raster") if isinstance(props, dict) else None
    if not isinstance(raster, dict):
        return (False, "terrainrgb")
    return (bool(raster.get("is_dem", False)), normalize_dem_encoding(raster.get("dem_encoding")))


@dataclass
class RasterTitilerForward:
    """Titiler upstream path (no base URL) and query params for a collection raster request."""

    forward_path: str
    params: list[tuple[str, str]]
    style_spec: dict = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)


async def raster_collection_item_ids(db: AsyncSession, collection_id: str) -> list[str]:
    from sqlalchemy import text

    q = await db.execute(
        text("SELECT DISTINCT id FROM features WHERE collection_id = :cid ORDER BY id"),
        {"cid": collection_id},
    )
    return [r.id for r in q.fetchall()]


async def prepare_raster_collection_titiler(
    db: AsyncSession,
    request: Request,
    collection_id: str,
    *,
    kind: str,
    lon: float | None = None,
    lat: float | None = None,
    tile_matrix_set_id: str | None = None,
    z: int | None = None,
    x: int | None = None,
    y: int | None = None,
    ext: str | None = None,
    mode: str | None = None,
    feature_id: str | None = None,
    style_id: str | None = None,
) -> RasterTitilerForward:
    """
    Build Titiler forward_path and params for collection raster tiles or point.

    kind: "tiles" | "point"
    """
    settings = get_settings()
    from app.crud import collections as collections_crud

    collection = await collections_crud.get_collection(db, collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    ensure_raster_collection(collection)
    collection_is_dem, collection_dem_encoding = collection_dem_settings(collection)
    dem_smooth = dem_terrain_smooth_settings(collection, collection_is_dem=collection_is_dem)

    titiler = settings.titiler_internal_url.rstrip("/")
    if not titiler:
        raise HTTPException(status_code=503, detail="Titiler not configured")
    secret = settings.titiler_internal_secret
    fetch_base = settings.raster_internal_fetch_base_url.rstrip("/")
    if not secret or not fetch_base:
        raise HTTPException(status_code=503, detail="Titiler internal fetch is not configured")

    item_ids = await raster_collection_item_ids(db, collection_id)
    response_headers: dict[str, str] = {}
    mode_resolved = (mode or ("mosaic" if item_ids else "item")).lower()
    params: list[tuple[str, str]] = []
    dem_algorithm: str | None = None

    if kind == "point":
        if lon is None or lat is None:
            raise HTTPException(status_code=400, detail="lon and lat are required")
        coord = f"{lon},{lat}"
    else:
        coord = None

    if mode_resolved == "item":
        if not feature_id:
            if len(item_ids) == 1:
                feature_id = item_ids[0]
            else:
                raise HTTPException(status_code=400, detail="feature_id is required when mode=item")
        cog_url: str | None = None
        if kind == "point":
            forward_path = f"/cog/point/{coord}"
        else:
            forward_path = f"/cog/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        if feature_id:
            f = await features_crud.get_feature(db, collection_id, feature_id)
            if f:
                props = f.properties or {}
                raster = props.get("raster") if isinstance(props, dict) else None
                cog_val = raster.get("cog_path") if isinstance(raster, dict) else None
                if isinstance(cog_val, str) and cog_val:
                    cog_url = cog_val
                else:
                    cog_url = str(cog_path_for(settings.raster_storage_path, collection_id, feature_id))
                is_dem, dem_enc = feature_dem_settings(f)
                if is_dem or collection_is_dem:
                    dem_algorithm = dem_enc if is_dem else collection_dem_encoding
        if not cog_url:
            cog_url = str(cog_path_for(settings.raster_storage_path, collection_id, feature_id))
        _validate_cog_on_disk(cog_url, settings)
        params.append(("url", cog_url))
    else:
        if not item_ids:
            raise HTTPException(status_code=400, detail="No raster items in this collection for mosaic mode.")
        expected_mv = compute_mosaic_version_id(collection_id, item_ids)
        mv_client_raw = request.query_params.get("mv")
        mv_client = (mv_client_raw or "").strip() or None
        if mv_client is not None and not mosaic_mv_matches_request(mv_client, expected_mv):
            raise HTTPException(
                status_code=400,
                detail="Mosaic version mismatch; reload collection metadata or open a fresh map layer.",
            )
        mosaic_url = (
            f"{fetch_base}/internal/collections/{collection_id}/rasters/mosaic.json"
            f"?token={quote(secret, safe='')}"
        )
        if kind == "point":
            forward_path = f"/mosaicjson/point/{coord}"
        else:
            forward_path = f"/mosaicjson/tiles/{tile_matrix_set_id}/{z}/{x}/{y}.{ext}"
        params.append(("url", mosaic_url))
        # mv is client cache-bust only; Titiler does not accept it on mosaicjson endpoints.
        if kind == "point":
            params.append(("pixel_selection", "first"))
        elif kind == "tiles":
            params.append(("mv", expected_mv))
        if kind == "tiles" and mv_client and mosaic_mv_matches_request(mv_client, expected_mv):
            from app.services.raster_mosaic_version import MOSAIC_TILE_CACHE_CONTROL

            response_headers["Cache-Control"] = MOSAIC_TILE_CACHE_CONTROL
        elif kind == "tiles":
            from app.services.raster_mosaic_version import MOSAIC_TILE_CACHE_CONTROL_LEGACY

            response_headers["Cache-Control"] = MOSAIC_TILE_CACHE_CONTROL_LEGACY

    request_keys = frozenset(request.query_params.keys())
    for k, v in request.query_params.multi_items():
        if k in _SKIP_FORWARD_KEYS:
            continue
        if kind == "point" and k in _POINT_STRIP_CLIENT_KEYS:
            continue
        params.append((k, v))

    dem_encoding_q = request.query_params.get("dem_encoding")
    dem_request = bool(dem_encoding_q)
    style = None
    if style_id and not dem_request:
        style = await raster_styles_crud.get_raster_style(db, collection_id, style_id)
        if style is None:
            style = await raster_styles_crud.get_public_raster_style(db, style_id)
    elif not dem_request:
        style = await raster_styles_crud.get_default_raster_style(db, collection_id)

    style_spec: dict = (style.style_spec if style and isinstance(style.style_spec, dict) else {}) or {}
    _merge_style_into_params(
        params,
        style_spec,
        request_keys,
        dem_request,
        kind=kind,
        mode=mode_resolved,
    )

    if dem_request:
        dem_algorithm = normalize_dem_encoding(dem_encoding_q)

    dem_algorithm = _resolve_dem_algorithm(
        request=request,
        style_spec=style_spec,
        request_keys=request_keys,
        dem_request=dem_request,
        dem_algorithm=dem_algorithm,
        mode=mode_resolved,
        params=params,
        collection_is_dem=collection_is_dem,
        collection_dem_encoding=collection_dem_encoding,
    )
    # Terrain RGB / terrarium algorithm is for *rendered* tiles only. Point reads need raw band
    # samples; sending algorithm breaks mosaic/cog point for DEM collections (empty values, Titiler bugs).
    if dem_algorithm and kind != "point":
        params.append(("algorithm", dem_algorithm))

    if dem_request and kind == "tiles":
        append_dem_terrain_smooth_titiler_params(
            params,
            z=z,
            kind=kind,
            dem_request=True,
            smooth=dem_smooth,
        )
        demv_client = (request.query_params.get("demv") or "").strip()
        demv_expected = dem_terrain_smooth_demv(dem_smooth)
        if not demv_client or demv_client != demv_expected:
            params[:] = [(k, v) for k, v in params if k != "demv"]
            params.append(("demv", demv_expected))

    if kind == "point" and collection_is_dem and not dem_request:
        if not any(k == "bidx" for k, _ in params):
            params.append(("bidx", "1"))

    return RasterTitilerForward(
        forward_path=forward_path,
        params=params,
        style_spec=style_spec,
        response_headers=response_headers,
    )


def _validate_cog_on_disk(cog_url: str, settings: Settings) -> None:
    _cu = str(cog_url).strip()
    if _cu and not _cu.startswith(("http://", "https://", "/vsicurl/", "/vsi")):
        try:
            p_check = resolve_stored_cog_path(_cu, settings.raster_storage_path)
        except CogPathOutsideStorageError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Raster file path is not under server storage.",
            ) from None
        if not p_check.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Raster GeoTIFF file for this item is not present on server storage. "
                    "The catalog row may be orphaned—delete the item and upload again, "
                    "or fix shared volume alignment for the raster worker and API."
                ),
            )


def _merge_style_into_params(
    params: list[tuple[str, str]],
    style_spec: dict,
    request_keys: frozenset[str],
    dem_request: bool,
    *,
    kind: str = "tiles",
    mode: str = "mosaic",
) -> None:
    if not style_spec or dem_request:
        return
    if is_classification_style(style_spec):
        if kind == "point":
            for pk, pv in titiler_params_from_classification_style_for_point(style_spec):
                if pk in request_keys:
                    continue
                params.append((pk, pv))
        else:
            if mode != "mosaic" and "asset" not in request_keys:
                asset_val = style_spec.get("asset")
                if asset_val is not None and str(asset_val).strip():
                    params.append(("asset", str(asset_val).strip()))
            for pk, pv in titiler_params_from_classification_style(style_spec):
                if pk in request_keys:
                    continue
                params.append((pk, pv))
    elif kind == "point":
        for key in ("asset", "assets", "bidx", "expression", "nodata"):
            if key in request_keys:
                continue
            value = style_spec.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                params.append((key, ",".join(str(x) for x in value)))
            else:
                params.append((key, str(value)))
    else:
        for key in ("asset", "assets", "bidx", "rescale", "colormap_name", "expression", "color_formula", "nodata"):
            if key in request_keys:
                continue
            value = style_spec.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                params.append((key, ",".join(str(x) for x in value)))
            else:
                params.append((key, str(value)))


def _resolve_dem_algorithm(
    *,
    request: Request,
    style_spec: dict,
    request_keys: frozenset[str],
    dem_request: bool,
    dem_algorithm: str | None,
    mode: str,
    params: list[tuple[str, str]],
    collection_is_dem: bool,
    collection_dem_encoding: str,
) -> str | None:
    bidx_vals = request.query_params.getlist("bidx")
    assets_vals = request.query_params.getlist("assets")

    def _style_supplies(key: str) -> bool:
        if dem_request:
            return False
        v = style_spec.get(key)
        if v is None or v == "" or v == []:
            return False
        return key not in request_keys

    force_analytic = bool(
        request.query_params.get("expression")
        or request.query_params.get("colormap_name")
        or request.query_params.get("colormap")
        or request.query_params.get("color_formula")
        or len(assets_vals) >= 2
        or _style_supplies("expression")
        or _style_supplies("colormap_name")
        or _style_supplies("colormap")
        or _style_supplies("color_formula")
    )
    if len(bidx_vals) == 1:
        force_analytic = True
    if _style_supplies("bidx"):
        bx = style_spec.get("bidx")
        if isinstance(bx, list) and len(bx) == 1:
            force_analytic = True
        elif isinstance(bx, str) and bx.strip() and "," not in bx.strip():
            force_analytic = True
    if force_analytic and not dem_request:
        return None
    has_mosaic_viz = mode == "mosaic" and any(k in _MOSAIC_VIZ_KEYS for k, _ in params)
    if (
        mode == "mosaic"
        and dem_algorithm is None
        and not force_analytic
        and collection_is_dem
        and not has_mosaic_viz
    ):
        return collection_dem_encoding
    return dem_algorithm
