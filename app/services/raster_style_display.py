"""Read-only summaries of raster style_spec for HTML collection view."""

from __future__ import annotations

from typing import Any


def raster_style_viz_context(style_spec: dict | None) -> dict[str, Any] | None:
    """Build template-friendly context from a raster style_spec."""
    if not style_spec or not isinstance(style_spec, dict):
        return None

    style_type = (style_spec.get("style_type") or "continuous").strip().lower()
    nodata = style_spec.get("nodata")
    nodata_display = None if nodata is None else str(nodata)

    if style_type == "classification":
        classes = style_spec.get("classes")
        rows: list[dict[str, str]] = []
        if isinstance(classes, list):
            for c in classes:
                if not isinstance(c, dict):
                    continue
                val = c.get("value")
                name = c.get("name") or val
                color = c.get("color") or ""
                if isinstance(color, str) and color and not color.startswith("#"):
                    color = f"#{color}"
                rows.append(
                    {
                        "value": str(val) if val is not None else "",
                        "name": str(name) if name is not None else "",
                        "color": color,
                    }
                )
        if not rows and isinstance(style_spec.get("colormap"), dict):
            for k, v in style_spec["colormap"].items():
                color = str(v)
                if color and not color.startswith("#"):
                    color = f"#{color}"
                rows.append({"value": str(k), "name": str(k), "color": color})
        return {
            "kind": "classification",
            "nodata": nodata_display,
            "classes": rows,
            "bidx": _format_bidx(style_spec.get("bidx")),
            "asset": style_spec.get("asset"),
        }

    mode = _infer_continuous_mode(style_spec)
    params: list[dict[str, str]] = []
    asset = style_spec.get("asset")
    if asset:
        params.append({"label": "Asset / item", "value": str(asset)})
    if mode == "expression":
        if style_spec.get("expression"):
            params.append({"label": "Expression", "value": str(style_spec["expression"])})
    elif mode == "rgb_assets":
        assets = style_spec.get("assets")
        if isinstance(assets, list) and assets:
            params.append({"label": "RGB assets", "value": ", ".join(str(a) for a in assets)})
    elif mode in ("rgb_bands", "single"):
        bidx = style_spec.get("bidx")
        if isinstance(bidx, list) and bidx:
            if len(bidx) >= 3:
                params.append({"label": "Red band", "value": f"b{bidx[0]}"})
                params.append({"label": "Green band", "value": f"b{bidx[1]}"})
                params.append({"label": "Blue band", "value": f"b{bidx[2]}"})
            else:
                params.append({"label": "Band", "value": f"b{bidx[0]}"})
    rescale = style_spec.get("rescale")
    if rescale:
        if isinstance(rescale, list):
            params.append({"label": "Rescale", "value": ", ".join(str(x) for x in rescale)})
        else:
            params.append({"label": "Rescale", "value": str(rescale)})
    if style_spec.get("colormap_name"):
        params.append({"label": "Colormap", "value": str(style_spec["colormap_name"])})
    if style_spec.get("color_formula"):
        params.append({"label": "Color formula", "value": str(style_spec["color_formula"])})
    if nodata_display is not None:
        params.append({"label": "No-data value", "value": nodata_display})

    return {
        "kind": "continuous",
        "mode_label": _mode_label(mode),
        "mode": mode,
        "params": params,
        "nodata": nodata_display,
    }


def _format_bidx(bidx: Any) -> str | None:
    if bidx is None:
        return None
    if isinstance(bidx, list):
        return ", ".join(f"b{x}" for x in bidx)
    return str(bidx)


def _infer_continuous_mode(spec: dict) -> str:
    if spec.get("expression"):
        return "expression"
    assets = spec.get("assets")
    if isinstance(assets, list) and len(assets) >= 3:
        return "rgb_assets"
    bidx = spec.get("bidx")
    if isinstance(bidx, list):
        if len(bidx) >= 3:
            return "rgb_bands"
        if len(bidx) == 1:
            return "single"
    return "rgb_bands"


def _mode_label(mode: str) -> str:
    return {
        "expression": "Expression",
        "rgb_assets": "RGB (3 assets)",
        "rgb_bands": "RGB (bands)",
        "single": "Single band",
        "continuous": "Continuous",
    }.get(mode, mode)
