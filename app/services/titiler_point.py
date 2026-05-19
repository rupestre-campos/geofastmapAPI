"""Titiler point sampling and UI-friendly enrichment for raster identify."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import HTTPException, Request

from app.services.raster_style_display import _infer_continuous_mode
from app.services.raster_style_spec import is_classification_style
from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text
from app.services.titiler_retry import titiler_execute_with_retry


# Query keys that must not be sent to Titiler /point (tile viz / cache / DEM encode).
TITILER_POINT_DROP_KEYS = frozenset(
    {
        "v",
        "lon",
        "lat",
        "algorithm",
        "colormap",
        "colormap_type",
        "colormap_name",
        "rescale",
        "color_formula",
        "mv",
        "dem_encoding",
        "demv",
    }
)


def style_spec_from_request_query(request: Request) -> dict[str, Any]:
    """Build a minimal style_spec from Titiler render query params (STAC / ad-hoc clients)."""
    q = request.query_params
    spec: dict[str, Any] = {}
    if q.get("expression"):
        spec["expression"] = q.get("expression")
    if q.get("colormap_name"):
        spec["colormap_name"] = q.get("colormap_name")
    if q.get("asset"):
        spec["asset"] = q.get("asset")
    assets = [v for k, v in q.multi_items() if k == "assets" and v]
    if assets:
        spec["assets"] = assets
    bidx = [int(v) for k, v in q.multi_items() if k == "bidx" and v and str(v).isdigit()]
    if not bidx:
        bidx = []
        for k, v in q.multi_items():
            if k == "bidx" and v:
                try:
                    bidx.append(int(v))
                except ValueError:
                    pass
    if bidx:
        spec["bidx"] = bidx
    if q.get("colormap") and q.get("colormap_type") == "explicit":
        try:
            spec["colormap"] = json.loads(q.get("colormap") or "{}")
        except json.JSONDecodeError:
            pass
        spec["style_type"] = "classification"
    if q.get("nodata") is not None:
        spec["nodata"] = q.get("nodata")
    return spec


def _format_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if v != v:
            return "—"
        if v == int(v):
            return str(int(v))
        return f"{v:.4g}"
    return str(v)


def _band_label_from_index(idx: Any) -> str:
    if idx is None:
        return "?"
    s = str(idx).strip()
    if s.lower().startswith("b"):
        return s.lower()
    return f"b{s}"


def _lookup_classification(style_spec: dict, raw_value: Any) -> dict[str, str] | None:
    if raw_value is None:
        return None
    key = str(int(raw_value)) if isinstance(raw_value, float) and raw_value == int(raw_value) else str(raw_value)
    classes = style_spec.get("classes")
    if isinstance(classes, list):
        for c in classes:
            if not isinstance(c, dict):
                continue
            cv = c.get("value")
            cv_key = (
                str(int(cv))
                if isinstance(cv, float) and cv == int(cv)
                else str(cv)
                if cv is not None
                else ""
            )
            if cv_key == key:
                color = c.get("color") or ""
                if isinstance(color, str) and color and not color.startswith("#"):
                    color = f"#{color}"
                return {
                    "name": str(c.get("name") or cv),
                    "color": color,
                    "value": key,
                }
    colormap = style_spec.get("colormap")
    if isinstance(colormap, dict) and key in colormap:
        color = str(colormap[key])
        if color and not color.startswith("#"):
            color = f"#{color}"
        return {"name": key, "color": color, "value": key}
    return None


def _values_list(titiler_body: dict) -> list[Any]:
    vals = titiler_body.get("values")
    if isinstance(vals, list):
        return vals
    if isinstance(vals, (int, float)):
        return [vals]
    return []


def _has_sample_values(titiler_body: dict) -> bool:
    """True when Titiler returned at least one non-null sampled value."""
    return any(v is not None for v in _values_list(titiler_body))


def _band_names_list(titiler_body: dict) -> list[str]:
    names = titiler_body.get("band_names")
    if isinstance(names, list):
        return [str(n) for n in names]
    return []


def enrich_point_response(titiler_body: dict, style_spec: dict | None) -> dict[str, Any]:
    """Add display rows for map popups from Titiler point JSON + optional style_spec."""
    spec = style_spec if isinstance(style_spec, dict) else {}
    values = _values_list(titiler_body)
    band_names = _band_names_list(titiler_body)
    coords = titiler_body.get("coordinates")
    nodata = spec.get("nodata")

    rows: list[dict[str, Any]] = []
    render_mode = "unknown"

    if is_classification_style(spec):
        render_mode = "classification"
        raw = values[0] if values else None
        cls = _lookup_classification(spec, raw)
        is_nodata = _is_nodata(raw, nodata)
        display_val = _format_value(raw)
        if cls:
            rows.append(
                {
                    "role": "class",
                    "label": cls["name"],
                    "value": raw,
                    "color": cls.get("color"),
                    "display": f"{cls['name']} ({display_val})",
                    "is_nodata": is_nodata,
                }
            )
        else:
            rows.append(
                {
                    "role": "value",
                    "label": "Class",
                    "value": raw,
                    "display": display_val,
                    "is_nodata": is_nodata,
                }
            )
    else:
        mode = _infer_continuous_mode(spec)
        render_mode = mode
        if mode == "rgb_assets":
            assets = spec.get("assets")
            asset_list = list(assets) if isinstance(assets, list) else []
            roles = ("R", "G", "B")
            for i, val in enumerate(values[:3]):
                role = roles[i] if i < 3 else str(i + 1)
                label = str(asset_list[i]) if i < len(asset_list) else (band_names[i] if i < len(band_names) else "?")
                rows.append(
                    {
                        "role": role,
                        "label": label,
                        "value": val,
                        "display": f"{role} {label} {_format_value(val)}",
                        "is_nodata": _is_nodata(val, nodata),
                    }
                )
        elif mode == "rgb_bands":
            bidx = spec.get("bidx")
            bidx_list = list(bidx) if isinstance(bidx, list) else []
            roles = ("R", "G", "B")
            for i, val in enumerate(values[:3]):
                role = roles[i] if i < 3 else str(i + 1)
                if i < len(bidx_list):
                    label = _band_label_from_index(bidx_list[i])
                elif i < len(band_names):
                    label = band_names[i]
                else:
                    label = f"b{i + 1}"
                rows.append(
                    {
                        "role": role,
                        "label": label,
                        "value": val,
                        "display": f"{role} {label} {_format_value(val)}",
                        "is_nodata": _is_nodata(val, nodata),
                    }
                )
        elif mode == "expression":
            expr = spec.get("expression") or "Expression"
            expr_label = str(expr)[:48] + ("…" if len(str(expr)) > 48 else "")
            val = values[0] if values else None
            rows.append(
                {
                    "role": "expr",
                    "label": expr_label,
                    "value": val,
                    "display": f"{expr_label} {_format_value(val)}",
                    "is_nodata": _is_nodata(val, nodata),
                }
            )
        else:
            # single band or fallback from titiler band count
            colormap = spec.get("colormap_name")
            bidx = spec.get("bidx")
            if isinstance(bidx, list) and bidx:
                label = _band_label_from_index(bidx[0])
            elif band_names:
                label = band_names[0]
            elif colormap:
                label = str(colormap)
            else:
                label = "Band"
            for i, val in enumerate(values):
                bl = label if len(values) <= 1 else (band_names[i] if i < len(band_names) else f"b{i + 1}")
                title = str(colormap) if colormap and len(values) == 1 else bl
                rows.append(
                    {
                        "role": "band",
                        "label": bl,
                        "value": val,
                        "display": f"{title} {_format_value(val)}",
                        "is_nodata": _is_nodata(val, nodata),
                    }
                )
            if not rows and values:
                for i, val in enumerate(values):
                    bl = band_names[i] if i < len(band_names) else f"b{i + 1}"
                    rows.append(
                        {
                            "role": "band",
                            "label": bl,
                            "value": val,
                            "display": f"{bl} {_format_value(val)}",
                            "is_nodata": _is_nodata(val, nodata),
                        }
                    )

        # DEM hint when no style but single elevation-like value
        if not rows and values:
            val = values[0]
            rows.append(
                {
                    "role": "elevation",
                    "label": "Elevation",
                    "value": val,
                    "display": f"Elevation {_format_value(val)}",
                    "is_nodata": _is_nodata(val, nodata),
                }
            )

    if not rows and not values:
        rows.append(
            {
                "role": "info",
                "label": "",
                "value": None,
                "display": "No raster coverage at this location",
                "is_nodata": True,
            }
        )

    return {
        "coordinates": coords,
        "values": values,
        "band_names": band_names,
        "rows": rows,
        "render_mode": render_mode,
    }


def _is_nodata(value: Any, nodata: Any) -> bool:
    if value is None or nodata is None:
        return value is None
    try:
        if isinstance(nodata, str):
            nodata = json.loads(nodata) if nodata.strip().startswith("[") else nodata
    except Exception:
        pass
    try:
        return float(value) == float(nodata)
    except (TypeError, ValueError):
        return str(value) == str(nodata)


def _titiler_params_only_url(params: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in params if k == "url"]


def _titiler_read_params(params: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Params for raw COG value reads (bidx, nodata), excluding url, viz, and mosaic-only keys."""
    skip = frozenset(
        {
            "url",
            "colormap",
            "colormap_type",
            "colormap_name",
            "rescale",
            "color_formula",
            "algorithm",
            "mv",
            "pixel_selection",
        }
    )
    return [(k, v) for k, v in params if k not in skip]


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


