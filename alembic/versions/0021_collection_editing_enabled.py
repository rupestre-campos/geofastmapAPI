"""Add editing_enabled to collections (admin-only toggle for read-only layers).

Revision ID: 0021
Revises: 0020
Create Date: 2026-03-02

When editing_enabled is False, only administrators may modify the collection or its features.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "editing_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("collections", "editing_enabled")
