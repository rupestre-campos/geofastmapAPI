"""STAC mosaic planner: search, greedy AOI coverage, MosaicJSON for Titiler."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import mercantile
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

from app.models.stac_catalog import StacCatalog


def _parse_date(s: str | None) -> date | None:
    if not s or not str(s).strip():
        return None
    t = str(s).strip()[:10]
    try:
        return datetime.strptime(t, "%Y-%m-%d").date()
    except ValueError:
        return None


# Northern hemisphere month ranges (inclusive month indices 1-12)
_SEASON_MONTHS: dict[str, tuple[int, int]] = {
    "spring": (3, 5),
    "summer": (6, 8),
    "autumn": (9, 11),
    "winter": (12, 2),  # Dec–Feb spans year boundary
}

# When no seasons are selected, a single STAC query over a long span is more likely to
# time out or be rejected than several smaller queries (same resilience as season slices).
_NO_SEASON_SPLIT_MIN_DAYS = 31

# Sentinel-2 MGRS tile (e.g. 23KNS): used to filter swap alternatives to the same granule tile.
_MGRS_5 = re.compile(r"^\d{2}[A-Z]{3}$")
# ESA compact naming: ..._T23KNS_...
_RE_TILE_T = re.compile(r"_T(\d{2}[A-Z]{3})_", re.IGNORECASE)
# Short ids: S2A_23KNS_20250810_...
_RE_TILE_S2AB = re.compile(r"(?:^|_)S2[AB]_(\d{2}[A-Z]{3})_", re.IGNORECASE)

# Max swap alternatives per selected image (same MGRS tile + footprint overlap), one API page.
_SWAP_OPTIONS_MAX = 50
# Default page size for swap grids (client may request up to _SWAP_OPTIONS_MAX per request).
_SWAP_OPTIONS_PAGE_DEFAULT = 5


def _slice_swap_alts(
    alts: list[dict[str, Any]],
    sel_key: str,
    *,
    swap_options_limit: int,
    swap_options_offset: dict[str, int] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (page slice, total count)."""
    off = int((swap_options_offset or {}).get(sel_key, 0) or 0)
    if off < 0:
        off = 0
    lim = max(1, min(int(swap_options_limit), _SWAP_OPTIONS_MAX))
    return alts[off : off + lim], len(alts)

# After the initial STAC search, optionally query tighter bboxes over coverage gaps.
_VOID_FILL_MAX_ROUNDS = 6
# Stop extra STAC rounds when uncovered AOI fraction is at or below this (0.1%).
_VOID_FILL_MIN_UNCOVERED = 0.001
# Per void round: sample at most this many disconnected gaps (each gets a small STAC bbox like click-to-fill).
_VOID_PINPOINT_MAX_PARTS = 16
# Same-pass date mode: split AOI into vertical (N–S) longitude strips; each strip picks one day.
# True satellite swaths are oblique; we approximate with meridian-aligned columns (see docstrings).
_SAME_PASS_NUM_STRIPS = 8


def mgrs_tile_from_stac_item_id(item_id: str | None) -> str | None:
    """Extract Sentinel-2 UTM / MGRS 100km tile id (e.g. 23KNS) from a STAC item id string."""
    if not item_id or not str(item_id).strip():
        return None
    s = str(item_id).strip()
    m = _RE_TILE_T.search(s)
    if m:
        t = m.group(1).upper()
        return t if _MGRS_5.match(t) else None
    m = _RE_TILE_S2AB.search(s)
    if m:
        t = m.group(1).upper()
        return t if _MGRS_5.match(t) else None
    return None


def _mgrs_tile_from_saved_item(it: dict[str, Any]) -> str | None:
    """MGRS tile from a saved mosaic row (may only have id + optional mgrs_tile)."""
    if not isinstance(it, dict):
        return None
    v = it.get("mgrs_tile")
    if isinstance(v, str) and v.strip():
        x = v.strip().upper()
        if _MGRS_5.match(x):
            return x
    props = it.get("properties") if isinstance(it.get("properties"), dict) else {}
    return mgrs_tile_from_feature(
        {"id": it.get("stac_item_id") or it.get("id"), "properties": props}
    )


def mgrs_tile_from_feature(feat: dict[str, Any]) -> str | None:
    """Prefer STAC properties (Earth Search / Sentinel), then parse item id."""
    props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
    for key in (
        "mgrs_tile",
        "s2:mgrs_tile",
        "MGRS_TILE",
        "sentinel:mgrs_tile",
        "s2:granule_id",
    ):
        v = props.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.strip().upper()
            # Sometimes full granule id; take last 5-char token that looks like MGRS
            if _MGRS_5.match(raw):
                return raw
            for part in re.split(r"[_\s]+", raw):
                if _MGRS_5.match(part):
                    return part
    iid = feat.get("id")
    if iid is not None:
        t = mgrs_tile_from_stac_item_id(str(iid))
        if t:
            return t
    return None


def _calendar_month_datetime_slices(ds: date, de: date) -> list[str]:
    """Split [ds, de] into one closed datetime interval per calendar month."""
    out: list[str] = []
    cur = date(ds.year, ds.month, 1)
    while cur <= de:
        if cur.month == 12:
            next_m = date(cur.year + 1, 1, 1)
        else:
            next_m = date(cur.year, cur.month + 1, 1)
        month_last = next_m - timedelta(days=1)
        s1 = max(ds, cur)
        e1 = min(de, month_last)
        if s1 <= e1:
            out.append(f"{s1.isoformat()}T00:00:00Z/{e1.isoformat()}T23:59:59Z")
        cur = next_m
    return out


