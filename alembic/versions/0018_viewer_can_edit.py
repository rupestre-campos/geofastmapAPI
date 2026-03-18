"""Add viewer_can_edit to collections and maps.

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-02

When True, everyone who can see the resource (by visibility) can edit;
when False, only owner and explicit editor shares can edit.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("viewer_can_edit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "maps",
        sa.Column("viewer_can_edit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("maps", "viewer_can_edit")
    op.drop_column("collections", "viewer_can_edit")
