"""STAC public tile grants for anonymous map viewers.

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stac_public_tile_grants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=False),
        sa.Column("stac_collection_id", sa.String(length=512), nullable=False),
        sa.Column("stac_item_id", sa.String(length=512), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_id",
            "stac_collection_id",
            "stac_item_id",
            name="uq_stac_public_tile_grants_item",
        ),
    )
    op.create_index(
        "ix_stac_public_tile_grants_lookup",
        "stac_public_tile_grants",
        ["catalog_id", "stac_collection_id", "stac_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_stac_public_tile_grants_lookup", table_name="stac_public_tile_grants")
    op.drop_table("stac_public_tile_grants")