def season_datetime_slices(
    date_start: str | None,
    date_end: str | None,
    seasons: list[str] | None,
) -> list[str]:
    """
    Build STAC `datetime` range strings (one per slice) intersecting [date_start, date_end].
    If seasons is empty/None, returns a single range for the full span.
    """
    ds = _parse_date(date_start)
    de = _parse_date(date_end)
    if ds is None and de is None:
        return []
    # If the user provides only one bound, don't explode the range to date.min/date.max.
    # Many STAC APIs will reject or behave poorly with extreme years.
    if ds is None and de is not None:
        ds = de
    if de is None and ds is not None:
        de = ds
    if de < ds:
        ds, de = de, ds

    if not seasons:
        span_days = (de - ds).days
        if span_days > _NO_SEASON_SPLIT_MIN_DAYS:
            chunks = _calendar_month_datetime_slices(ds, de)
            if chunks:
                return chunks
        return [f"{ds.isoformat()}T00:00:00Z/{de.isoformat()}T23:59:59Z"]

    want = {s.lower().strip() for s in seasons if s}
    slices: list[tuple[date, date]] = []

    y0, y1 = ds.year, de.year
    for year in range(y0, y1 + 1):
        for sn in want:
            if sn not in _SEASON_MONTHS:
                continue
            a, b = _SEASON_MONTHS[sn]
            if sn == "winter":
                # Dec (year) and Jan–Feb (year+1)
                d1 = date(year, 12, 1)
                d2 = date(year, 12, 31)
                if d2 >= ds and d1 <= de:
                    s1, e1 = max(d1, ds), min(d2, de)
                    if s1 <= e1:
                        slices.append((s1, e1))
                d3 = date(year + 1, 1, 1)
                d4 = date(year + 1, 2, 28)
                if d4 >= ds and d3 <= de:
                    s2, e2 = max(d3, ds), min(d4, de)
                    if s2 <= e2:
                        slices.append((s2, e2))
            else:
                d1 = date(year, a, 1)
                last = 31 if b in (1, 3, 5, 7, 8, 10, 12) else 30
                if b == 2:
                    last = 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
                d2 = date(year, b, last)
                if d2 >= ds and d1 <= de:
                    s1, e1 = max(d1, ds), min(d2, de)
                    if s1 <= e1:
                        slices.append((s1, e1))

    if not slices:
        return [f"{ds.isoformat()}T00:00:00Z/{de.isoformat()}T23:59:59Z"]

    slices.sort(key=lambda x: x[0])
    out: list[str] = []
    for s1, e1 in slices:
        out.append(f"{s1.isoformat()}T00:00:00Z/{e1.isoformat()}T23:59:59Z")
    return out


def _feature_geom(feat: dict[str, Any]) -> Polygon | None:
    g = feat.get("geometry")
    if not isinstance(g, dict):
        return None
    try:
        geom = shape(g)
        if geom.is_empty:
            return None
        if geom.geom_type == "Polygon":
            poly = geom  # type: ignore[assignment]
        elif geom.geom_type == "MultiPolygon":
            poly = max(geom.geoms, key=lambda p: p.area)  # type: ignore[union-attr]
        else:
            return None
        if not poly.is_valid:
            poly = make_valid(poly)  # type: ignore[assignment]
            if poly.geom_type != "Polygon":
                return None
        return poly  # type: ignore[return-value]
    except Exception:
        return None


def _stac_bbox_rect(feat: dict[str, Any]) -> Polygon | None:
    """Axis-aligned rectangle from Item `bbox` only (no geometry)."""
    b = feat.get("bbox")
    if isinstance(b, list) and len(b) >= 4:
        try:
            return box(float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        except (TypeError, ValueError):
            pass
    return None


def _footprint_geom(feat: dict[str, Any]) -> Polygon | None:
    """
    Scene footprint for coverage: prefer GeoJSON `geometry` (data extent, no-data excluded upstream);
    fall back to `bbox` rectangle when geometry is missing or unusable.
    """
    g = _feature_geom(feat)
    if g is not None and not g.is_empty:
        return g
    return _stac_bbox_rect(feat)


def primary_raster_href(feat: dict[str, Any]) -> str | None:
    assets = feat.get("assets") or {}
    if not isinstance(assets, dict):
        return None
    preferred = ("visual", "image", "cog", "B04", "red", "green", "blue", "nir")
    for key in preferred:
        a = assets.get(key)
        if isinstance(a, dict):
            href = a.get("href")
            if isinstance(href, str) and href.startswith("http"):
                return href
    for _k, a in assets.items():
        if not isinstance(a, dict):
            continue
        href = a.get("href")
        t = (a.get("type") or "").lower()
        if isinstance(href, str) and href.startswith("http") and ("tif" in href.lower() or "image" in t or "geotiff" in t):
            return href
    return None


def thumbnail_href(feat: dict[str, Any]) -> str | None:
    """Best-effort thumbnail/preview href from STAC assets."""
    assets = feat.get("assets") or {}
    if not isinstance(assets, dict):
        return None
    preferred = ("thumbnail", "thumb", "preview", "rendered_preview", "browse", "overview")
    for key in preferred:
        a = assets.get(key)
        if isinstance(a, dict):
            href = a.get("href")
            if isinstance(href, str) and href.startswith("http"):
                return href
    for _k, a in assets.items():
        if not isinstance(a, dict):
            continue
        href = a.get("href")
        t = (a.get("type") or "").lower()
        roles = a.get("roles") or []
        roles_s = " ".join([str(x).lower() for x in roles]) if isinstance(roles, list) else str(roles).lower()
        if isinstance(href, str) and href.startswith("http") and (
            "thumbnail" in roles_s
            or "preview" in roles_s
            or "png" in t
            or "jpeg" in t
            or "jpg" in t
            or href.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ):
            return href
    return None


def _cloud_cover(feat: dict[str, Any]) -> float | None:
    props = feat.get("properties") or {}
    if not isinstance(props, dict):
        return None
    for k in ("eo:cloud_cover", "cloud_cover", "cloud"):
        v = props.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _item_datetime(feat: dict[str, Any]) -> datetime | None:
    props = feat.get("properties") or {}
    if not isinstance(props, dict):
        return None
    for k in ("datetime", "start_datetime", "end_datetime"):
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s[:19])
            except ValueError:
                pass
    return None


