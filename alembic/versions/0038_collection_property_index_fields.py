"""Per-collection JSON property fields to index for faster filters and replace_filtered deletes.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("property_index_fields", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collections", "property_index_fields")