async def _try_cog_point_reads(
    client: httpx.AsyncClient,
    titiler_base: str,
    coord: str,
    cog_urls: list[str],
    read_params: list[tuple[str, str]],
    *,
    shared_secret: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    for cog_url in _dedupe_urls(cog_urls):
        cog_params = [("url", cog_url)] + read_params
        try:
            body = await fetch_titiler_point_json(
                client,
                titiler_base,
                f"/cog/point/{coord}",
                cog_params,
                shared_secret=shared_secret,
                timeout=timeout,
            )
        except HTTPException:
            continue
        if _has_sample_values(body):
            return body
    return None


async def fetch_titiler_point_json(
    client: httpx.AsyncClient,
    titiler_base: str,
    forward_path: str,
    params: list[tuple[str, str]],
    *,
    shared_secret: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """GET Titiler point endpoint; return parsed JSON or raise HTTPException."""
    base = titiler_base.rstrip("/")
    url = f"{base}{forward_path}"
    resp, _attempts = await titiler_execute_with_retry(
        lambda: client.get(
            url, params=params, headers={"Accept": "application/json"}, timeout=timeout
        ),
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=sanitize_titiler_upstream_error_text(
                resp.text,
                shared_secret=shared_secret,
                max_len=1000,
            ),
        )
    try:
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail="Invalid JSON from Titiler point") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected Titiler point response")
    return data


async def fetch_mosaic_point_with_fallback(
    client: httpx.AsyncClient,
    titiler_base: str,
    forward_path: str,
    params: list[tuple[str, str]],
    *,
    shared_secret: str | None = None,
    timeout: float = 30.0,
    extra_cog_urls: list[str] | None = None,
) -> dict:
    """
    Mosaic point read; if empty, try DB-resolved COG URLs, then Titiler /point/.../assets + /cog/point.
    """
    raw = await fetch_titiler_point_json(
        client, titiler_base, forward_path, params, shared_secret=shared_secret, timeout=timeout
    )
    if _has_sample_values(raw):
        return raw
    if not forward_path.startswith("/mosaicjson/point/"):
        return raw

    coord = forward_path.removeprefix("/mosaicjson/point/").split("/assets")[0]
    if not coord:
        return raw

    read_params = _titiler_read_params(params)
    url_only = _titiler_params_only_url(params)

    if extra_cog_urls:
        hit = await _try_cog_point_reads(
            client,
            titiler_base,
            coord,
            list(extra_cog_urls),
            read_params,
            shared_secret=shared_secret,
            timeout=timeout,
        )
        if hit is not None:
            return hit

    assets_path = f"/mosaicjson/point/{coord}/assets"
    cog_urls: list[str] = []
    try:
        resp = await client.get(
            f"{titiler_base.rstrip('/')}{assets_path}",
            params=url_only,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code < 400:
            assets_body = resp.json()
            if isinstance(assets_body, list):
                for item in assets_body:
                    if isinstance(item, str) and item.strip():
                        cog_urls.append(item.strip())
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("href")
                        if u:
                            cog_urls.append(str(u))
            elif isinstance(assets_body, dict):
                for item in assets_body.get("assets") or assets_body.get("urls") or []:
                    if isinstance(item, str) and item.strip():
                        cog_urls.append(item.strip())
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("href")
                        if u:
                            cog_urls.append(str(u))
    except Exception:
        pass

    hit = await _try_cog_point_reads(
        client,
        titiler_base,
        coord,
        cog_urls,
        read_params,
        shared_secret=shared_secret,
        timeout=timeout,
    )
    if hit is not None:
        return hit
    return raw
