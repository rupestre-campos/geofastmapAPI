"""STAC catalogs, collection stac_source, raster views.

Revision ID: 0024
Revises: 0023
Create Date: 2026-03-31

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stac_catalogs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("stac_api_root_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("default_collections", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stac_catalogs_enabled", "stac_catalogs", ["enabled"])

    op.add_column(
        "collections",
        sa.Column("stac_source", sa.JSON(), nullable=True),
    )

    op.create_table(
        "raster_views",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="private"),
        sa.Column("json_relative_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raster_views_owner_id", "raster_views", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_raster_views_owner_id", table_name="raster_views")
    op.drop_table("raster_views")
    op.drop_column("collections", "stac_source")
    op.drop_index("ix_stac_catalogs_enabled", table_name="stac_catalogs")
    op.drop_table("stac_catalogs")
