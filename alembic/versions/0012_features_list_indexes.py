"""Add B-tree indexes for items list and count (distinct id, no union).

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-02

- idx_features_collection_id_id: (collection_id, id) for COUNT(DISTINCT id) and
  list phase 1 (SELECT DISTINCT id ORDER BY id LIMIT/OFFSET) — index-only scan per partition.
- idx_features_collection_created_at: (collection_id, created_at) for datetime filters
  and sortby=created_at.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # List/count by collection: distinct ids in order (no geometry/properties)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_collection_id_id ON features (collection_id, id)"
    )
    # Datetime filter and sortby=created_at
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_collection_created_at ON features (collection_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_collection_created_at")
    op.execute("DROP INDEX IF EXISTS idx_features_collection_id_id")
