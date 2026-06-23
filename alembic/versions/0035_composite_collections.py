"""Composite collections + state_code expression index for faster replace_filtered deletes.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("composite_members", sa.JSON(), nullable=True),
    )
    op.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_features_cid_state_code
            ON features (collection_id, ((properties->>'state_code')))
            WHERE properties ? 'state_code'
            """
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS idx_features_cid_state_code"))
    op.drop_column("collections", "composite_members")
