"""add raster_settings to collections

Revision ID: 0032_collection_raster_settings
Revises: 0031_raster_collections_and_styles
Create Date: 2026-05-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0032_collection_raster_settings"
down_revision = "0031_raster_collections_and_styles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("raster_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "raster_settings")
