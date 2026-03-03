"""Convert uploaded images to thumbnail JPEG for map cards."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

# Max dimensions for thumbnail (gallery cards use ~280px width, 140px height)
THUMBNAIL_MAX_WIDTH = 400
THUMBNAIL_MAX_HEIGHT = 300
THUMBNAIL_JPEG_QUALITY = 85
THUMBNAIL_MAX_BYTES = 150_000  # ~150KB cap


def image_to_thumbnail(data: bytes, content_type: str | None = None) -> bytes:
    """Convert image bytes to thumbnail JPEG. Accepts JPEG, PNG, WebP, GIF."""
    img = Image.open(io.BytesIO(data))
    img = _ensure_rgb(img)
    img.thumbnail((THUMBNAIL_MAX_WIDTH, THUMBNAIL_MAX_HEIGHT), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=THUMBNAIL_JPEG_QUALITY, optimize=True)
    result = out.getvalue()
    if len(result) > THUMBNAIL_MAX_BYTES:
        # Reduce quality to meet size cap
        for q in range(THUMBNAIL_JPEG_QUALITY - 10, 20, -10):
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=q, optimize=True)
            result = out.getvalue()
            if len(result) <= THUMBNAIL_MAX_BYTES:
                break
    return result


def _ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB", "L"):
        return img
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")  # P, CMYK, etc.
