"""Validation helpers for bulk import mode and replace_filters."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.utils.property_filters import PropertyFilter, parse_filter_param

BULK_IMPORT_MODES = frozenset({"append", "replace", "replace_filtered"})
BULK_COPY_MODES = frozenset({"append", "replace"})


def _normalize_replace_filters_raw(raw: list[str] | str | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [line.strip() for line in raw.splitlines() if line.strip()]
    out: list[str] = []
    for item in raw:
        if not item:
            continue
        if isinstance(item, str) and "\n" in item:
            out.extend(line.strip() for line in item.splitlines() if line.strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def validate_bulk_import_mode_and_filters(
    mode: str,
    replace_filters_raw: list[str] | str | None = None,
) -> tuple[str, list[str]]:
    """
    Validate bulk import mode and optional replace_filters.
    Returns (mode, serialized filter lines) for storage on session/payload.
    """
    mode = str(mode or "append").strip()
    if mode not in BULK_IMPORT_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'append', 'replace', or 'replace_filtered'",
        )
    if bool(getattr(get_settings(), "bulk_copy_ingest_enabled", True)) and mode not in BULK_COPY_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'append' or 'replace' for bulk file upload (replace_filtered is not supported)",
        )
    lines = _normalize_replace_filters_raw(replace_filters_raw)
    if mode == "replace_filtered":
        parsed = parse_filter_param(lines)
        if not parsed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="replace_filtered requires at least one valid filter (key:op:value per line)",
            )
        lines = [f"{pf.key}:{pf.op.value}:{pf.value}" for pf in parsed]
    elif lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="replace_filters is only allowed when mode is replace_filtered",
        )
    return mode, lines


def parsed_replace_filters(lines: list[str]) -> list[PropertyFilter]:
    return parse_filter_param(lines)


def parse_queue_compute_tiles(value: object | None, *, default: bool = False) -> bool:
    """Parse bulk import flag; default false — tile builds use POST /tiles/build (manual/cron)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() not in ("false", "0", "no", "")
