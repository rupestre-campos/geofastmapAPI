"""Nickname validation and map gallery access labels."""

import pytest

from app.services.map_gallery_meta import map_access_label, map_access_badge_class
from app.utils.nickname import validate_nickname


class TestValidateNickname:
    def test_empty_clears(self):
        assert validate_nickname(None) is None
        assert validate_nickname("") is None
        assert validate_nickname("   ") is None

    def test_alphanumeric_ok(self):
        assert validate_nickname("User42") == "User42"

    def test_rejects_special_chars(self):
        with pytest.raises(ValueError, match="letters and numbers"):
            validate_nickname("bad-name")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="128"):
            validate_nickname("a" * 129)


class TestMapAccessLabel:
    def test_public(self):
        assert map_access_label(
            visibility="public", share_count=0, is_owner=False, shared_with_me=False
        ) == "Public"

    def test_private(self):
        assert map_access_label(
            visibility="private", share_count=0, is_owner=True, shared_with_me=False
        ) == "Private"

    def test_shared_count(self):
        assert map_access_label(
            visibility="private", share_count=2, is_owner=True, shared_with_me=False
        ) == "Shared (2)"

    def test_shared_with_me(self):
        assert map_access_label(
            visibility="private", share_count=1, is_owner=False, shared_with_me=True
        ) == "Shared with you"

    def test_badge_classes(self):
        assert map_access_badge_class("Public") == "map-badge-public"
        assert map_access_badge_class("Shared (3)") == "map-badge-shared"
