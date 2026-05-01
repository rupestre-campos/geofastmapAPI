"""Add raster_settings JSON to collections.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("raster_settings", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collections", "raster_settings")
