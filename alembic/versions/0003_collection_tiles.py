"""Add collection_tiles table for PMTiles build tracking.

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-20

- collection_tiles: collection_id, pmtiles_path, built_at, features_updated_at
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_tiles",
        sa.Column("collection_id", sa.String(), primary_key=True),
        sa.Column("pmtiles_path", sa.Text(), nullable=True),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("features_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("collection_tiles")
