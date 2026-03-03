"""Add thumbnail_data to maps for uploaded thumbnails.

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("maps", sa.Column("thumbnail_data", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("maps", "thumbnail_data")
