"""Bulk import: skip per-row features trigger via session GUC (see migration 0036)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings


@contextmanager
def bulk_import_features_trigger_disabled(
    engine: Engine,
    session: Session,
) -> Iterator[None]:
    """Set geofast.bulk_skip_features_touch=on for this DB session during bulk import."""
    if not getattr(get_settings(), "bulk_skip_features_touch_trigger", True):
        yield
        return
    try:
        session.execute(text("SET geofast.bulk_skip_features_touch = 'on'"))
        yield
    finally:
        try:
            session.execute(text("RESET geofast.bulk_skip_features_touch"))
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass


def refresh_collection_features_last_updated_sync(
    engine: Engine,
    collection_id: str,
) -> None:
    """Set collections.features_last_updated_at from MAX(features.updated_at) after bulk import."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE collections c
                SET features_last_updated_at = (
                    SELECT MAX(f.updated_at) FROM features f WHERE f.collection_id = :cid
                )
                WHERE c.id = :cid
                """
            ),
            {"cid": collection_id},
        )
