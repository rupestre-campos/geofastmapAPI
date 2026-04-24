"""Add optional request_headers JSON to observability request_events.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "request_events",
        sa.Column("request_headers", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("request_events", "request_headers")
