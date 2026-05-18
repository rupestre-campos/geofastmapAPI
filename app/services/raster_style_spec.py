"""Validate and normalize raster style_spec (continuous + classification)."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException, status

MAX_CLASSIFICATION_ENTRIES = 256
_HEX_COLOR_RE = re.compile(r"^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")

_CONTINUOUS_KEYS = (
    "asset",
    "assets",
    "bidx",
    "rescale",
    "colormap_name",
    "expression",
    "color_formula",
)


class RasterStyleSpecError(ValueError):
    pass


def _normalize_pixel_value_key(raw: Any) -> str:
    if isinstance(raw, bool):
        raise RasterStyleSpecError("Boolean pixel values are not allowed")
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise RasterStyleSpecError("Invalid pixel value")
        if raw == int(raw):
            return str(int(raw))
        return str(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise RasterStyleSpecError("Empty pixel value key")
        return s
    raise RasterStyleSpecError(f"Invalid pixel value type: {type(raw).__name__}")


def parse_nodata_value(raw: Any) -> int | float | str | None:
    """Parse optional Titiler nodata override (stored in style_spec.nodata)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise RasterStyleSpecError("nodata cannot be a boolean")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise RasterStyleSpecError("Invalid nodata value")
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return s
    raise RasterStyleSpecError(f"Invalid nodata type: {type(raw).__name__}")


def normalize_hex_color(raw: Any) -> str:
    if not isinstance(raw, str):
        raise RasterStyleSpecError("Color must be a hex string")
    s = raw.strip()
    if not _HEX_COLOR_RE.match(s):
        raise RasterStyleSpecError(f"Invalid hex color: {raw!r}")
    if len(s) == 4:
        return "#" + "".join(c * 2 for c in s[1:].upper())
    return "#" + s[1:].upper()


def _parse_json_input(raw: Any, field_name: str) -> Any:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            raise RasterStyleSpecError(f"{field_name}: invalid JSON ({e})") from e
    return raw


