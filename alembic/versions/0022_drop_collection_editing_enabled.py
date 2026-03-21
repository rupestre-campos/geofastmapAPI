"""Drop editing_enabled from collections.

Revision ID: 0022
Revises: 0021
Create Date: 2026-03-21

Removes the admin-only "block layer editing" flag; editing permissions
are now governed solely by ownership, shares, and viewer_can_edit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("collections", "editing_enabled")


def downgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "editing_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
