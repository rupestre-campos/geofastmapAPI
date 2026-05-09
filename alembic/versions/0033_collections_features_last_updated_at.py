"""Denormalize max(features.updated_at) onto collections for fast reads.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-08

- Adds collections.features_last_updated_at (nullable), backfilled from features.
- Row-level triggers on partitioned parent table `features` keep it in sync on
  INSERT/UPDATE/DELETE (PG does not allow statement-level triggers on partitioned tables).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("features_last_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        text("""
            UPDATE collections c
            SET features_last_updated_at = (
                SELECT MAX(f.updated_at)
                FROM features f
                WHERE f.collection_id = c.id
            )
        """)
    )
    op.execute(
        text("""
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
        """)
    )
    op.execute(
        text("""
            CREATE TRIGGER geofast_features_touch_collection_features_last_updated_at
            AFTER INSERT OR UPDATE OR DELETE ON features
            FOR EACH ROW
            EXECUTE FUNCTION geofast_sync_collection_features_last_updated_at();
        """)
    )


def downgrade() -> None:
    op.execute(
        text("DROP TRIGGER IF EXISTS geofast_features_touch_collection_features_last_updated_at ON features")
    )
    op.execute(text("DROP FUNCTION IF EXISTS geofast_sync_collection_features_last_updated_at()"))
    op.drop_column("collections", "features_last_updated_at")
