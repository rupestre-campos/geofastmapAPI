"""Add id to created_at index for sortby=created_at index-only scan.

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-02

- Replaces idx_features_collection_created_at with (collection_id, created_at, id)
  so Phase 1 "GROUP BY id, min(created_at) ORDER BY created_at" can use index-only scan
  (all selected columns in index), reducing heap I/O for large tables.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_collection_created_at")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_collection_created_at_id "
        "ON features (collection_id, created_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_features_collection_created_at_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_features_collection_created_at "
        "ON features (collection_id, created_at)"
    )
