"""Tests for sanitizing forwarded Titiler/GDAL errors."""

from app.services.titiler_error_sanitize import sanitize_titiler_upstream_error_text


def test_sanitize_strips_token_query_param():
    raw = (
        '{"detail":"failed /vsicurl/http://host/internal/x/cog?token=secret123abc '
        'not recognized"}'
    )
    out = sanitize_titiler_upstream_error_text(raw, shared_secret=None)
    assert "secret123abc" not in out
    assert "token=<redacted>" in out or "<redacted>" in out


def test_sanitize_strips_literal_secret():
    sec = "c7955740fc92c65131593537c183765cd7853767c120788bb69aa3496c1d35fa"
    raw = f'{{"detail":"url ... token={sec}"}}'
    out = sanitize_titiler_upstream_error_text(raw, shared_secret=sec)
    assert sec not in out


def test_sanitize_empty_returns_generic():
    assert sanitize_titiler_upstream_error_text("") == "Titiler error"
    assert sanitize_titiler_upstream_error_text(None) == "Titiler error"