def _dedupe_key(feat: dict[str, Any]) -> str:
    cat = (feat.get("properties") or {}).get("geofast:sourceCatalog") or ""
    coll = feat.get("collection") or (feat.get("properties") or {}).get("collection") or ""
    fid = feat.get("id") or ""
    return f"{cat}:{coll}:{fid}"


@dataclass
class Candidate:
    feature: dict[str, Any]
    geom: Polygon
    href: str
    key: str
    cloud: float | None
    dt: datetime | None


def _swap_alt_dict(other: Candidate) -> dict[str, Any]:
    mt = mgrs_tile_from_feature(other.feature)
    return {
        "key": other.key,
        "id": other.feature.get("id"),
        "href": other.href,
        "thumbnail": thumbnail_href(other.feature),
        "cloud_cover": other.cloud,
        "footprint": mapping(other.geom),
        "mgrs_tile": mt,
    }


def _sort_key_lowest_cloud(c: Candidate) -> tuple[float, str]:
    cc = c.cloud
    if cc is None:
        return (1e9, c.key)
    return (cc, c.key)


def _sort_key_newest(c: Candidate) -> tuple[float, str]:
    if c.dt is None:
        return (-1e9, c.key)
    return (-c.dt.timestamp(), c.key)


def greedy_cover_aoi(
    aoi: Polygon,
    candidates: list[Candidate],
    sort_mode: str,
    *,
    min_coverage: float = 0.995,
    max_iterations: int = 500,
) -> tuple[list[Candidate], Polygon | None, float]:
    """
    Pick a small set of candidates whose footprints cover the AOI (greedy by uncovered area).
    Returns (selected, remaining_uncovered_polygon, uncovered_fraction).
    """
    if not candidates:
        return [], aoi, 1.0
    aoi_area = aoi.area
    if aoi_area <= 0:
        return [], aoi, 1.0

    remaining: Polygon | Any = aoi
    selected: list[Candidate] = []
    used_keys: set[str] = set()
    tie = _sort_key_lowest_cloud if sort_mode == "lowest_cloud" else _sort_key_newest

    for _ in range(max_iterations):
        uncovered_frac = remaining.area / aoi_area if aoi_area > 0 else 0
        if uncovered_frac <= (1.0 - min_coverage) or remaining.is_empty:
            break
        best: Candidate | None = None
        best_gain = 0.0
        best_tie: tuple[Any, ...] | None = None
        for c in candidates:
            if c.key in used_keys:
                continue
            try:
                inter = remaining.intersection(c.geom)
                gain = inter.area if not inter.is_empty else 0.0
            except Exception:
                gain = 0.0
            if gain <= 0:
                continue
            t = tie(c)
            if gain > best_gain + 1e-12 or (
                math.isclose(gain, best_gain, rel_tol=1e-9) and (best_tie is None or t < best_tie)
            ):
                best_gain = gain
                best = c
                best_tie = t
        if best is None:
            break
        selected.append(best)
        used_keys.add(best.key)
        try:
            remaining = remaining.difference(best.geom)
            if remaining.is_empty:
                break
        except Exception:
            break

    uncovered_frac = remaining.area / aoi_area if aoi_area > 0 and not remaining.is_empty else 0.0
    return selected, remaining if not remaining.is_empty else None, uncovered_frac


def _polygonal_part(geom: Any) -> Any | None:
    """Keep only polygonal pieces; drop lines/points from overlays."""
    if geom is None or geom.is_empty:
        return None
    gt = getattr(geom, "geom_type", None)
    if gt in ("Polygon", "MultiPolygon"):
        return geom
    if gt == "GeometryCollection":
        polys = [g for g in geom.geoms if getattr(g, "geom_type", "") in ("Polygon", "MultiPolygon")]
        if not polys:
            return None
        u = unary_union(polys)
        return u if not u.is_empty else None
    return None


def _union_candidate_geoms(selected: list[Candidate]) -> Any | None:
    if not selected:
        return None
    try:
        u = unary_union([c.geom for c in selected])
        return u if not u.is_empty else None
    except Exception:
        return None


