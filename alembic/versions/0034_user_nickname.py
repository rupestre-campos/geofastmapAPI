"""Add optional unique nickname on users.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("nickname", sa.String(128), nullable=True))
    op.create_index("ix_users_nickname_lower", "users", [sa.text("lower(nickname)")], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_nickname_lower", table_name="users")
    op.drop_column("users", "nickname")
