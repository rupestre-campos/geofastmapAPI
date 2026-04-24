"""Shared RGBA preview handling for STAC thumbnails (border transparency + optional crop)."""

from __future__ import annotations

import io

from PIL import Image, ImageFile


def apply_border_transparency_rgba(im: Image.Image) -> None:
    """In-place: near-black and near-white pixels become transparent (matches STAC thumbnail proxy)."""
    thr_dark = 18
    thr_white_min = 235
    data = list(im.getdata())
    out_data = []
    for (r0, g0, b0, a0) in data:
        if a0 and r0 <= thr_dark and g0 <= thr_dark and b0 <= thr_dark:
            out_data.append((r0, g0, b0, 0))
        elif a0 and min(r0, g0, b0) >= thr_white_min:
            out_data.append((r0, g0, b0, 0))
        else:
            out_data.append((r0, g0, b0, a0))
    im.putdata(out_data)


def crop_image_to_nontransparent(im: Image.Image, *, pad: int = 2) -> Image.Image:
    """Crop to bounding box of pixels with alpha > 10; return original if empty or degenerate."""
    w, h = im.size
    if w <= 0 or h <= 0:
        return im
    bbox = im.getbbox()
    if bbox is None:
        return im
    x0, y0, x1, y1 = bbox
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    x1 = min(w, x1 + pad)
    y1 = min(h, y1 + pad)
    if x1 <= x0 or y1 <= y0:
        return im
    return im.crop((x0, y0, x1, y1))


def decode_image_rgba(raw: bytes) -> Image.Image:
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    im = Image.open(io.BytesIO(raw))
    return im.convert("RGBA")
