"""Tests for FeatureInsertBuffer."""
from unittest.mock import MagicMock

from app.utils.feature_subdivide import FeatureInsertBuffer


def test_feature_insert_buffer_flush_clears():
    buf = FeatureInsertBuffer(
        collection_id="c1",
        now="2026-01-01",
        max_vertices=256,
    )
    buf.add("fid-1", "POINT(0 0)", {"a": 1})
    buf.add("fid-2", None, None)
    session = MagicMock()
    n = buf.flush(session)
    assert n == 2
    assert len(buf) == 0
    assert session.execute.call_count == 2
