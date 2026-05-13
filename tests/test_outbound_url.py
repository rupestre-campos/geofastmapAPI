import pytest

from app.utils.outbound_url import UnsafeOutboundUrlError, validate_public_http_url


def test_validate_public_http_url_accepts_public_https():
    validate_public_http_url("https://example.com/stac")


def test_validate_public_http_url_rejects_private_ip():
    with pytest.raises(UnsafeOutboundUrlError):
        validate_public_http_url("http://192.168.1.1/")


def test_validate_public_http_url_rejects_localhost():
    with pytest.raises(UnsafeOutboundUrlError):
        validate_public_http_url("http://localhost:19999")


def test_validate_public_http_url_require_https():
    validate_public_http_url("https://x.test", require_https=True)
    with pytest.raises(UnsafeOutboundUrlError):
        validate_public_http_url("http://x.test", require_https=True)