def _dedupe_candidates_by_key(cands: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        if c.key in seen:
            continue
        seen.add(c.key)
        out.append(c)
    return out


def _mean_cloud_candidates(cands: list[Candidate]) -> float:
    xs = [c.cloud for c in cands if c.cloud is not None]
    if not xs:
        return 50.0
    return float(sum(xs) / len(xs))


def _candidate_in_date_window(c: Candidate, start: date, end: date) -> bool:
    """True if acquisition UTC calendar date falls in [start, end] inclusive."""
    if c.dt is None:
        return False
    try:
        d = c.dt.date()
        return start <= d <= end
    except Exception:
        return False


def _filter_candidates_to_date_window(
    candidates: list[Candidate], start: date, end: date
) -> list[Candidate]:
    return [c for c in candidates if _candidate_in_date_window(c, start, end)]


def _pick_best_seven_day_window_from_candidates(
    candidates: list[Candidate],
) -> tuple[date, date] | None:
    """
    Sliding 7-day UTC window [anchor-3, anchor+3] inclusive (7 calendar days).
    Not tied to ISO Monday–Sunday: e.g. anchor Sunday includes prior Saturday and following Monday.
    Picks the window with lowest mean eo:cloud_cover (tie-break: more scenes).
    """
    dated = [c for c in candidates if c.dt is not None]
    if not dated:
        return None

    anchor_dates = sorted({c.dt.date() for c in dated})
    best_window: tuple[date, date] | None = None
    best_key: tuple[float, int] | None = None

    for anchor in anchor_dates:
        start = anchor - timedelta(days=3)
        end = anchor + timedelta(days=3)
        in_win = [c for c in dated if start <= c.dt.date() <= end]
        if not in_win:
            continue
        key = (_mean_cloud_candidates(in_win), -len(in_win))
        if best_key is None or key < best_key:
            best_key = key
            best_window = (start, end)

    return best_window


def _aoi_longitude_strips(aoi: Any, n: int) -> list[Any]:
    """
    Vertical (north–south) slices of the AOI: columns between lines of longitude.

    Satellite ground tracks are **oblique** (not meridian-aligned); we approximate each pass-day
    "strip" as one such column so scenes in a column tend to share a pass. Intersection with the AOI
    still yields **full granule footprints** in the interior and **small clipped pieces** at edges —
    greedy cover handles both via footprint geometry.
    """
    try:
        g = unary_union(aoi) if getattr(aoi, "geom_type", None) == "MultiPolygon" else aoi
        if not g.is_valid:
            g = make_valid(g)
    except Exception:
        g = aoi
    if g.is_empty:
        return []
    minx, miny, maxx, maxy = g.bounds
    width = maxx - minx
    if width <= 1e-11:
        return [g] if g.geom_type == "Polygon" else list(g.geoms) if g.geom_type == "MultiPolygon" else []
    n = max(1, min(n, 48))
    eps = max(width * 1e-7, 1e-9)
    strips: list[Any] = []
    for i in range(n):
        x0 = minx + (i / n) * width
        x1 = minx + ((i + 1) / n) * width
        strip = box(x0 - eps, miny - 0.05, x1 + eps, maxy + 0.05)
        try:
            inter = g.intersection(strip)
            if inter.is_empty:
                continue
            if inter.geom_type == "Polygon":
                strips.append(inter)
            elif inter.geom_type == "MultiPolygon":
                for pg in inter.geoms:
                    if not pg.is_empty and pg.area > 0:
                        strips.append(pg)
        except Exception:
            continue
    return strips if strips else ([g] if g.geom_type == "Polygon" else list(g.geoms) if g.geom_type == "MultiPolygon" else [])


def _best_same_day_cover_for_band(
    need: Any,
    pool: list[Candidate],
    sort_mode: str,
) -> list[Candidate]:
    """Among acquisition days, pick the day whose greedy cover minimizes uncovered area on `need`."""
    by_date: dict[date, list[Candidate]] = defaultdict(list)
    no_date: list[Candidate] = []
    for c in pool:
        if c.dt is None:
            no_date.append(c)
            continue
        by_date[c.dt.date()].append(c)

    best_sel: list[Candidate] = []
    best_key: tuple[float, float] | None = None  # (uncovered_frac_on_band, mean_cloud)

    for _d, plist in sorted(by_date.items(), key=lambda kv: (_mean_cloud_candidates(kv[1]), kv[0])):
        sel, _rem, frac = greedy_cover_aoi(need, plist, sort_mode)
        mean_cl = _mean_cloud_candidates(plist)
        key = (float(frac), mean_cl)
        if best_key is None or key < best_key:
            best_key = key
            best_sel = sel

    if no_date:
        sel2, _r2, frac2 = greedy_cover_aoi(need, no_date, sort_mode)
        mean_nd = _mean_cloud_candidates(no_date)
        key2 = (float(frac2), mean_nd)
        if best_key is None or key2 < best_key:
            best_sel = sel2

    return best_sel


def same_pass_date_strips_select(
    aoi: Polygon,
    intersecting: list[Candidate],
    sort_mode: str,
    *,
    num_strips: int = _SAME_PASS_NUM_STRIPS,
) -> tuple[list[Candidate], Polygon | None, float]:
    """
    Each vertical (N–S) longitude strip picks one acquisition day that minimizes uncovered area on
    the **part of that strip not already covered** by granules chosen for earlier strips. Without
    this, excluding already-used keys from the pool made every column pick extra scenes even when
    one wide granule already covered multiple columns (many redundant selections vs. the map).
    Optional gap-fill pass uses any dates for leftover holes.
    """
    bands = _aoi_longitude_strips(aoi, num_strips)
    # West → east (along-track order is approximated; strips are meridian columns, not true swath azimuth)
    bands.sort(key=lambda p: p.centroid.x)

    selected: list[Candidate] = []
    used: set[str] = set()
    aoi_area = float(aoi.area) if aoi.area > 0 else 1.0
    min_need = max(aoi_area * 1e-10, 1e-16)

    for band in bands:
        cu = _union_candidate_geoms(selected)
        try:
            if cu is None:
                need = band
            else:
                need = band.difference(cu)
            need = _polygonal_part(need)
            if need is None or need.is_empty or float(need.area) < min_need:
                continue
        except Exception:
            continue

        pool = []
        for c in intersecting:
            if c.key in used:
                continue
            try:
                if c.geom.intersects(need):
                    pool.append(c)
            except Exception:
                continue
        if not pool:
            continue
        for c in _best_same_day_cover_for_band(need, pool, sort_mode):
            if c.key not in used:
                selected.append(c)
                used.add(c.key)

    if not selected:
        return greedy_cover_aoi(aoi, intersecting, sort_mode)

    try:
        ugeom = unary_union([c.geom for c in selected])
        remaining = aoi.difference(ugeom)
        if remaining.geom_type == "GeometryCollection":
            polys = [x for x in remaining.geoms if getattr(x, "geom_type", "") in ("Polygon", "MultiPolygon")]
            remaining = unary_union(polys) if polys else remaining
        if remaining.is_empty:
            return _dedupe_candidates_by_key(selected), None, 0.0
    except Exception:
        remaining = aoi

    gap_pool = [c for c in intersecting if c.key not in used]
    if gap_pool and not remaining.is_empty:
        try:
            gs, rem_g, _frac_g = greedy_cover_aoi(remaining, gap_pool, sort_mode)
            for c in gs:
                if c.key not in used:
                    selected.append(c)
                    used.add(c.key)
            ugeom = unary_union([c.geom for c in selected])
            remaining = aoi.difference(ugeom)
            if remaining.geom_type == "GeometryCollection":
                polys = [x for x in remaining.geoms if getattr(x, "geom_type", "") in ("Polygon", "MultiPolygon")]
                remaining = unary_union(polys) if polys else remaining
        except Exception:
            pass

    if remaining.is_empty:
        return _dedupe_candidates_by_key(selected), None, 0.0
    uncovered_frac = float(remaining.area / aoi_area) if aoi_area > 0 else 0.0
    return _dedupe_candidates_by_key(selected), remaining, uncovered_frac


def build_mosaicjson_from_footprints(
    items: list[tuple[str, Polygon]],
    *,
    minzoom: int = 6,
    maxzoom: int = 18,
) -> dict[str, Any]:
    """Build MosaicJSON 0.0.3 with `tiles` quadkey index (Titiler / rio-tiler)."""
    if not items:
        raise ValueError("No raster assets for mosaic")
    urls = [x[0] for x in items]
    geoms = [x[1] for x in items]
    union = unary_union(geoms)
    west, south, east, north = union.bounds
    quadkey_zoom = min(14, max(minzoom, 7))
    tiles: dict[str, list[str]] = {}
    for tile in mercantile.tiles(west, south, east, north, [quadkey_zoom]):
        qk = mercantile.quadkey(tile)
        b = mercantile.bounds(tile)
        tb = box(b.west, b.south, b.east, b.north)
        here: list[str] = []
        seen: set[str] = set()
        for url, g in zip(urls, geoms):
            try:
                if tb.intersects(g) and url not in seen:
                    seen.add(url)
                    here.append(url)
            except Exception:
                continue
        if here:
            tiles[qk] = here
    if not tiles:
        qk = mercantile.quadkey(mercantile.Tile(x=0, y=0, z=0))
        tiles[qk] = list(dict.fromkeys(urls))
    return {
        "mosaicjson": "0.0.3",
        "version": "1.0.0",
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "quadkey_zoom": quadkey_zoom,
        "bounds": [west, south, east, north],
        "center": [(west + east) / 2, (south + north) / 2, minzoom],
        "tiles": tiles,
    }


def bbox_union_from_geoms(geoms: list[Polygon]) -> list[float]:
    if not geoms:
        return [-180.0, -85.0, 180.0, 85.0]
    u = unary_union(geoms)
    b = u.bounds
    return [float(b[0]), float(b[1]), float(b[2]), float(b[3])]


def void_search_bbox(remaining: Any, clip_bbox: list[float]) -> list[float] | None:
    """
    Padded bounds of coverage gaps, clipped to the original planner search extent.
    Used to run additional STAC Item Search queries over areas still missing imagery.
    """
    if remaining is None:
        return None
    try:
        if remaining.is_empty:
            return None
        b = remaining.bounds
    except Exception:
        return None
    w = b[2] - b[0]
    h = b[3] - b[1]
    pad = max(w * 0.03, h * 0.03, 0.008)
    inflated = [
        max(-180.0, b[0] - pad),
        max(-85.0, b[1] - pad),
        min(180.0, b[2] + pad),
        min(85.0, b[3] + pad),
    ]
    ox = max(inflated[0], float(clip_bbox[0]))
    oy = max(inflated[1], float(clip_bbox[1]))
    ox2 = min(inflated[2], float(clip_bbox[2]))
    oy2 = min(inflated[3], float(clip_bbox[3]))
    if ox >= ox2 - 1e-9 or oy >= oy2 - 1e-9:
        return None
    return [ox, oy, ox2, oy2]


def _polygon_parts(geom: Any) -> list[Any]:
    """Disconnected polygonal pieces of a coverage gap (MultiPolygon, Polygon, or GeometryCollection)."""
    if geom is None or geom.is_empty:
        return []
    gt = getattr(geom, "geom_type", None)
    if gt == "Polygon":
        return [geom]
    if gt == "MultiPolygon":
        return list(geom.geoms)
    if gt == "GeometryCollection":
        out: list[Any] = []
        try:
            for g in geom.geoms:
                if g.is_empty:
                    continue
                g2 = make_valid(g) if not g.is_valid else g
                if g2.geom_type == "Polygon":
                    out.append(g2)
                elif g2.geom_type == "MultiPolygon":
                    out.extend(g2.geoms)
        except Exception:
            return []
        return out
    return []


def _dedupe_bboxes(bboxes: list[list[float]]) -> list[list[float]]:
    seen: set[tuple[float, float, float, float]] = set()
    out: list[list[float]] = []
    for bb in bboxes:
        if not bb or len(bb) < 4:
            continue
        key = tuple(round(float(x), 6) for x in bb[:4])
        if key in seen:
            continue
        if key[0] >= key[2] - 1e-9 or key[1] >= key[3] - 1e-9:
            continue
        seen.add(key)
        out.append([float(key[0]), float(key[1]), float(key[2]), float(key[3])])
    return out


def pinpoint_bboxes_from_remainder(
    remaining: Any,
    clip_bbox: list[float],
    *,
    max_parts: int = _VOID_PINPOINT_MAX_PARTS,
) -> list[list[float]]:
    """
    Build small WGS84 bboxes around interior points of each gap (same idea as the UI
    "click hole to search"). Large void bboxes often hit STAC limit before returning
    scenes that actually cover small corners; pinpoint queries surface those items.
    """
    try:
        g0 = remaining
        if not g0.is_valid:
            g0 = make_valid(g0)
    except Exception:
        return []
    parts = _polygon_parts(g0)
    if not parts:
        return []
    parts.sort(key=lambda p: p.area, reverse=True)
    parts = parts[:max_parts]

    out: list[list[float]] = []
    for poly in parts:
        try:
            if poly.is_empty:
                continue
            pt = poly.representative_point()
            # ~200 m floor (similar to add-image click), scale up with hole size, cap ~12 km half-width
            ar = float(poly.area)
            half = max(0.0025, min(math.sqrt(max(ar, 1e-12)) * 0.32, 0.11))
            bb = [
                max(-180.0, pt.x - half),
                max(-85.0, pt.y - half),
                min(180.0, pt.x + half),
                min(85.0, pt.y + half),
            ]
            ox = max(bb[0], float(clip_bbox[0]))
            oy = max(bb[1], float(clip_bbox[1]))
            ox2 = min(bb[2], float(clip_bbox[2]))
            oy2 = min(bb[3], float(clip_bbox[3]))
            if ox >= ox2 - 1e-9 or oy >= oy2 - 1e-9:
                continue
            out.append([ox, oy, ox2, oy2])
        except Exception:
            continue
    return _dedupe_bboxes(out)


async def collect_stac_features(
    catalogs: list[StacCatalog],
    *,
    stac_collection: str,
    bbox: list[float],
    datetime_slices: list[str],
    cloud_cover_max: float | None,
    sort_mode: str,
    fetch_limit: int = 500,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Run federated STAC search (one or more datetime slices) and merge/dedupe features."""
    from app.api.routes.stac import execute_stac_search_for_catalogs

    catalog_ids = [c.id for c in catalogs]
    merged_feats: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Track errors across slices, but only report catalogs that never succeeded for any slice.
    errors_all: list[dict[str, str]] = []
    succeeded: set[str] = set()
    for dt in datetime_slices:
        body: dict[str, Any] = {
            "limit": fetch_limit,
            "bbox": bbox,
            "collections": [stac_collection],
            "catalog_ids": catalog_ids,
            "datetime": dt,
        }
        if cloud_cover_max is not None:
            body["query"] = {"eo:cloud_cover": {"lte": float(cloud_cover_max)}}
        part, errs = await execute_stac_search_for_catalogs(catalogs, body)
        errs = errs or []
        if errs:
            errors_all.extend(errs)
        failed_cats = {str(e.get("catalog_id") or "") for e in errs if isinstance(e, dict)}
        for cid in catalog_ids:
            if cid and cid not in failed_cats:
                succeeded.add(cid)
        feats = part.get("features") if isinstance(part, dict) else None
        if not isinstance(feats, list):
            continue
        for f in feats:
            if not isinstance(f, dict):
                continue
            k = _dedupe_key(f)
            if k in seen:
                continue
            seen.add(k)
            merged_feats.append(f)
    # Sort candidates for tie-breaking in greedy (prefilter order)
    cands: list[Candidate] = []
    for f in merged_feats:
        g = _footprint_geom(f)
        if g is None or g.is_empty:
            continue
        href = primary_raster_href(f)
        if not href:
            continue
        cands.append(
            Candidate(
                feature=f,
                geom=g,
                href=href,
                key=_dedupe_key(f),
                cloud=_cloud_cover(f),
                dt=_item_datetime(f),
            )
        )
    if sort_mode == "lowest_cloud":
        cands.sort(key=_sort_key_lowest_cloud)
    else:
        cands.sort(key=_sort_key_newest)
    # Deduplicate catalog errors across datetime slices, but only keep catalogs that never succeeded.
    uniq: dict[str, str] = {}
    for e in errors_all:
        cid = str(e.get("catalog_id") or "")
        if not cid or cid in succeeded:
            continue
        det = str(e.get("detail") or "")
        if cid and cid not in uniq:
            uniq[cid] = det
    errors_out = [{"catalog_id": k, "detail": v} for k, v in uniq.items()]
    return [c.feature for c in cands], errors_out


def plan_mosaic_from_features(
    aoi: Polygon,
    features: list[dict[str, Any]],
    sort_mode: str,
    *,
    same_pass_date_strips: bool = False,
    locked_date_window: tuple[date, date] | None = None,
    swap_options_limit: int = _SWAP_OPTIONS_PAGE_DEFAULT,
    swap_options_offset: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Greedy select + swap options + GeoJSON footprints for the API response."""
    candidates: list[Candidate] = []
    for f in features:
        g = _footprint_geom(f)
        if g is None or g.is_empty:
            continue
        href = primary_raster_href(f)
        if not href:
            continue
        candidates.append(
            Candidate(
                feature=f,
                geom=g,
                href=href,
                key=_dedupe_key(f),
                cloud=_cloud_cover(f),
                dt=_item_datetime(f),
            )
        )
    # Restrict to those intersecting AOI
    intersecting: list[Candidate] = []
    for c in candidates:
        try:
            if c.geom.intersects(aoi):
                intersecting.append(c)
        except Exception:
            continue
    same_seven_day_window_out: dict[str, Any] | None = None
    if same_pass_date_strips:
        if locked_date_window is not None:
            ds, de = locked_date_window
            intersecting = _filter_candidates_to_date_window(intersecting, ds, de)
            same_seven_day_window_out = {
                "start": ds.isoformat(),
                "end": de.isoformat(),
                "locked": True,
            }
        else:
            win = _pick_best_seven_day_window_from_candidates(intersecting)
            if win is not None:
                ds, de = win
                in_win = _filter_candidates_to_date_window(intersecting, ds, de)
                same_seven_day_window_out = {
                    "start": ds.isoformat(),
                    "end": de.isoformat(),
                    "locked": False,
                    "mean_cloud_in_window": round(_mean_cloud_candidates(in_win), 4),
                    "candidates_in_window": len(in_win),
                }
                intersecting = in_win

    if sort_mode == "lowest_cloud":
        intersecting.sort(key=_sort_key_lowest_cloud)
    else:
        intersecting.sort(key=_sort_key_newest)

    if same_pass_date_strips:
        selected, remaining_uncovered, uncovered_frac = same_pass_date_strips_select(
            aoi, intersecting, sort_mode
        )
    else:
        selected, remaining_uncovered, uncovered_frac = greedy_cover_aoi(aoi, intersecting, sort_mode)

    selected_keys = {c.key for c in selected}
    swap_options: dict[str, list[dict[str, Any]]] = {}
    swap_options_total: dict[str, int] = {}
    for sel in selected:
        sel_tile = mgrs_tile_from_feature(sel.feature)
        alts: list[dict[str, Any]] = []
        for other in intersecting:
            if other.key == sel.key:
                continue
            if other.key in selected_keys:
                continue
            try:
                if sel.geom.intersection(other.geom).area <= 0:
                    continue
            except Exception:
                continue
            if sel_tile:
                ot = mgrs_tile_from_feature(other.feature)
                if ot != sel_tile:
                    continue
            alts.append(_swap_alt_dict(other))
        page, total = _slice_swap_alts(
            alts,
            sel.key,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        swap_options[sel.key] = page
        swap_options_total[sel.key] = total

    footprints = []
    for c in selected:
        props = {
            "key": c.key,
            "id": c.feature.get("id"),
            "href": c.href,
            "cloud_cover": c.cloud,
            "geofast:sourceCatalog": (c.feature.get("properties") or {}).get("geofast:sourceCatalog"),
            "collection": c.feature.get("collection"),
        }
        footprints.append(
            {
                "type": "Feature",
                "geometry": mapping(c.geom),
                "properties": props,
            }
        )

    selected_payload = []
    for c in selected:
        props = c.feature.get("properties") if isinstance(c.feature.get("properties"), dict) else {}
        selected_payload.append(
            {
                "key": c.key,
                "catalog_id": props.get("geofast:sourceCatalog"),
                "stac_collection_id": c.feature.get("collection") or props.get("collection"),
                "stac_item_id": c.feature.get("id"),
                "href": c.href,
                "thumbnail": thumbnail_href(c.feature),
                "cloud_cover": c.cloud,
                "footprint": mapping(c.geom),
                "mgrs_tile": mgrs_tile_from_feature(c.feature),
            }
        )

    rem_geo: dict[str, Any] | None = None
    if remaining_uncovered is not None and not remaining_uncovered.is_empty:
        try:
            rem_geo = mapping(remaining_uncovered)
        except Exception:
            rem_geo = None

    out: dict[str, Any] = {
        "selected": selected_payload,
        "footprints": {"type": "FeatureCollection", "features": footprints},
        "swap_options": swap_options,
        "swap_options_total": swap_options_total,
        "uncovered_fraction": uncovered_frac,
        "candidates_matched": len(intersecting),
        "remaining_uncovered": rem_geo,
        "use_same_pass_date_strips": bool(same_pass_date_strips),
        "same_seven_day_window": same_seven_day_window_out,
    }
    return out


async def plan_mosaic_with_void_fill(
    catalogs: list[StacCatalog],
    *,
    stac_collection: str,
    aoi: Polygon,
    search_bbox: list[float],
    datetime_slices: list[str],
    cloud_cover_max: float | None,
    sort_mode: str,
    fetch_limit: int,
    same_pass_date_strips: bool = False,
    swap_options_limit: int = _SWAP_OPTIONS_PAGE_DEFAULT,
    swap_options_offset: dict[str, int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    """
    Run an initial STAC search over the full AOI bbox, plan coverage, then repeat STAC search
    over uncovered gaps until the AOI is nearly covered, no new items appear, or max rounds is reached.

    Later rounds use **pinpoint** bboxes: one small search per disconnected gap (via
    `representative_point`), matching the mosaic planner "click hole to search" behavior. A single
    large void bbox often hits the STAC item limit with scenes that do not cover small corners.
    """
    merged: dict[str, dict[str, Any]] = {}
    all_errors: list[dict[str, str]] = []
    last_result: dict[str, Any] | None = None
    locked_date_window: tuple[date, date] | None = None

    for round_idx in range(_VOID_FILL_MAX_ROUNDS):
        if round_idx == 0:
            q_bboxes = [[float(x) for x in search_bbox]]
        else:
            assert last_result is not None
            uf = float(last_result.get("uncovered_fraction") or 1.0)
            if uf <= _VOID_FILL_MIN_UNCOVERED:
                last_result["void_fill_stopped"] = "coverage_met"
                break
            rem = last_result.get("remaining_uncovered")
            if not rem:
                last_result["void_fill_stopped"] = "no_remaining_geometry"
                break
            try:
                rem_g = shape(rem)
            except Exception:
                last_result["void_fill_stopped"] = "invalid_remaining"
                break
            # Pinpoint each gap (like UI click-to-fill); avoids STAC limit crowding out
            # granules that only appear under small intersection bboxes.
            pin = pinpoint_bboxes_from_remainder(rem_g, search_bbox)
            if pin:
                q_bboxes = pin
            else:
                vb = void_search_bbox(rem_g, search_bbox)
                if vb is None:
                    last_result["void_fill_stopped"] = "void_bbox_degenerate"
                    break
                q_bboxes = [vb]

        if not q_bboxes:
            break

        n_before = len(merged)
        for q_bbox in q_bboxes:
            feats, errs = await collect_stac_features(
                catalogs,
                stac_collection=stac_collection,
                bbox=q_bbox,
                datetime_slices=datetime_slices,
                cloud_cover_max=cloud_cover_max,
                sort_mode=sort_mode,
                fetch_limit=fetch_limit,
            )
            all_errors.extend(errs)
            for f in feats:
                if isinstance(f, dict):
                    merged[_dedupe_key(f)] = f

        if round_idx > 0 and len(merged) == n_before:
            if last_result is not None:
                last_result["void_fill_stopped"] = "no_new_features"
            break

        last_result = plan_mosaic_from_features(
            aoi,
            list(merged.values()),
            sort_mode,
            same_pass_date_strips=same_pass_date_strips,
            locked_date_window=locked_date_window,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        if same_pass_date_strips and locked_date_window is None:
            sw = last_result.get("same_seven_day_window")
            if isinstance(sw, dict) and sw.get("start") and sw.get("end"):
                try:
                    locked_date_window = (
                        date.fromisoformat(str(sw["start"])[:10]),
                        date.fromisoformat(str(sw["end"])[:10]),
                    )
                except ValueError:
                    pass
        last_result["void_fill_rounds"] = round_idx + 1
        last_result["stac_feature_pool_size"] = len(merged)

        uf = float(last_result.get("uncovered_fraction") or 0.0)
        if uf <= _VOID_FILL_MIN_UNCOVERED:
            last_result["void_fill_stopped"] = "coverage_met"
            break

    if last_result is None:
        last_result = plan_mosaic_from_features(
            aoi,
            [],
            sort_mode,
            same_pass_date_strips=same_pass_date_strips,
            locked_date_window=locked_date_window,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        last_result["void_fill_rounds"] = 0
        last_result["stac_feature_pool_size"] = 0

    if "void_fill_stopped" not in last_result:
        uf = float(last_result.get("uncovered_fraction") or 0.0)
        last_result["void_fill_stopped"] = (
            "max_rounds" if uf > _VOID_FILL_MIN_UNCOVERED else "coverage_met"
        )

    err_map: dict[str, str] = {}
    for e in all_errors:
        if not isinstance(e, dict):
            continue
        cid = str(e.get("catalog_id") or "")
        if cid:
            err_map[cid] = str(e.get("detail") or "")
    errors_out = [{"catalog_id": k, "detail": v} for k, v in err_map.items()]

    merged_list = list(merged.values()) if merged else []
    return last_result, errors_out, merged_list


def swap_options_for_selected(
    aoi: Polygon,
    features: list[dict[str, Any]],
    sort_mode: str,
    selected_items: list[dict[str, Any]],
    *,
    swap_options_limit: int = _SWAP_OPTIONS_PAGE_DEFAULT,
    swap_options_offset: dict[str, int] | None = None,
) -> dict[str, Any]:
    """
    Compute swap options for a pre-selected set of items (key + footprint), using the same
    STAC candidate pool filtering/sorting as the planner.
    """
    candidates: list[Candidate] = []
    for f in features:
        g = _footprint_geom(f)
        if g is None or g.is_empty:
            continue
        href = primary_raster_href(f)
        if not href:
            continue
        candidates.append(
            Candidate(
                feature=f,
                geom=g,
                href=href,
                key=_dedupe_key(f),
                cloud=_cloud_cover(f),
                dt=_item_datetime(f),
            )
        )

    id_to_dedupe_key: dict[str, str] = {}
    for c in candidates:
        fid = c.feature.get("id")
        if fid is not None:
            id_to_dedupe_key[str(fid)] = c.key

    intersecting: list[Candidate] = []
    for c in candidates:
        try:
            if c.geom.intersects(aoi):
                intersecting.append(c)
        except Exception:
            continue

    if sort_mode == "lowest_cloud":
        intersecting.sort(key=_sort_key_lowest_cloud)
    else:
        intersecting.sort(key=_sort_key_newest)

    selected_geoms: list[tuple[str, Polygon, dict[str, Any]]] = []
    selected_keys: set[str] = set()
    selected_payload: list[dict[str, Any]] = []
    footprints: list[dict[str, Any]] = []

    for idx, it in enumerate(selected_items or []):
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or it.get("stac_item_id") or it.get("id") or f"item-{idx}")
        fp = it.get("footprint")
        if not isinstance(fp, dict):
            continue
        try:
            g = shape(fp)
            if g.is_empty:
                continue
            if g.geom_type == "Polygon":
                poly = g  # type: ignore[assignment]
            elif g.geom_type == "MultiPolygon":
                poly = max(g.geoms, key=lambda p: p.area)  # type: ignore[union-attr]
            else:
                continue
        except Exception:
            continue

        selected_keys.add(key)
        fid_s = str(it.get("stac_item_id") or it.get("id") or "")
        if fid_s and fid_s in id_to_dedupe_key:
            selected_keys.add(id_to_dedupe_key[fid_s])
        selected_geoms.append((key, poly, it))
        selected_payload.append(
            {
                "key": key,
                "catalog_id": it.get("catalog_id"),
                "stac_collection_id": it.get("stac_collection_id"),
                "stac_item_id": it.get("stac_item_id") or it.get("id"),
                "href": it.get("href"),
                "thumbnail": it.get("thumbnail"),
                "cloud_cover": it.get("cloud_cover"),
                "footprint": mapping(poly),
                "mgrs_tile": _mgrs_tile_from_saved_item(it),
            }
        )
        footprints.append(
            {
                "type": "Feature",
                "geometry": mapping(poly),
                "properties": {
                    "key": key,
                    "id": it.get("stac_item_id") or it.get("id"),
                    "href": it.get("href"),
                    "cloud_cover": it.get("cloud_cover"),
                    "collection": it.get("stac_collection_id"),
                },
            }
        )

    swap_options: dict[str, list[dict[str, Any]]] = {}
    swap_options_total: dict[str, int] = {}
    for sel_key, sel_geom, it0 in selected_geoms:
        sel_tile = _mgrs_tile_from_saved_item(it0) if isinstance(it0, dict) else None
        alts: list[dict[str, Any]] = []
        for other in intersecting:
            if other.key in selected_keys:
                continue
            try:
                if sel_geom.intersection(other.geom).area <= 0:
                    continue
            except Exception:
                continue
            if sel_tile:
                ot = mgrs_tile_from_feature(other.feature)
                if ot != sel_tile:
                    continue
            alts.append(_swap_alt_dict(other))
        page, total = _slice_swap_alts(
            alts,
            sel_key,
            swap_options_limit=swap_options_limit,
            swap_options_offset=swap_options_offset,
        )
        swap_options[sel_key] = page
        swap_options_total[sel_key] = total

    return {
        "selected": selected_payload,
        "footprints": {"type": "FeatureCollection", "features": footprints},
        "swap_options": swap_options,
        "swap_options_total": swap_options_total,
        "uncovered_fraction": None,
        "remaining_uncovered": None,
        "candidates_matched": len(intersecting),
        "use_same_pass_date_strips": False,
        "same_seven_day_window": None,
    }