def _parse_colormap_dict(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RasterStyleSpecError("Colormap must be a JSON object (value → hex color)")
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = _normalize_pixel_value_key(k)
        if key in out:
            raise RasterStyleSpecError(f"Duplicate pixel value in colormap: {key}")
        out[key] = normalize_hex_color(v)
    return out


def _parse_classes_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    classes: list[dict[str, Any]] = []

    if isinstance(raw, list):
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise RasterStyleSpecError(f"classes[{i}] must be an object")
            if "value" not in item:
                raise RasterStyleSpecError(f"classes[{i}] missing value")
            value_key = _normalize_pixel_value_key(item["value"])
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RasterStyleSpecError(f"classes[{i}] missing or empty name")
            color_raw = item.get("color")
            if color_raw is None:
                raise RasterStyleSpecError(f"classes[{i}] missing color")
            classes.append(
                {
                    "value": value_key,
                    "name": name.strip(),
                    "color": normalize_hex_color(color_raw),
                }
            )
        return classes

    if isinstance(raw, dict):
        for k, item in raw.items():
            value_key = _normalize_pixel_value_key(k)
            if not isinstance(item, dict):
                raise RasterStyleSpecError(f"classes[{value_key}] must be an object")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RasterStyleSpecError(f"classes[{value_key}] missing or empty name")
            color_raw = item.get("color")
            if color_raw is None:
                raise RasterStyleSpecError(f"classes[{value_key}] missing color")
            classes.append(
                {
                    "value": value_key,
                    "name": name.strip(),
                    "color": normalize_hex_color(color_raw),
                }
            )
        return classes

    raise RasterStyleSpecError("classes must be a JSON array or object")


def parse_classification_inputs(
    colormap_raw: Any = None,
    classes_raw: Any = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Parse colormap + classes JSON; merge colors; enforce limits."""
    colormap = _parse_colormap_dict(_parse_json_input(colormap_raw, "colormap"))
    classes = _parse_classes_list(_parse_json_input(classes_raw, "classes"))

    if not colormap and not classes:
        raise RasterStyleSpecError("Classification style requires colormap and/or classes JSON")

    for entry in classes:
        vk = entry["value"]
        if vk not in colormap:
            colormap[vk] = entry["color"]
        elif colormap[vk].upper() != entry["color"].upper():
            raise RasterStyleSpecError(
                f"Color mismatch for value {vk}: colormap has {colormap[vk]}, class has {entry['color']}"
            )
        entry["color"] = colormap[vk]

    if len(colormap) > MAX_CLASSIFICATION_ENTRIES:
        raise RasterStyleSpecError(
            f"Too many classification entries (max {MAX_CLASSIFICATION_ENTRIES})"
        )

    # Sort classes by numeric value when possible for stable legend order
    def _sort_key(c: dict) -> tuple[int, float | None, str]:
        v = c["value"]
        try:
            if "." in v:
                return (0, float(v), v)
            return (0, float(int(v)), v)
        except ValueError:
            return (1, None, v)

    if classes:
        classes_sorted = sorted(classes, key=_sort_key)
    else:
        classes_sorted = [
            {"value": k, "name": k, "color": colormap[k]}
            for k in sorted(colormap.keys(), key=lambda k: _sort_key({"value": k}))
        ]

    return colormap, classes_sorted


def build_classification_style_spec(
    base: dict | None,
    *,
    colormap_raw: Any = None,
    classes_raw: Any = None,
) -> dict:
    """Build a normalized classification style_spec from editor/base fields + user JSON."""
    base = dict(base or {})
    colormap, classes = parse_classification_inputs(colormap_raw, classes_raw)

    bidx = base.get("bidx")
    if bidx is None:
        bidx_list = ["1"]
    elif isinstance(bidx, list):
        bidx_list = [str(x) for x in bidx if x is not None and str(x).strip()]
        if not bidx_list:
            bidx_list = ["1"]
    else:
        bidx_list = [str(bidx).strip()] if str(bidx).strip() else ["1"]

    out: dict[str, Any] = {
        "style_type": "classification",
        "colormap": colormap,
        "colormap_type": "explicit",
        "classes": classes,
        "bidx": bidx_list,
    }
    asset = base.get("asset")
    if asset is not None and str(asset).strip():
        out["asset"] = str(asset).strip()
    nodata = parse_nodata_value(base.get("nodata"))
    if nodata is not None:
        out["nodata"] = nodata
    return out


def normalize_raster_style_spec(spec: dict | None) -> dict:
    """Validate and normalize style_spec before persistence."""
    if not spec or not isinstance(spec, dict):
        return spec or {}

    style_type = (spec.get("style_type") or "continuous").strip().lower()
    if style_type not in ("continuous", "classification"):
        raise RasterStyleSpecError(f"Unknown style_type: {style_type!r}")

    if style_type != "classification":
        out = dict(spec)
        nodata = parse_nodata_value(spec.get("nodata"))
        if nodata is None:
            out.pop("nodata", None)
        else:
            out["nodata"] = nodata
        return out

    # Full classification document from API client
    if spec.get("colormap") is not None or spec.get("classes") is not None:
        colormap, classes = parse_classification_inputs(spec.get("colormap"), spec.get("classes"))
        out = build_classification_style_spec(
            {k: spec.get(k) for k in ("asset", "bidx", "nodata")},
            colormap_raw=colormap,
            classes_raw=classes,
        )
        return out

    raise RasterStyleSpecError(
        "Classification style_spec requires colormap and/or classes"
    )


def normalize_raster_style_spec_http(spec: dict | None) -> dict:
    """Like normalize_raster_style_spec but raises HTTPException for API routes."""
    try:
        return normalize_raster_style_spec(spec)
    except RasterStyleSpecError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


def titiler_nodata_param(style_spec: dict) -> tuple[str, str] | None:
    """Titiler ``nodata`` query param from style_spec, if set."""
    if not style_spec or not isinstance(style_spec, dict):
        return None
    nd = parse_nodata_value(style_spec.get("nodata"))
    if nd is None:
        return None
    return ("nodata", str(nd))


def titiler_params_from_classification_style(style_spec: dict) -> list[tuple[str, str]]:
    """Build Titiler query param pairs for a classification style_spec."""
    import json as json_mod

    colormap = style_spec.get("colormap")
    if not isinstance(colormap, dict) or not colormap:
        return []

    params: list[tuple[str, str]] = [
        ("colormap", json_mod.dumps(colormap)),
        ("colormap_type", str(style_spec.get("colormap_type") or "explicit")),
    ]
    bidx = style_spec.get("bidx")
    if isinstance(bidx, list) and bidx:
        params.append(("bidx", ",".join(str(x) for x in bidx)))
    elif bidx is not None:
        params.append(("bidx", str(bidx)))
    else:
        params.append(("bidx", "1"))
    nd = titiler_nodata_param(style_spec)
    if nd:
        params.append(nd)
    return params


def is_classification_style(style_spec: dict | None) -> bool:
    if not style_spec or not isinstance(style_spec, dict):
        return False
    return (style_spec.get("style_type") or "").strip().lower() == "classification"
