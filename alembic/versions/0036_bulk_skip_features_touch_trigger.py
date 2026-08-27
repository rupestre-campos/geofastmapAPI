"""Bulk import: session flag to skip per-row features touch trigger (faster million-row loads).

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION geofast_sync_collection_features_last_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                cid text;
                mx timestamptz;
            BEGIN
                IF current_setting('geofast.bulk_skip_features_touch', true) = 'on' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'DELETE' THEN
                    cid := OLD.collection_id;
                    SELECT MAX(updated_at) INTO mx FROM features WHERE collection_id = cid;
                    UPDATE collections SET features_last_updated_at = mx WHERE id = cid;
                    RETURN OLD;
                END IF;
                cid := NEW.collection_id;
                UPDATE collections SET features_last_updated_at = GREATEST(
                    COALESCE(features_last_updated_at, '-infinity'::timestamptz),
                    NEW.updated_at
                ) WHERE id = cid;
                RETURN NEW;
            END;
            $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION geofast_sync_collection_features_last_updated_at()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                cid text;
                mx timestamptz;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    cid := OLD.collection_id;
                    SELECT MAX(updated_at) INTO mx FROM features WHERE collection_id = cid;
                    UPDATE collections SET features_last_updated_at = mx WHERE id = cid;
                    RETURN OLD;
                END IF;
                cid := NEW.collection_id;
                UPDATE collections SET features_last_updated_at = GREATEST(
                    COALESCE(features_last_updated_at, '-infinity'::timestamptz),
                    NEW.updated_at
                ) WHERE id = cid;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
