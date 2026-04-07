"""Raster views: bbox, definition JSON, allow_public_maps for mosaic planner.

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("raster_views", sa.Column("bbox", JSONB(), nullable=True))
    op.add_column("raster_views", sa.Column("definition", JSONB(), nullable=True))
    op.add_column(
        "raster_views",
        sa.Column("allow_public_maps", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("raster_views", "allow_public_maps")
    op.drop_column("raster_views", "definition")
    op.drop_column("raster_views", "bbox")
