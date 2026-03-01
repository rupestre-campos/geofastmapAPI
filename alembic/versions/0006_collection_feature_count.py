"""Add feature_count to collections and backfill from features.

Revision ID: 0006
Revises: 0005
Create Date: 2026-02-13

- Add collections.feature_count (cached total) for fast item list when no filters.
- Backfill from current feature counts per collection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("feature_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Backfill: set feature_count from actual counts (works with partitioned features table)
    conn = op.get_bind()
    conn.execute(
        text("""
            UPDATE collections c
            SET feature_count = COALESCE(
                (SELECT count(*) FROM features f WHERE f.collection_id = c.id),
                0
            )
        """)
    )


def downgrade() -> None:
    op.drop_column("collections", "feature_count")
