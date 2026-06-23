"""Composite collections (merged static tile layers).

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("composite_members", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collections", "composite_members")
