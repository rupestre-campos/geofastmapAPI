"""Strip secrets from upstream Titiler/GDAL error strings before returning them to clients."""

from __future__ import annotations

import re


def sanitize_titiler_upstream_error_text(
    text: str | None,
    *,
    shared_secret: str | None = None,
    max_len: int = 2000,
) -> str:
    """
    Redact ``token=``, ``secret=``, and literal copies of ``titiler_internal_secret`` so that
    forwarded HTTP/GDAL errors cannot leak credentials to browsers or logs intended for users.
    """
    if not text or not str(text).strip():
        return "Titiler error"
    s = str(text)[:max_len]
    s = re.sub(r"([?&]token=)[^&\s\"'<>]+", r"\1<redacted>", s, flags=re.IGNORECASE)
    s = re.sub(r"([?&]secret=)[^&\s\"'<>]+", r"\1<redacted>", s, flags=re.IGNORECASE)
    sec = (shared_secret or "").strip()
    if len(sec) >= 8 and sec in s:
        s = s.replace(sec, "<redacted>")
    return s
