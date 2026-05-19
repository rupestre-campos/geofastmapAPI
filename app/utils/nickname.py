"""Nickname validation (display name; unique, alphanumeric)."""

from __future__ import annotations

import re

NICKNAME_MAX_LEN = 128
_NICKNAME_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")


def normalize_nickname(value: str | None) -> str | None:
    """Return stripped nickname or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def validate_nickname(value: str | None) -> str | None:
    """
    Validate nickname for set/update. Empty/None clears nickname.
    Raises ValueError with a short message on invalid input.
    """
    nick = normalize_nickname(value)
    if nick is None:
        return None
    if len(nick) > NICKNAME_MAX_LEN:
        raise ValueError(f"Nickname must be at most {NICKNAME_MAX_LEN} characters.")
    if not _NICKNAME_RE.fullmatch(nick):
        raise ValueError("Nickname may only contain letters and numbers (A–Z, a–z, 0–9).")
    return nick
