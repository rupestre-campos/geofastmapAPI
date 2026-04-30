"""Add collection_type and raster_styles table.

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("collection_type", sa.String(length=16), nullable=False, server_default="vector"),
    )
    op.create_table(
        "raster_styles",
        sa.Column("collection_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("style_spec", sa.JSON(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("collection_id", "id"),
    )
    op.create_index("ix_raster_styles_id", "raster_styles", ["id"], unique=False)
    op.create_index("ix_raster_styles_owner_id", "raster_styles", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_raster_styles_owner_id", table_name="raster_styles")
    op.drop_index("ix_raster_styles_id", table_name="raster_styles")
    op.drop_table("raster_styles")
    op.drop_column("collections", "collection_type")
