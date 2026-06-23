"""Drop hardcoded state_code index (per-collection indexes will be configured in UI).

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS idx_features_cid_state_code"))


def downgrade() -> None:
    pass
