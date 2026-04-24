"""
UI-only footprint refinement from STAC preview images.

Assumes the preview image covers the Item's WGS84 bbox with north at the top row.
Does not replace canonical footprints used for MosaicJSON (see mosaic_plan).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import numpy as np
from PIL import Image
from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

from app.core.config import get_settings
from app.services.mosaic_plan import thumbnail_href
from app.utils.preview_image import apply_border_transparency_rgba, decode_image_rgba

_ALPHA_MIN = 10
_FETCH_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def _binary_fill_holes(fg: np.ndarray) -> np.ndarray:
    """fg bool True=data. Fill interior False holes surrounded by True."""
    if fg.ndim != 2 or fg.size == 0:
        return fg
    h, w = fg.shape
    bg = ~fg
    visited = np.zeros((h, w), dtype=bool)
    stack: list[tuple[int, int]] = []
    for j in range(w):
        if bg[0, j]:
            stack.append((0, j))
        if bg[h - 1, j]:
            stack.append((h - 1, j))
    for i in range(h):
        if bg[i, 0]:
            stack.append((i, 0))
        if bg[i, w - 1]:
            stack.append((i, w - 1))
    while stack:
        i, j = stack.pop()
        if visited[i, j] or not bg[i, j]:
            continue
        visited[i, j] = True
        if i > 0 and bg[i - 1, j] and not visited[i - 1, j]:
            stack.append((i - 1, j))
        if i + 1 < h and bg[i + 1, j] and not visited[i + 1, j]:
            stack.append((i + 1, j))
        if j > 0 and bg[i, j - 1] and not visited[i, j - 1]:
            stack.append((i, j - 1))
        if j + 1 < w and bg[i, j + 1] and not visited[i, j + 1]:
            stack.append((i, j + 1))
    holes = bg & ~visited
    out = fg.copy()
    out[holes] = True
    return out


def _largest_connected_component(fg: np.ndarray) -> np.ndarray:
    """Largest 4-connected True region (drops small detached blobs)."""
    if fg.ndim != 2 or not np.any(fg):
        return fg
    h, w = fg.shape
    visited = np.zeros((h, w), dtype=bool)
    best: np.ndarray | None = None
    best_n = 0
    for si in range(h):
        for sj in range(w):
            if not fg[si, sj] or visited[si, sj]:
                continue
            stack = [(si, sj)]
            visited[si, sj] = True
            comp = np.zeros((h, w), dtype=bool)
            n = 0
            while stack:
                i, j = stack.pop()
                comp[i, j] = True
                n += 1
                for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                    if 0 <= ni < h and 0 <= nj < w and fg[ni, nj] and not visited[ni, nj]:
                        visited[ni, nj] = True
                        stack.append((ni, nj))
            if n > best_n:
                best_n = n
                best = comp
    return best if best is not None else fg


def _footprint_polygon_from_mask(
    mask: np.ndarray,
    w: int,
    h: int,
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict[str, Any] | None:
    """Vectorize data mask to one outer polygon in WGS84; no interior holes."""
    ys, xs = np.where(mask)
    if ys.size == 0:
        return None
    fw, fh = float(w), float(h)
    dx = (east - west) / fw
    dy = (north - south) / fh
    pix_boxes = []
    for y, x in zip(ys.tolist(), xs.tolist()):
        min_lon = west + x * dx
        max_lon = west + (x + 1) * dx
        max_lat = north - y * dy
        min_lat = north - (y + 1) * dy
        pix_boxes.append(box(min_lon, min_lat, max_lon, max_lat))
    geom = unary_union(pix_boxes)
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        outer = Polygon(geom.exterior)
        return _simplified_extrema_quad_mapping(outer)
    if geom.geom_type == "MultiPolygon":
        poly = max(geom.geoms, key=lambda p: p.area)
        outer = Polygon(poly.exterior)
        return _simplified_extrema_quad_mapping(outer)
    try:
        poly2 = max((g for g in geom.geoms if g.geom_type == "Polygon"), key=lambda p: p.area)
    except Exception:
        return None
    outer = Polygon(poly2.exterior)
    return _simplified_extrema_quad_mapping(outer)


def _simplified_extrema_quad_mapping(poly: Polygon) -> dict[str, Any] | None:
    """
    Build a 4-point polygon from extrema vertices on computed footprint polygon:
    lower-left, upper-left, upper-right, lower-right (from actual boundary vertices).
    This avoids axis-aligned bbox corners while keeping a simple footprint shape.
    """
    if poly.is_empty:
        return None
    coords = list(poly.exterior.coords)
    if len(coords) < 4:
        return mapping(poly)
    # Drop duplicated closing coordinate for selection.
    pts = coords[:-1] if coords[0] == coords[-1] else coords
    if len(pts) < 3:
        return mapping(poly)
    # Pick corners by directional extrema on the polygon boundary (not bbox corners).
    # This is more robust when one corner is not at absolute min/max X.
    ll = min(pts, key=lambda p: (p[0] + p[1], p[0], p[1]))   # lower-left-ish
    ul = max(pts, key=lambda p: (p[1] - p[0], -p[0], p[1]))  # upper-left-ish
    ur = max(pts, key=lambda p: (p[0] + p[1], p[0], p[1]))   # upper-right-ish
    lr = max(pts, key=lambda p: (p[0] - p[1], p[0], -p[1]))  # lower-right-ish
    quad_pts = [ll, ul, ur, lr]
    # Deduplicate in order; fallback to original outer if degenerate.
    uniq: list[tuple[float, float]] = []
    for p in quad_pts:
        if not uniq or p != uniq[-1]:
            uniq.append((float(p[0]), float(p[1])))
    if len(set(uniq)) < 3:
        return mapping(poly)
    q = Polygon(uniq)
    if q.is_empty or not q.is_valid:
        return mapping(poly)
    return mapping(q)


def footprint_display_geojson_from_rgba(
    im: Image.Image,
    bbox4326: list[float],
) -> dict[str, Any] | None:
    """
    Border-mask padding (near-black / near-white), then keep the largest contiguous data blob,
    fill interior nodata holes, and vectorize mask to one outer polygon.
    This intentionally follows PNG alpha/no-data semantics for thumbnail preview.
    """
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    apply_border_transparency_rgba(im)
    w, h = im.size
    if w <= 1 or h <= 1:
        return None
    arr = np.asarray(im)
    alpha = arr[:, :, 3]
    fg = alpha > _ALPHA_MIN
    if not np.any(fg):
        return None
    fg = _largest_connected_component(fg)
    fg = _binary_fill_holes(fg)

    west, south, east, north = (float(bbox4326[i]) for i in range(4))
    if east <= west or north <= south:
        return None

    return _footprint_polygon_from_mask(fg, w, h, west, south, east, north)


def footprint_display_geojson_from_bytes(
    bbox4326: list[float],
    raw: bytes,
) -> dict[str, Any] | None:
    try:
        im = decode_image_rgba(raw)
        return footprint_display_geojson_from_rgba(im, bbox4326)
    except Exception:
        return None


def _feat_by_lookups(features: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    from app.services.mosaic_plan import _dedupe_key

    by_key: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for f in features:
        if not isinstance(f, dict):
            continue
        by_key[str(_dedupe_key(f))] = f
        fid = f.get("id")
        if fid is not None:
            by_id[str(fid)] = f
    return by_key, by_id


def _resolve_feature_for_item(
    item: dict[str, Any],
    by_key: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    k = item.get("key")
    if k is not None and str(k) in by_key:
        return by_key[str(k)]
    iid = item.get("stac_item_id") or item.get("id")
    if iid is not None and str(iid) in by_id:
        return by_id[str(iid)]
    return None


def build_footprint_display_work_specs(
    result: dict[str, Any],
    features: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """
    Build independent thumbnail jobs for distributed footprint_display attachment.

    Each spec is ``{"path": [...], "url": str, "bbox4": [w,s,e,n]}`` where path is either
    ``["selected", index]`` or ``["swap", selected_key, alt_index]`` (matches ``result`` layout).
    """
    if not features or max_items < 1:
        return []
    by_key, by_id = _feat_by_lookups(features)
    specs: list[dict[str, Any]] = []
    sel = result.get("selected") or []
    if isinstance(sel, list):
        for i, it in enumerate(sel):
            if not isinstance(it, dict):
                continue
            feat = _resolve_feature_for_item(it, by_key, by_id)
            if feat is None:
                continue
            b = feat.get("bbox")
            if not isinstance(b, list) or len(b) < 4:
                continue
            try:
                bbox4 = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            except (TypeError, ValueError):
                continue
            url = thumbnail_href(feat)
            if not url or not (str(url).startswith("http://") or str(url).startswith("https://")):
                continue
            specs.append({"path": ["selected", int(i)], "url": str(url), "bbox4": bbox4})
    swaps = result.get("swap_options") or {}
    if isinstance(swaps, dict):
        for sel_key, alts in swaps.items():
            if not isinstance(alts, list):
                continue
            for j, alt in enumerate(alts):
                if not isinstance(alt, dict):
                    continue
                feat = _resolve_feature_for_item(alt, by_key, by_id)
                if feat is None:
                    continue
                b = feat.get("bbox")
                if not isinstance(b, list) or len(b) < 4:
                    continue
                try:
                    bbox4 = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
                except (TypeError, ValueError):
                    continue
                url = thumbnail_href(feat)
                if not url or not (str(url).startswith("http://") or str(url).startswith("https://")):
                    continue
                specs.append({"path": ["swap", str(sel_key), int(j)], "url": str(url), "bbox4": bbox4})
    return specs[:max_items]


def apply_footprint_display_patches(
    result: dict[str, Any],
    patches: list[dict[str, Any]],
) -> None:
    """Write ``footprint_display`` onto ``result`` using patch dicts from workers or local gather."""
    for p in patches:
        if not isinstance(p, dict):
            continue
        path = p.get("path")
        geo = p.get("footprint_display")
        if not isinstance(path, list) or len(path) < 2:
            continue
        if not isinstance(geo, dict):
            continue
        kind = path[0]
        if kind == "selected":
            try:
                idx = int(path[1])
            except (TypeError, ValueError):
                continue
            sel = result.get("selected")
            if not isinstance(sel, list) or idx < 0 or idx >= len(sel):
                continue
            it = sel[idx]
            if isinstance(it, dict):
                it["footprint_display"] = geo
        elif kind == "swap" and len(path) == 3:
            sel_key = str(path[1])
            try:
                j = int(path[2])
            except (TypeError, ValueError):
                continue
            swaps = result.get("swap_options")
            if not isinstance(swaps, dict):
                continue
            alts = swaps.get(sel_key)
            if not isinstance(alts, list) or j < 0 or j >= len(alts):
                continue
            alt = alts[j]
            if isinstance(alt, dict):
                alt["footprint_display"] = geo


async def fetch_footprint_display_geojson(
    url: str,
    bbox4: list[float],
    *,
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any] | None:
    """HTTP GET preview + CPU decode to GeoJSON (shared by local attach and footprint subtasks)."""
    if timeout is None:
        s = get_settings()
        timeout = httpx.Timeout(
            float(s.mosaic_footprint_fetch_read_timeout_seconds or 20.0),
            connect=float(s.mosaic_footprint_fetch_connect_timeout_seconds or 5.0),
        )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={"Accept": "image/*,*/*;q=0.8"})
        except httpx.HTTPError:
            return None
        if r.status_code >= 400:
            return None
    return await asyncio.to_thread(footprint_display_geojson_from_bytes, bbox4, r.content)


async def attach_footprint_displays_to_plan_result(
    result: dict[str, Any],
    features: list[dict[str, Any]],
    *,
    max_concurrent: int | None = None,
) -> None:
    """Mutates result selected + swap_options entries with optional footprint_display."""
    if not features:
        return
    s = get_settings()
    fetch_concurrent = max(1, int(max_concurrent or s.mosaic_footprint_fetch_max_concurrent or 1))
    cpu_concurrent = max(1, int(s.mosaic_footprint_cpu_max_concurrent or 1))
    max_items = max(1, int(s.mosaic_footprint_max_items or 1))
    timeout = httpx.Timeout(
        float(s.mosaic_footprint_fetch_read_timeout_seconds or 20.0),
        connect=float(s.mosaic_footprint_fetch_connect_timeout_seconds or 5.0),
    )
    specs = build_footprint_display_work_specs(result, features, max_items=max_items)
    if not specs:
        return

    cache: dict[tuple[str, float, float, float, float], dict[str, Any] | None] = {}
    fetch_sem = asyncio.Semaphore(fetch_concurrent)
    cpu_sem = asyncio.Semaphore(cpu_concurrent)
    in_flight: dict[tuple[str, float, float, float, float], asyncio.Task[dict[str, Any] | None]] = {}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:

        async def fetch_geo(url: str, bbox4: list[float]) -> dict[str, Any] | None:
            key = (url, bbox4[0], bbox4[1], bbox4[2], bbox4[3])
            if key in cache:
                return cache[key]
            if key in in_flight:
                return await in_flight[key]

            async def _do_fetch() -> dict[str, Any] | None:
                async with fetch_sem:
                    try:
                        r = await client.get(url, headers={"Accept": "image/*,*/*;q=0.8"})
                    except httpx.HTTPError:
                        cache[key] = None
                        return None
                    if r.status_code >= 400:
                        cache[key] = None
                        return None
                async with cpu_sem:
                    geo = await asyncio.to_thread(footprint_display_geojson_from_bytes, bbox4, r.content)
                cache[key] = geo
                return geo

            t = asyncio.create_task(_do_fetch())
            in_flight[key] = t
            try:
                return await t
            finally:
                in_flight.pop(key, None)

        async def run_spec(spec: dict[str, Any]) -> dict[str, Any] | None:
            url = str(spec.get("url") or "")
            bbox4 = spec.get("bbox4")
            path = spec.get("path")
            if not url or not isinstance(bbox4, list) or len(bbox4) < 4 or not isinstance(path, list):
                return None
            geo = await fetch_geo(url, [float(bbox4[i]) for i in range(4)])
            if geo is None:
                return None
            return {"path": path, "footprint_display": geo}

        rows = await asyncio.gather(*[run_spec(sp) for sp in specs])
    patches = [p for p in rows if isinstance(p, dict) and p.get("footprint_display")]
    apply_footprint_display_patches(result, patches)
