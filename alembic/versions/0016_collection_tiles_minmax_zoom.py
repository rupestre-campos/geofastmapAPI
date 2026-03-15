"""Add minzoom/maxzoom to collection_tiles for TileJSON and map source.

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-02

- collection_tiles: minzoom, maxzoom (nullable; set when tiles are built)
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("collection_tiles", sa.Column("minzoom", sa.Integer(), nullable=True))
    op.add_column("collection_tiles", sa.Column("maxzoom", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("collection_tiles", "maxzoom")
    op.drop_column("collection_tiles", "minzoom")
